"""Tamper-evident on-disk persistence for trained models (ALI-30).

The previous implementation used ``joblib.load`` (pickle) on a file in a shared
volume, which allows code execution if the file is replaced. Models are now
stored as plain numeric arrays (``numpy.savez``, loaded with
``allow_pickle=False``) plus JSON metadata, wrapped in an HMAC-SHA256
envelope. The tag is verified *before* any parsing; a missing key, a bad tag,
a wrong symbol or malformed content means the file is refused and the caller
falls back to UNKNOWN / retraining.

File layout::

    MAGIC (8 bytes) | HMAC-SHA256 tag (32 bytes) | npz payload

The tag covers a domain string, the symbol and the payload, so a valid file
cannot be replayed under another symbol's name.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import structlog
from regime_detector.config import is_valid_symbol
from regime_detector.features import Scaler
from regime_detector.model import (
    HmmParams,
    ModelError,
    TrainedModel,
    assemble_model,
)

log = structlog.get_logger()

MAGIC = b"AFERGM1\n"
_TAG_LEN = hashlib.sha256().digest_size
_DOMAIN = b"afe-regime-model-v1\x00"
FORMAT_VERSION = 1
MAX_MODEL_BYTES = 1_048_576
_LOAD_ERRORS = (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile)


class ModelStoreError(ValueError):
    """Raised for invalid symbols or an unusable envelope."""


def _tag(key: bytes, symbol: str, payload: bytes) -> bytes:
    message = _DOMAIN + symbol.encode("ascii") + b"\x00" + payload
    return hmac.new(key, message, hashlib.sha256).digest()


def serialize_model(model: TrainedModel, symbol: str) -> bytes:
    """Encode ``model`` as npz bytes (no pickle) with JSON metadata inside."""
    meta = {
        "version": FORMAT_VERSION,
        "symbol": symbol,
        "trained_at": model.trained_at,
        "n_train_rows": model.n_train_rows,
        "state_labels": [label.value for label in model.labels],
    }
    buffer = io.BytesIO()
    np.savez(
        buffer,
        startprob=model.params.startprob,
        transmat=model.params.transmat,
        means=model.params.means,
        covars=model.params.covars,
        scaler_mean=model.scaler.mean,
        scaler_scale=model.scaler.scale,
        meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8),
    )
    return buffer.getvalue()


def deserialize_model(payload: bytes, symbol: str) -> TrainedModel:
    """Decode and fully validate npz bytes; raises on any inconsistency."""
    with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
        meta = json.loads(bytes(archive["meta"]).decode("utf-8"))
        params = HmmParams(
            startprob=archive["startprob"],
            transmat=archive["transmat"],
            means=archive["means"],
            covars=archive["covars"],
        )
        scaler = Scaler(mean=archive["scaler_mean"], scale=archive["scaler_scale"])
    if meta.get("version") != FORMAT_VERSION or meta.get("symbol") != symbol:
        raise ModelError("model metadata does not match this symbol/version")
    if not (np.isfinite(scaler.mean).all() and (scaler.scale > 0).all()):
        raise ModelError("scaler parameters are invalid")
    model = assemble_model(
        params, scaler, float(meta["trained_at"]), int(meta["n_train_rows"])
    )
    if [label.value for label in model.labels] != meta.get("state_labels"):
        raise ModelError("stored state labels disagree with the deterministic assignment")
    return model


class ModelStore:
    """Per-symbol, HMAC-verified model files under one directory.

    ``key=None`` disables the store entirely: nothing is loaded or written.
    """

    def __init__(self, directory: Path, key: bytes | None) -> None:
        self._directory = directory
        self._key = key

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def _path(self, symbol: str) -> Path:
        if not is_valid_symbol(symbol):
            raise ModelStoreError(f"invalid symbol {symbol!r}")
        return self._directory / f"regime_{symbol}.model"

    def save(self, symbol: str, model: TrainedModel) -> bool:
        """Atomically write the signed model. Returns False (and logs) on failure."""
        if self._key is None:
            return False
        try:
            path = self._path(symbol)
            payload = serialize_model(model, symbol)
            blob = MAGIC + _tag(self._key, symbol, payload) + payload
            self._directory.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_bytes(blob)
            os.replace(temp, path)
        except (OSError, ModelStoreError, ValueError) as exc:
            log.error("model_persist_failed", symbol=symbol, error=str(exc))
            return False
        log.info("model_persisted", symbol=symbol, path=str(path))
        return True

    def load(self, symbol: str) -> TrainedModel | None:
        """Return the verified model, or None (fail closed) for any problem."""
        if self._key is None:
            return None
        try:
            path = self._path(symbol)
            if not path.exists():
                return None
            return self._read_verified(path, symbol, self._key)
        except _LOAD_ERRORS + (ModelStoreError, ModelError) as exc:
            log.warning("model_load_refused", symbol=symbol, error=str(exc))
            return None

    @staticmethod
    def _read_verified(path: Path, symbol: str, key: bytes) -> TrainedModel:
        if path.stat().st_size > MAX_MODEL_BYTES:
            raise ModelStoreError("model file too large")
        blob = path.read_bytes()
        header = len(MAGIC) + _TAG_LEN
        if len(blob) <= header or not blob.startswith(MAGIC):
            raise ModelStoreError("bad model envelope")
        tag, payload = blob[len(MAGIC) : header], blob[header:]
        if not hmac.compare_digest(tag, _tag(key, symbol, payload)):
            raise ModelStoreError("HMAC mismatch (file tampered or wrong key)")
        model = deserialize_model(payload, symbol)
        log.info("model_loaded", symbol=symbol, path=str(path))
        return model

