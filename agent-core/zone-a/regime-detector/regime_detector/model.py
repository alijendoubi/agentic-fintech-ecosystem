"""Gaussian HMM training, deterministic state labelling and inference.

Features are standardised before fitting (the scaler is part of the trained
model and is persisted with it) and ``min_covar`` is set explicitly in
standardised units, so the return feature is no longer swamped by the
annualised-vol feature (ALI-29).

Every failure path returns or raises a fail-closed result: an untrained,
invalid or erroring model yields ``RegimeLabel.UNKNOWN``, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from hmmlearn.hmm import GaussianHMM

from regime_detector.features import N_FEATURES, Scaler
from regime_detector.labels import N_STATES, RegimeLabel

N_ITER = 200
TOL = 1e-4
RANDOM_STATE = 42
#: Diagonal regularisation floor, in *standardised* units (features have unit variance).
MIN_COVAR = 1e-2
#: Posteriors are averaged over the last few bars (per phase_1 spec).
SMOOTHING_BARS = 5
_STOCHASTIC_TOL = 1e-6
_RETURN_COL = 0
_VOL_COL = 1


class ModelError(ValueError):
    """Raised for invalid, non-finite or malformed model parameters."""


class TrainingError(ModelError):
    """Raised when HMM training cannot produce a valid model."""


@dataclass(frozen=True, slots=True)
class HmmParams:
    """Plain-array HMM parameters (what gets persisted; never a pickle)."""

    startprob: np.ndarray
    transmat: np.ndarray
    means: np.ndarray
    covars: np.ndarray


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """Immutable trained model for one symbol. ``labels[i]`` names HMM state ``i``."""

    hmm: GaussianHMM
    params: HmmParams
    scaler: Scaler
    labels: tuple[RegimeLabel, ...]
    trained_at: float
    n_train_rows: int


@dataclass(frozen=True, slots=True)
class Prediction:
    """One inference result; ``UNKNOWN`` carries a machine-readable ``reason``."""

    label: RegimeLabel
    confidence: float
    state_index: int | None = None
    reason: str | None = None


def unknown(reason: str) -> Prediction:
    """Fail-closed prediction."""
    return Prediction(RegimeLabel.UNKNOWN, 0.0, None, reason)


# ── labelling ────────────────────────────────────────────────────────────────


def _pick(remaining: list[int], values: np.ndarray, *, largest: bool) -> int:
    """Pick from ``remaining`` (ascending) by value; ties go to the lowest state index."""
    subset = values[remaining]
    position = int(np.argmax(subset) if largest else np.argmin(subset))
    return remaining[position]


def assign_state_labels(means: np.ndarray) -> tuple[RegimeLabel, ...]:
    """Map each HMM state to a regime by its mean return / vol ordering.

    Deterministic (ties broken by lowest state index) and invariant to the
    positive per-feature scaling used for standardisation:

      1. highest mean vol                    -> CRISIS
      2. highest mean return of the rest     -> TRENDING_BULL
      3. lowest mean return of the rest      -> TRENDING_BEAR
      4. highest mean vol of the rest        -> HIGH_VOL_CHOP
      5. the remaining state                 -> LOW_VOL_CHOP
    """
    if means.ndim != 2 or means.shape[0] != N_STATES or means.shape[1] <= _VOL_COL:
        raise ModelError(f"means must have shape ({N_STATES}, >= 2), got {means.shape}")
    if not np.isfinite(means).all():
        raise ModelError("means contain non-finite values")
    returns, vols = means[:, _RETURN_COL], means[:, _VOL_COL]
    remaining = list(range(N_STATES))
    labels: dict[int, RegimeLabel] = {}
    steps = (
        (RegimeLabel.CRISIS, vols, True),
        (RegimeLabel.TRENDING_BULL, returns, True),
        (RegimeLabel.TRENDING_BEAR, returns, False),
        (RegimeLabel.HIGH_VOL_CHOP, vols, True),
    )
    for label, values, largest in steps:
        chosen = _pick(remaining, values, largest=largest)
        labels[chosen] = label
        remaining.remove(chosen)
    labels[remaining[0]] = RegimeLabel.LOW_VOL_CHOP
    return tuple(labels[i] for i in range(N_STATES))


# ── parameter validation / (de)construction ─────────────────────────────────


def validate_params(params: HmmParams) -> None:
    """Raise ``ModelError`` unless the parameters describe a sane 5-state model."""
    expected = {
        "startprob": (params.startprob, (N_STATES,)),
        "transmat": (params.transmat, (N_STATES, N_STATES)),
        "means": (params.means, (N_STATES, N_FEATURES)),
        "covars": (params.covars, (N_STATES, N_FEATURES, N_FEATURES)),
    }
    for name, (array, shape) in expected.items():
        if array.shape != shape:
            raise ModelError(f"{name} has shape {array.shape}, expected {shape}")
        if not np.isfinite(array).all():
            raise ModelError(f"{name} contains non-finite values")
    if abs(float(params.startprob.sum()) - 1.0) > _STOCHASTIC_TOL or (params.startprob < 0).any():
        raise ModelError("startprob is not a probability vector")
    row_sums = params.transmat.sum(axis=1)
    if (np.abs(row_sums - 1.0) > _STOCHASTIC_TOL).any() or (params.transmat < 0).any():
        raise ModelError("transmat is not row-stochastic")
    for covar in params.covars:
        try:
            np.linalg.cholesky(covar)
        except np.linalg.LinAlgError as exc:
            raise ModelError("covariance is not positive definite") from exc


def build_hmm(params: HmmParams) -> GaussianHMM:
    """Reconstruct a ready-to-predict ``GaussianHMM`` from validated parameters."""
    validate_params(params)
    hmm = GaussianHMM(n_components=N_STATES, covariance_type="full", min_covar=MIN_COVAR)
    hmm.startprob_ = params.startprob.copy()
    hmm.transmat_ = params.transmat.copy()
    hmm.means_ = params.means.copy()
    hmm.covars_ = params.covars.copy()
    return hmm


def _params_from_hmm(hmm: GaussianHMM) -> HmmParams:
    arrays = [
        np.array(a, dtype=np.float64, copy=True)
        for a in (hmm.startprob_, hmm.transmat_, hmm.means_, hmm.covars_)
    ]
    for array in arrays:
        array.setflags(write=False)
    return HmmParams(*arrays)


def assemble_model(
    params: HmmParams, scaler: Scaler, trained_at: float, n_train_rows: int
) -> TrainedModel:
    """Build a ``TrainedModel`` from parameters (used by training and by the store)."""
    hmm = build_hmm(params)
    labels = assign_state_labels(params.means)
    return TrainedModel(hmm, params, scaler, labels, trained_at, n_train_rows)


# ── training ─────────────────────────────────────────────────────────────────


def train_model(features: np.ndarray, trained_at: float) -> TrainedModel:
    """Fit a new model on a finite (N, 4) feature matrix.

    Raises ``TrainingError`` on any failure; the caller must keep serving the
    previous model (or UNKNOWN) and must not treat the attempt as a success.
    """
    if features.ndim != 2 or features.shape[1] != N_FEATURES:
        raise TrainingError(f"features must have shape (N, {N_FEATURES}), got {features.shape}")
    if not np.isfinite(features).all():
        raise TrainingError("features contain non-finite values")
    scaler = Scaler.fit(features)
    fitter = GaussianHMM(
        n_components=N_STATES,
        covariance_type="full",
        n_iter=N_ITER,
        tol=TOL,
        min_covar=MIN_COVAR,
        random_state=RANDOM_STATE,
    )
    try:
        fitter.fit(scaler.transform(features))
        params = _params_from_hmm(fitter)
        return assemble_model(params, scaler, trained_at, len(features))
    except (ValueError, np.linalg.LinAlgError, FloatingPointError) as exc:
        raise TrainingError(f"HMM fit failed: {exc}") from exc


# ── inference ────────────────────────────────────────────────────────────────


def predict_regime(model: TrainedModel | None, features: np.ndarray | None) -> Prediction:
    """Regime for the latest bar; UNKNOWN if the model is missing or the input is bad."""
    if model is None:
        return unknown("model_untrained")
    if features is None or features.ndim != 2 or features.shape[1] != N_FEATURES:
        return unknown("bad_features")
    if len(features) < 1 or not np.isfinite(features).all():
        return unknown("bad_features")
    window = model.scaler.transform(features[-SMOOTHING_BARS:])
    try:
        posteriors = model.hmm.predict_proba(window)
    except (ValueError, np.linalg.LinAlgError, FloatingPointError):
        return unknown("inference_error")
    mean_posterior = posteriors.mean(axis=0)
    if not np.isfinite(mean_posterior).all():
        return unknown("inference_error")
    state = int(np.argmax(mean_posterior))
    return Prediction(model.labels[state], float(mean_posterior[state]), state)


def apply_confidence_floor(prediction: Prediction, floor: float) -> Prediction:
    """Downgrade a low-confidence prediction to UNKNOWN (returns a new object)."""
    if prediction.label is RegimeLabel.UNKNOWN or prediction.confidence >= floor:
        return prediction
    return replace(
        prediction,
        label=RegimeLabel.UNKNOWN,
        confidence=0.0,
        reason=f"low_confidence:{prediction.confidence:.3f}",
    )
