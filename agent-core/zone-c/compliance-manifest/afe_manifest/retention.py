"""Retention metadata.

RETENTION_YEARS = 7 is taken from the repository documents (not independently verified against MiFID
II text --
TODO(owner): legal confirmation that 7 years is the correct/only requirement, and whether longer
holds apply):
  * agent-core/README.md ("Every trade decision generates a Compliance Manifest (Zone C, 7-year
  retention)")
  * agent-core/shared/proto/compliance_manifest.proto ("Retention: 7 years (MiFID II requirement)")
  * agent-core/docs/regulatory/eu-ai-act-limited-risk-disclosure.md (Art. 12 row: 7-year manifest
  retention)
  * agent-core/infrastructure/docker-compose.yml (RETENTION_YEARS=7 for compliance-manifest)
The builder refuses any shorter period.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

RETENTION_YEARS = 7
RETENTION_SOURCES = (
    "agent-core/README.md",
    "agent-core/shared/proto/compliance_manifest.proto",
    "agent-core/docs/regulatory/eu-ai-act-limited-risk-disclosure.md",
    "agent-core/infrastructure/docker-compose.yml",
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def datetime_from_ns(ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=ns // 1000)


def add_years(moment: datetime, years: int) -> datetime:
    """Calendar-aware. A Feb 29 start maps to Mar 1 (the LATER date, i.e. never shortens
    retention)."""
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, month=3, day=1)


def retain_until_ns(created_at_ns: int, years: int = RETENTION_YEARS) -> int:
    end = add_years(datetime_from_ns(created_at_ns), years)
    return ((end - _EPOCH) // timedelta(microseconds=1)) * 1000 + created_at_ns % 1000


def format_ns_utc(ns: int) -> str:
    return datetime_from_ns(ns).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
