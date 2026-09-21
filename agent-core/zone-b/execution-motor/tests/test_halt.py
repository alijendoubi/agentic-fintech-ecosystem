from __future__ import annotations

from execution_motor.halt import HaltSignalSource, KillSwitch


class _Signal:
    def __init__(self, halted: bool = False, boom: bool = False) -> None:
        self.halted = halted
        self.boom = boom

    def is_halted(self) -> bool:
        if self.boom:
            raise ConnectionError("redis down")
        return self.halted


def test_starts_halted_by_default() -> None:
    switch = KillSwitch()
    assert switch.is_halted()
    assert "initial" in switch.reason


def test_resume_clears_local_halt() -> None:
    switch = KillSwitch()
    switch.resume(operator="ali")
    assert not switch.is_halted()


def test_halt_sets_reason_and_blocks() -> None:
    switch = KillSwitch(start_halted=False)
    assert not switch.is_halted()
    switch.halt("manual stop")
    assert switch.is_halted()
    assert switch.reason == "manual stop"


def test_external_signal_halts_even_if_local_flag_clear() -> None:
    signal: HaltSignalSource = _Signal(halted=True)
    switch = KillSwitch(start_halted=False, signal_source=signal)
    assert switch.is_halted()
    assert "external" in switch.reason


def test_external_halt_is_sticky_until_explicit_resume() -> None:
    signal = _Signal(halted=True)
    switch = KillSwitch(start_halted=False, signal_source=signal)
    assert switch.is_halted()
    signal.halted = False
    assert switch.is_halted()  # sticky: external clearing alone does not re-enable trading
    switch.resume(operator="ali")
    assert not switch.is_halted()


def test_signal_source_error_fails_closed() -> None:
    switch = KillSwitch(start_halted=False, signal_source=_Signal(boom=True))
    assert switch.is_halted()
    assert "signal source" in switch.reason


def test_resume_refused_while_external_signal_still_halted() -> None:
    signal = _Signal(halted=True)
    switch = KillSwitch(start_halted=False, signal_source=signal)
    switch.resume(operator="ali")
    assert switch.is_halted()


def test_resume_requires_operator_name() -> None:
    switch = KillSwitch()
    switch.resume(operator="")
    assert switch.is_halted()
