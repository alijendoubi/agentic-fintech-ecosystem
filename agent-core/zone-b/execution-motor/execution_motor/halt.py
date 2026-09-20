"""In-process kill switch. Fail closed: starts HALTED, halts on any signal-source error.

An external system (e.g. Aegis heartbeat / operator HITL terminal, via Redis or gRPC in a
later package) is plugged in through ``HaltSignalSource``. The motor checks the switch before
every submit. An external halt is sticky: trading resumes only through an explicit ``resume``,
and ``resume`` is ignored while the external source still reports halted.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

import structlog

_log = structlog.get_logger("execution_motor.halt")


class HaltSignalSource(Protocol):
    def is_halted(self) -> bool:
        """True when an external authority demands a halt. May raise; that counts as halted."""
        ...


class KillSwitch:
    def __init__(
        self,
        *,
        start_halted: bool = True,
        signal_source: HaltSignalSource | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._halted = start_halted
        self._reason = "initial state: halted until explicit resume" if start_halted else ""
        self._source = signal_source

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def halt(self, reason: str) -> None:
        with self._lock:
            already = self._halted
            self._halted = True
            self._reason = reason
        if not already:
            _log.critical("kill_switch_halted", reason=reason)

    def resume(self, *, operator: str) -> None:
        """Clear the local halt. No-op (stays halted) without an operator or while the
        external source still demands a halt."""
        if not operator.strip():
            _log.error("kill_switch_resume_refused", why="operator required")
            return
        if self._external_halted():
            _log.error("kill_switch_resume_refused", why="external signal still halted")
            return
        with self._lock:
            self._halted = False
            self._reason = ""
        _log.warning("kill_switch_resumed", operator=operator)

    def is_halted(self) -> bool:
        if self._external_halted():
            return True
        with self._lock:
            return self._halted

    def _external_halted(self) -> bool:
        if self._source is None:
            return False
        try:
            halted = self._source.is_halted()
        except Exception as exc:  # noqa: BLE001 - any signal-source failure must halt (fail closed)
            self.halt(f"signal source error: {type(exc).__name__}")
            return True
        if halted:
            self.halt("external halt signal")
        return halted


class KillStateHaltSource:
    """Adapts Aegis ``KillSwitchState.effective_level`` (0 = KILL_LEVEL_NORMAL) to a halt signal.

    Any level above NORMAL halts new submissions. ``fetch_level`` (e.g. a GetKillSwitchState
    call or a WatchKillSwitchState cache) is supplied by the gRPC wiring; if it raises,
    ``KillSwitch`` treats that as halted.
    """

    def __init__(self, fetch_level: Callable[[], int]) -> None:
        self._fetch = fetch_level

    def is_halted(self) -> bool:
        return int(self._fetch()) != 0
