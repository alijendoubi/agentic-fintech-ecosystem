import math

import numpy as np
import pytest

from afe_audit.canonical import canonical_json
from afe_audit.drift import (
    DriftConfigError,
    DriftLevel,
    DriftMonitor,
    DriftThresholds,
    align_counts,
    classify,
    kl_divergence,
)


def test_kl_of_identical_distributions_is_zero() -> None:
    p = np.array([0.2, 0.3, 0.5])
    assert kl_divergence(p, p) == pytest.approx(0.0, abs=1e-12)


def test_kl_matches_hand_computation() -> None:
    p = np.array([0.5, 0.5])
    q = np.array([0.9, 0.1])
    expected = 0.5 * math.log(0.5 / 0.9) + 0.5 * math.log(0.5 / 0.1)
    assert kl_divergence(p, q) == pytest.approx(expected)


def test_kl_is_asymmetric() -> None:
    p = np.array([0.5, 0.5])
    q = np.array([0.9, 0.1])
    assert kl_divergence(p, q) != pytest.approx(kl_divergence(q, p))


def test_kl_with_zero_in_q_is_infinite_without_smoothing() -> None:
    assert kl_divergence(np.array([0.5, 0.5]), np.array([1.0, 0.0])) == math.inf


def test_kl_zero_p_terms_contribute_nothing() -> None:
    assert kl_divergence(np.array([1.0, 0.0]), np.array([0.5, 0.5])) == pytest.approx(math.log(2))


@pytest.mark.parametrize(
    ("p", "q"),
    [
        ([0.5, 0.5], [1.0]),
        ([-0.1, 1.1], [0.5, 0.5]),
        ([0.5, 0.6], [0.5, 0.5]),
        ([0.5, 0.5], [float("nan"), 1.0]),
        ([], []),
    ],
)
def test_kl_rejects_invalid_input(p: list[float], q: list[float]) -> None:
    with pytest.raises(ValueError, match=r"."):
        kl_divergence(np.array(p), np.array(q))


def test_align_counts_uses_union_of_categories_and_smooths() -> None:
    p, q, labels = align_counts({"BUY": 8, "SELL": 2}, {"BUY": 5, "HOLD": 5}, epsilon=0.5)
    assert labels == ("BUY", "HOLD", "SELL")
    assert p.sum() == pytest.approx(1.0)
    assert q.sum() == pytest.approx(1.0)
    assert np.all(p > 0)
    assert np.all(q > 0)


def test_align_counts_rejects_empty_and_negative_and_bad_epsilon() -> None:
    with pytest.raises(ValueError, match="epsilon"):
        align_counts({"a": 1}, {"a": 1}, epsilon=0.0)
    with pytest.raises(ValueError, match="negative"):
        align_counts({"a": -1}, {"a": 1}, epsilon=0.5)
    with pytest.raises(ValueError, match="empty"):
        align_counts({"a": 0}, {"a": 1}, epsilon=0.0001)


def test_thresholds_must_be_strictly_increasing_and_positive() -> None:
    DriftThresholds(soft_alert=0.1, soft_switch=0.2, logic_switch=0.3)
    for bad in ((0.3, 0.2, 0.1), (0.1, 0.1, 0.2), (0.0, 0.1, 0.2), (0.1, 0.2, float("inf"))):
        with pytest.raises(DriftConfigError):
            DriftThresholds(*bad)


def test_thresholds_from_env_requires_all_variables() -> None:
    env = {
        "KL_SOFT_ALERT_THRESHOLD": "0.1",
        "KL_SOFT_SWITCH_THRESHOLD": "0.3",
        "KL_LOGIC_SWITCH_THRESHOLD": "0.5",
    }
    t = DriftThresholds.from_env(env)
    assert (t.soft_alert, t.soft_switch, t.logic_switch) == (0.1, 0.3, 0.5)
    missing = {k: v for k, v in env.items() if k != "KL_SOFT_SWITCH_THRESHOLD"}
    with pytest.raises(DriftConfigError, match="KL_SOFT_SWITCH_THRESHOLD"):
        DriftThresholds.from_env(missing)
    with pytest.raises(DriftConfigError, match="not a number"):
        DriftThresholds.from_env({**env, "KL_SOFT_ALERT_THRESHOLD": "abc"})


def test_classify_boundaries_and_non_finite_fails_closed() -> None:
    t = DriftThresholds(0.1, 0.3, 0.5)
    assert classify(0.0999, t) is DriftLevel.NONE
    assert classify(0.1, t) is DriftLevel.SOFT_ALERT
    assert classify(0.3, t) is DriftLevel.SOFT_SWITCH
    assert classify(0.5, t) is DriftLevel.LOGIC_SWITCH
    assert classify(math.inf, t) is DriftLevel.LOGIC_SWITCH
    assert classify(math.nan, t) is DriftLevel.LOGIC_SWITCH


def test_monitor_flags_shift_and_reports_labels() -> None:
    mon = DriftMonitor(thresholds=DriftThresholds(0.05, 0.2, 0.5), epsilon=1e-3)
    same = mon.evaluate({"BUY": 50, "SELL": 50}, {"BUY": 52, "SELL": 48})
    assert same.level is DriftLevel.NONE
    shifted = mon.evaluate({"BUY": 50, "SELL": 50}, {"BUY": 95, "SELL": 5})
    assert shifted.level is DriftLevel.SOFT_SWITCH
    collapsed = mon.evaluate({"BUY": 50, "SELL": 50}, {"HOLD": 100})
    assert collapsed.level is DriftLevel.LOGIC_SWITCH
    assert collapsed.labels == ("BUY", "HOLD", "SELL")


def test_monitor_epsilon_is_required_and_validated() -> None:
    with pytest.raises(DriftConfigError, match="epsilon"):
        DriftMonitor(thresholds=DriftThresholds(0.1, 0.2, 0.3), epsilon=0.0)


def test_reading_payload_is_audit_safe() -> None:
    mon = DriftMonitor(thresholds=DriftThresholds(0.1, 0.2, 0.3), epsilon=1e-3)
    reading = mon.evaluate({"BUY": 10, "SELL": 10}, {"BUY": 15, "SELL": 5})
    payload = reading.to_audit_payload()
    canonical_json(payload)  # must contain no floats
    assert payload["level"] == reading.level.value
