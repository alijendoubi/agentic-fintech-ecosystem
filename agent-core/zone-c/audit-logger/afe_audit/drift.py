"""KL-divergence drift monitor for decision-distribution shift (DORA framework 3.1, EU AI Act Art.
15).

Pure functions plus small frozen value objects. Nothing here is calibrated: thresholds and the
smoothing epsilon are
REQUIRED configuration (no built-in defaults) and must come from the owner's calibration process
(docs/regulatory/ptc-calibration.md does not define KL values yet -- TODO(owner)). The 0.1/0.3/0.5
values in
docker-compose are placeholders, not calibrated limits.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

_FloatArray = NDArray[np.float64]
_SUM_TOLERANCE = 1e-9
ENV_SOFT_ALERT = "KL_SOFT_ALERT_THRESHOLD"
ENV_SOFT_SWITCH = "KL_SOFT_SWITCH_THRESHOLD"
ENV_LOGIC_SWITCH = "KL_LOGIC_SWITCH_THRESHOLD"


class DriftConfigError(ValueError):
    """Thresholds/epsilon are missing or invalid."""


class DriftLevel(StrEnum):
    NONE = "none"
    SOFT_ALERT = "soft_alert"
    SOFT_SWITCH = "soft_switch"
    LOGIC_SWITCH = "logic_switch"


def _validate_distribution(name: str, dist: _FloatArray) -> None:
    if dist.ndim != 1 or dist.size == 0:
        raise ValueError(f"{name} must be a non-empty 1-D array")
    if not np.all(np.isfinite(dist)):
        raise ValueError(f"{name} contains non-finite values")
    if np.any(dist < 0):
        raise ValueError(f"{name} contains negative probabilities")
    if abs(float(dist.sum()) - 1.0) > _SUM_TOLERANCE:
        raise ValueError(f"{name} does not sum to 1")


def kl_divergence(p: _FloatArray, q: _FloatArray) -> float:
    """D_KL(P || Q) in nats. Terms with p_i == 0 contribute 0; p_i > 0 with q_i == 0 gives +inf.

    Use ``align_counts`` (which smooths) to obtain finite values from raw counts."""
    _validate_distribution("p", p)
    _validate_distribution("q", q)
    if p.shape != q.shape:
        raise ValueError("p and q must have the same length")
    mask = p > 0
    if np.any(q[mask] == 0):
        return math.inf
    return float(np.sum(p[mask] * np.log(p[mask] / q[mask])))


def align_counts(
    reference: Mapping[str, int], current: Mapping[str, int], epsilon: float
) -> tuple[_FloatArray, _FloatArray, tuple[str, ...]]:
    """Align two category->count mappings on the sorted union of labels and apply additive
    smoothing:
    ``p_i = (c_i + epsilon) / (N + epsilon * K)``. Returns (reference_dist, current_dist,
    labels)."""
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise ValueError("epsilon must be a positive finite number")
    labels = tuple(sorted(set(reference) | set(current)))
    out: list[_FloatArray] = []
    for name, counts in (("reference", reference), ("current", current)):
        raw = np.array([counts.get(label, 0) for label in labels], dtype=np.float64)
        if np.any(raw < 0):
            raise ValueError(f"{name} counts contain negative values")
        if raw.sum() == 0:
            raise ValueError(f"{name} counts are empty (total 0)")
        out.append((raw + epsilon) / (raw.sum() + epsilon * raw.size))
    return out[0], out[1], labels


@dataclass(frozen=True)
class DriftThresholds:
    """Levels in nats, strictly increasing and positive."""

    soft_alert: float
    soft_switch: float
    logic_switch: float

    def __post_init__(self) -> None:
        values = (self.soft_alert, self.soft_switch, self.logic_switch)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise DriftConfigError("thresholds must be finite and > 0")
        if not self.soft_alert < self.soft_switch < self.logic_switch:
            raise DriftConfigError(
                "thresholds must be strictly increasing: soft_alert < soft_switch < logic_switch"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DriftThresholds:
        source = os.environ if env is None else env
        parsed: list[float] = []
        for key in (ENV_SOFT_ALERT, ENV_SOFT_SWITCH, ENV_LOGIC_SWITCH):
            raw = source.get(key)
            if raw is None or raw.strip() == "":
                raise DriftConfigError(f"required threshold {key} is not set")
            try:
                parsed.append(float(raw))
            except ValueError as exc:
                raise DriftConfigError(f"{key}={raw!r} is not a number") from exc
        return cls(*parsed)


def classify(kl: float, thresholds: DriftThresholds) -> DriftLevel:
    """Map a KL value to a level. Non-finite input (nan/inf) fails closed to the most severe
    level."""
    if not math.isfinite(kl):
        return DriftLevel.LOGIC_SWITCH
    if kl >= thresholds.logic_switch:
        return DriftLevel.LOGIC_SWITCH
    if kl >= thresholds.soft_switch:
        return DriftLevel.SOFT_SWITCH
    if kl >= thresholds.soft_alert:
        return DriftLevel.SOFT_ALERT
    return DriftLevel.NONE


@dataclass(frozen=True)
class DriftReading:
    kl: float
    level: DriftLevel
    labels: tuple[str, ...]
    reference_counts: tuple[int, ...]
    current_counts: tuple[int, ...]

    def to_audit_payload(self) -> dict[str, object]:
        """Audit-safe (float-free) representation: KL is carried as a string with 12 significant
        digits."""
        return {
            "kl_nats": format(self.kl, ".12g"),
            "level": self.level.value,
            "labels": list(self.labels),
            "reference_counts": list(self.reference_counts),
            "current_counts": list(self.current_counts),
        }


@dataclass(frozen=True)
class DriftMonitor:
    thresholds: DriftThresholds
    epsilon: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.epsilon) or self.epsilon <= 0:
            raise DriftConfigError(
                "epsilon (smoothing) must be a positive finite number, supplied by the owner"
            )

    def evaluate(self, reference: Mapping[str, int], current: Mapping[str, int]) -> DriftReading:
        p_dist, q_dist, labels = align_counts(current, reference, self.epsilon)
        # D_KL(current || reference): how surprising the live decision mix is relative to the
        # reference mix.
        kl = kl_divergence(p_dist, q_dist)
        return DriftReading(
            kl=kl,
            level=classify(kl, self.thresholds),
            labels=labels,
            reference_counts=tuple(int(reference.get(x, 0)) for x in labels),
            current_counts=tuple(int(current.get(x, 0)) for x in labels),
        )
