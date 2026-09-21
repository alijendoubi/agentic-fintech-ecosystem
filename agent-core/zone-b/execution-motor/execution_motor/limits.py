"""Defence-in-depth limits: idempotency store and notional caps (Aegis is the primary gate)."""

from __future__ import annotations

import json
import os
import threading
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .errors import ConfigError, IdempotencyStoreError
from .models import Order

_ZERO = Decimal(0)
IDEMPOTENCY_FILENAME = "idempotency.jsonl"


class IdempotencyStore(Protocol):
    def claim(self, key: str) -> bool:
        """Atomically record ``key``. True only for the first caller; False = duplicate.

        A claim is NEVER released, not even after a failure: after an ambiguous submit the
        order may exist at the broker, so re-submission must stay blocked.
        """
        ...


class InMemoryIdempotencyStore:
    """Process-local store. Forgets on restart, so the motor refuses it in production (use
    ``FileIdempotencyStore``); the broker's client_order_id uniqueness is only a second line."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


class FileIdempotencyStore:
    """Persistent single-use store: append-only JSON lines, fsynced BEFORE ``claim`` returns
    True, so a restart cannot forget a claimed signal_id/order_id (replay across restarts).

    Fail closed: an unreadable or corrupt file refuses to load (ConfigError). If a claim cannot
    be persisted, the key is still remembered in memory and ``IdempotencyStoreError`` is
    raised; the motor's last-resort boundary halts and rejects. Single process only: run one
    motor per file (multi-instance needs a shared store such as Redis, a follow-up).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._load()
        try:
            self._fh = path.open("a", encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"idempotency store not writable: {path}") from exc

    def _load(self) -> None:
        if not self._path.exists():
            return
        number = 0
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                number += 1
                key = json.loads(line)["key"]
                if not isinstance(key, str) or not key:
                    raise ValueError("bad key")
                self._seen.add(key)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"idempotency store corrupt near line {number}") from exc

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            try:
                self._fh.write(json.dumps({"key": key}) + "\n")
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except (OSError, ValueError) as exc:  # ValueError: write on a closed file
                raise IdempotencyStoreError("claim could not be persisted") from exc
            return True

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def open_persistent_idempotency_store(state_dir: Path) -> FileIdempotencyStore:
    """File-backed store under ``state_dir`` (created if absent), failing closed.

    A brand-new (absent or empty) directory starts a fresh store. A directory that already
    holds anything but has no store file means the store was deleted or the wrong directory is
    mounted: refuse rather than silently forget every earlier claim. A corrupt store file is
    refused by ``FileIdempotencyStore`` itself.
    """
    path = state_dir / IDEMPOTENCY_FILENAME
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        populated = any(state_dir.iterdir())
    except OSError as exc:
        raise ConfigError(f"state dir unusable: {state_dir}") from exc
    if populated and not path.is_file():
        raise ConfigError(f"idempotency store missing in a non-empty state dir: {state_dir}")
    return FileIdempotencyStore(path)


def _usable_price(value: Decimal | None) -> Decimal | None:
    if value is None or not value.is_finite() or value <= _ZERO:
        return None
    return value


def compute_notional(order: Order, trusted_price: Decimal | None) -> Decimal | None:
    """Worst-case notional: quantity x the highest usable price among the signed limit, the
    signed stop and ``trusted_price`` (the motor's own fresh quote, NEVER a caller-supplied one).

    Returns None when no usable price exists (e.g. a market order with no trusted quote),
    which the motor treats as a rejection: an unknown exposure cannot be capped.
    """
    prices = [
        p
        for p in (
            _usable_price(order.limit_price),
            _usable_price(order.stop_price),
            _usable_price(trusted_price),
        )
        if p is not None
    ]
    if not prices:
        return None
    return order.quantity * max(prices)


class NotionalLedger:
    """Session-cumulative notional reservation. Thread-safe."""

    def __init__(self, cap: Decimal) -> None:
        if not cap.is_finite() or cap <= _ZERO:
            raise ValueError("cap must be > 0")
        self._cap = cap
        self._reserved = _ZERO
        self._lock = threading.Lock()

    @property
    def reserved(self) -> Decimal:
        with self._lock:
            return self._reserved

    def try_reserve(self, amount: Decimal) -> bool:
        if not amount.is_finite() or amount <= _ZERO:
            return False
        with self._lock:
            if self._reserved + amount > self._cap:
                return False
            self._reserved += amount
            return True

    def release(self, amount: Decimal) -> None:
        with self._lock:
            self._reserved = max(_ZERO, self._reserved - amount)
