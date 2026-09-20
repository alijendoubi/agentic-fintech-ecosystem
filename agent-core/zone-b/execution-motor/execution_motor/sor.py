"""Smart order router: pick a venue, avoiding venues whose measured toxicity is too high.

The router is a pure decision function. It is given the candidate venue names and OUR OWN
realised observations per venue; it never invents statistics. A venue without enough
evidence is "unscored" and is DENIED unless the operator explicitly sets
``UnscoredPolicy.ALLOW`` (needed during cold start, e.g. the first paper-trading days).

Threshold: ``min(config.max_toxicity, order.max_venue_toxicity)`` when the order sets one
(a non-zero value), so an order can tighten but never loosen the configured limit.
Selection: preferred venue if eligible, else lowest score (unscored last), ties by name.
Score definition: see ``toxicity`` module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import structlog

from .models import Order
from .toxicity import (
    ToxicityParams,
    VenueObservation,
    compute_toxicity,
    filter_window,
)

_log = structlog.get_logger("execution_motor.sor")


class UnscoredPolicy(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class RouterConfig:
    max_toxicity: Decimal = Decimal("0.5")  # UNCALIBRATED placeholder, see toxicity docstring
    params: ToxicityParams = field(default_factory=ToxicityParams)
    unscored_policy: UnscoredPolicy = UnscoredPolicy.DENY
    window_days: int = 5  # matches VENUE_TOXICITY_WINDOW_DAYS in docker-compose

    def __post_init__(self) -> None:
        if not (self.max_toxicity.is_finite() and Decimal(0) <= self.max_toxicity <= Decimal(1)):
            raise ValueError("max_toxicity must be within [0, 1]")
        if self.window_days < 1:
            raise ValueError("window_days must be >= 1")


@dataclass(frozen=True)
class VenueAssessment:
    venue: str
    score: Decimal | None
    eligible: bool
    detail: str


@dataclass(frozen=True)
class RoutingDecision:
    venue: str | None
    threshold: Decimal
    assessments: tuple[VenueAssessment, ...]


class SmartOrderRouter:
    def __init__(self, config: RouterConfig) -> None:
        self._config = config

    def _threshold(self, order: Order) -> Decimal:
        if order.max_venue_toxicity > 0:
            return min(self._config.max_toxicity, order.max_venue_toxicity)
        return self._config.max_toxicity

    def _assess(
        self,
        venue: str,
        observations: Sequence[VenueObservation],
        threshold: Decimal,
        now_ns: int,
    ) -> VenueAssessment:
        windowed = filter_window(observations, now_ns=now_ns, window_days=self._config.window_days)
        result = compute_toxicity(windowed, self._config.params)
        if result is None:
            allowed = self._config.unscored_policy is UnscoredPolicy.ALLOW
            return VenueAssessment(venue, None, allowed, "unscored: insufficient evidence")
        if result.score > threshold:
            return VenueAssessment(venue, result.score, False, "toxicity above threshold")
        return VenueAssessment(venue, result.score, True, "ok")

    def route(
        self,
        order: Order,
        candidates: Sequence[str],
        observations: Mapping[str, Sequence[VenueObservation]],
        *,
        now_ns: int,
    ) -> RoutingDecision:
        threshold = self._threshold(order)
        assessments = tuple(
            self._assess(v, observations.get(v, ()), threshold, now_ns) for v in candidates
        )
        eligible = {a.venue: a for a in assessments if a.eligible}
        chosen: str | None = None
        if order.preferred_venue in eligible:
            chosen = order.preferred_venue
        elif eligible:
            chosen = min(
                eligible.values(),
                key=lambda a: (
                    a.score is None,
                    a.score if a.score is not None else Decimal(0),
                    a.venue,
                ),
            ).venue
        _log.info(
            "route_decision",
            order_id=order.order_id,
            venue=chosen,
            threshold=str(threshold),
            assessments=[(a.venue, str(a.score), a.eligible) for a in assessments],
        )
        return RoutingDecision(venue=chosen, threshold=threshold, assessments=assessments)
