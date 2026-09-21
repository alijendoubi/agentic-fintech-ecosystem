from __future__ import annotations

import numpy as np
import pytest
from synthetic import make_columns

from regime_detector.features import extract_features
from regime_detector.labels import STATE_REGIMES, RegimeLabel
from regime_detector.model import (
    N_STATES,
    HmmParams,
    ModelError,
    TrainingError,
    apply_confidence_floor,
    assign_state_labels,
    build_hmm,
    predict_regime,
    train_model,
    unknown,
    validate_params,
)


@pytest.fixture(scope="module")
def trained_and_features():  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(2024)
    build = extract_features(*make_columns(rng, n_per_regime=90))
    assert build.matrix is not None
    return train_model(build.matrix, trained_at=1_000.0), build.matrix


def _means(returns: list[float], vols: list[float]) -> np.ndarray:
    return np.column_stack([returns, vols, np.zeros(5), np.zeros(5)])


def test_hmm_has_five_states(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    model, _ = trained_and_features
    assert model.hmm.n_components == 5


def test_label_assignment_by_return_and_vol_ordering() -> None:
    # state:      0     1     2     3     4
    returns = [0.9, -0.9, 0.1, -0.1, 0.0]
    vols = [-0.5, -0.4, 0.5, -1.0, 3.0]
    labels = assign_state_labels(_means(returns, vols))
    assert labels[4] is RegimeLabel.CRISIS  # highest vol
    assert labels[0] is RegimeLabel.TRENDING_BULL  # highest return of the rest
    assert labels[1] is RegimeLabel.TRENDING_BEAR  # lowest return of the rest
    assert labels[2] is RegimeLabel.HIGH_VOL_CHOP  # highest vol of the rest
    assert labels[3] is RegimeLabel.LOW_VOL_CHOP


def test_label_assignment_covers_all_five_labels_for_any_ordering(
    rng: np.random.Generator,
) -> None:
    for _ in range(50):
        labels = assign_state_labels(rng.normal(size=(5, 4)))
        assert set(labels) == set(STATE_REGIMES)


def test_label_assignment_is_deterministic_and_breaks_ties_by_index() -> None:
    tied = np.zeros((5, 4))
    first = assign_state_labels(tied)
    assert first == assign_state_labels(tied.copy())
    assert first[0] is RegimeLabel.CRISIS  # lowest index wins every tie


def test_label_assignment_invariant_to_state_permutation_of_values() -> None:
    returns = [0.9, -0.9, 0.1, -0.1, 0.0]
    vols = [-0.5, -0.4, 0.5, -1.0, 3.0]
    base = assign_state_labels(_means(returns, vols))
    perm = [3, 0, 4, 1, 2]
    permuted = assign_state_labels(_means([returns[i] for i in perm], [vols[i] for i in perm]))
    assert [permuted[k] for k in range(5)] == [base[i] for i in perm]


def test_label_assignment_rejects_bad_means() -> None:
    with pytest.raises(ModelError):
        assign_state_labels(np.zeros((4, 4)))
    bad = np.zeros((5, 4))
    bad[2, 1] = np.nan
    with pytest.raises(ModelError):
        assign_state_labels(bad)


def test_trained_model_uses_all_five_labels(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    model, _ = trained_and_features
    assert set(model.labels) == set(STATE_REGIMES)


def test_return_feature_is_not_ignored_after_standardisation(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    """ALI-29: covariance of the standardised return column is on a unit scale."""
    model, _ = trained_and_features
    return_var = model.params.covars[:, 0, 0]
    assert (return_var >= 1e-2 - 1e-12).all()
    assert model.scaler.scale[0] < 1e-2 < model.scaler.scale[1]  # raw scales differ hugely


def test_confidence_in_range_and_posteriors_sum_to_one(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    model, features = trained_and_features
    posteriors = model.hmm.predict_proba(model.scaler.transform(features[-10:]))
    assert np.allclose(posteriors.sum(axis=1), 1.0, atol=1e-6)
    prediction = predict_regime(model, features)
    assert prediction.label in STATE_REGIMES
    assert 0.0 <= prediction.confidence <= 1.0


def test_trained_labels_follow_the_state_means(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    """Labels on a model fitted to synthetic data obey the documented ordering.

    Whether the HMM recovers the *generating* regimes is not asserted: that is a
    property of real data and needs owner validation (TODO(owner)).
    """
    model, _ = trained_and_features
    means = model.params.means
    by_label = {label: i for i, label in enumerate(model.labels)}
    assert by_label[RegimeLabel.CRISIS] == int(np.argmax(means[:, 1]))
    others = [i for i in range(5) if i != by_label[RegimeLabel.CRISIS]]
    assert by_label[RegimeLabel.TRENDING_BULL] == max(others, key=lambda i: means[i, 0])


def test_predict_untrained_or_bad_input_is_unknown(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    model, features = trained_and_features
    assert predict_regime(None, features).label is RegimeLabel.UNKNOWN
    assert predict_regime(model, None).reason == "bad_features"
    assert predict_regime(model, features[:0]).label is RegimeLabel.UNKNOWN
    poisoned = features.copy()
    poisoned[-1, 2] = np.nan
    assert predict_regime(model, poisoned).label is RegimeLabel.UNKNOWN
    assert predict_regime(model, features[:, :3]).label is RegimeLabel.UNKNOWN


def test_confidence_floor_downgrades_to_unknown_without_mutation() -> None:
    from regime_detector.model import Prediction

    original = Prediction(RegimeLabel.CRISIS, 0.4, 2)
    floored = apply_confidence_floor(original, 0.55)
    assert floored.label is RegimeLabel.UNKNOWN and floored.confidence == 0.0
    assert floored.reason is not None and floored.reason.startswith("low_confidence")
    assert original.label is RegimeLabel.CRISIS
    assert apply_confidence_floor(Prediction(RegimeLabel.CRISIS, 0.9, 2), 0.55).label is (
        RegimeLabel.CRISIS
    )
    assert unknown("x").label is RegimeLabel.UNKNOWN


def test_train_rejects_bad_features() -> None:
    with pytest.raises(TrainingError):
        train_model(np.zeros((50, 3)), 0.0)
    bad = np.ones((50, 4))
    bad[3, 1] = np.inf
    with pytest.raises(TrainingError):
        train_model(bad, 0.0)


def test_train_too_little_data_fails_closed() -> None:
    rng = np.random.default_rng(1)
    with pytest.raises(TrainingError):
        train_model(rng.normal(size=(3, 4)), 0.0)


def test_validate_params_rejects_corruption(trained_and_features) -> None:  # type: ignore[no-untyped-def]
    model, _ = trained_and_features
    good = model.params
    validate_params(good)
    bad_trans = np.array(good.transmat)
    bad_trans[0, 0] += 0.5
    with pytest.raises(ModelError):
        validate_params(HmmParams(good.startprob, bad_trans, good.means, good.covars))
    nan_means = np.array(good.means)
    nan_means[0, 0] = np.nan
    with pytest.raises(ModelError):
        validate_params(HmmParams(good.startprob, good.transmat, nan_means, good.covars))
    bad_cov = np.array(good.covars)
    bad_cov[1] = -np.eye(4)
    with pytest.raises(ModelError):
        build_hmm(HmmParams(good.startprob, good.transmat, good.means, bad_cov))
    with pytest.raises(ModelError):
        validate_params(HmmParams(good.startprob[:4], good.transmat, good.means, good.covars))


def test_n_states_constant() -> None:
    assert N_STATES == 5
