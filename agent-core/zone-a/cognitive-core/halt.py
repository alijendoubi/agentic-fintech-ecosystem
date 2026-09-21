"""Kill-switch / human-in-the-loop halt gate for the runner.

The runner must not emit signals while halted. Two independent triggers:

* env `COGNITIVE_HALT`: a truthy value halts. Read from the process environment, so it is
  a start-up decision (an operator restarts the container to change it).
* file `COGNITIVE_HALT_FILE`: halted while the file exists. Checked on every call, so an
  operator (or a monitor) can halt a live process by creating the file.

Fail closed: an unparsable env value, or any error while looking for the file other than
"it does not exist", counts as halted. This gate is a Zone-A convenience in front of the
Aegis kill-switch hierarchy; it does not replace it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HALT_ENV_VAR = "COGNITIVE_HALT"
HALT_FILE_ENV_VAR = "COGNITIVE_HALT_FILE"
_FALSE = frozenset({"", "0", "false", "no", "off"})
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class HaltStatus:
    halted: bool
    reason: str = ""


RUNNING = HaltStatus(False)


class HaltGate:
    def __init__(
        self, env: Mapping[str, str] | None = None, halt_file: str | Path | None = None
    ) -> None:
        self._env = os.environ if env is None else env
        raw_file = halt_file if halt_file is not None else self._env.get(HALT_FILE_ENV_VAR, "")
        self._file = Path(raw_file) if str(raw_file).strip() else None

    def check(self) -> HaltStatus:
        return self._check_env() or self._check_file() or RUNNING

    def _check_env(self) -> HaltStatus | None:
        raw = self._env.get(HALT_ENV_VAR)
        if raw is None:
            return None
        value = raw.strip().lower()
        if value in _FALSE:
            return None
        if value in _TRUE:
            return HaltStatus(True, f"{HALT_ENV_VAR} is set")
        return HaltStatus(True, f"{HALT_ENV_VAR}={raw!r} is not a boolean (fail closed)")

    def _check_file(self) -> HaltStatus | None:
        if self._file is None:
            return None
        try:
            present = self._file.exists()
        except OSError as exc:
            return HaltStatus(True, f"cannot inspect halt file ({type(exc).__name__})")
        return HaltStatus(True, f"halt file {self._file} exists") if present else None
