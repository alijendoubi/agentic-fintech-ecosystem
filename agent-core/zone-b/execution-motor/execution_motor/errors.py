"""Exception types. Every one of them is a fail-closed signal: nothing here is retried blindly."""

from __future__ import annotations


class MotorError(Exception):
    """Base class for all execution-motor errors."""


class OrderValidationError(MotorError):
    """An inbound OrderRequest is malformed or ambiguous. The order is never executed."""


class ConfigError(MotorError):
    """Configuration is missing or invalid. The motor refuses to start."""


class IdempotencyStoreError(MotorError):
    """A single-use claim could not be persisted. Fail closed: the motor halts."""


class LiveTradingRefused(ConfigError):
    """A non-paper endpoint was requested without BOTH explicit live opt-ins."""


class BrokerError(MotorError):
    """Base class for broker-client errors."""


class BrokerRejectedError(BrokerError):
    """The broker DEFINITIVELY refused the order (4xx other than timeout/rate-limit).

    Safe to report as REJECTED: the order does not exist at the broker.
    """

    def __init__(self, message: str, *, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


class SubmitOutcomeUnknown(BrokerError):
    """A submit was ambiguous (timeout, 5xx, malformed body) and reconciliation could not
    establish the truth. The order MAY exist at the broker. Never resubmit automatically."""


class BrokerReadError(BrokerError):
    """A read-only call failed after its retries were exhausted."""
