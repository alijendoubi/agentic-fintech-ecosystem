"""Structural interface to the Zone C audit logger (``afe_audit.AuditLogger`` satisfies it). Kept
local so this
package does not import the audit-logger package (each service is built from its own directory)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class AuditSink(Protocol):
    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> Any:
        """Durably record the event or raise. Raising means the action must not proceed."""
        ...
