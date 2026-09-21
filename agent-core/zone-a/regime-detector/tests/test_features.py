from __future__ import annotations

import numpy as np

from regime_detector.features import N_FEATURES, Scaler, extract_features

Column = list[float | None]


def _cols(n: int = 50) -> tuple[Column, Column, Column]:
    prices: Column = [100.0 + i * 0.01 + (i % 3) * 0.05 for i in range(n)]
    spreads: Column = [0.05 + 0.001 * (i % 4) for i in range(n)]
    ofis: Column = [0.1 * (i % 5 - 2) for i in range(n)]
    return prices, spreads, ofis


def test_feature_shape() -> None:
    result = extract_features(*_cols(50))
    assert result.matrix is not None
    assert result.matrix.shape[1] == N_FEATURES
    assert np.isfinite(result.matrix).all()
    # early returns have no vol yet -> dropped, never zero-filled
    assert result.matrix.shape[0] < 49
    assert result.rows_used == result.matrix.shape[0]


def test_none_rows_dropped_jointly_and_alignment_kept() -> None:
    prices, spreads, ofis = _cols(60)
    spreads[30] = None
    ofis[40] = None
    result = extract_features(prices, spreads, ofis)
    baseline = extract_features(*_cols(60))
    assert result.matrix is not None and baseline.matrix is not None
    assert np.isfinite(result.matrix).all()
    assert result.matrix.shape[0] == baseline.matrix.shape[0] - 2


def test_bad_price_drops_both_adjacent_returns_without_splicing() -> None:
    prices, spreads, ofis = _cols(60)
    prices[30] = None
    result = extract_features(prices, spreads, ofis)
    baseline = extract_features(*_cols(60))
    assert result.matrix is not None and baseline.matrix is not None
    # the return into and the return out of the bad price are both invalid
    assert baseline.matrix.shape[0] - result.matrix.shape[0] == 2


def test_nonfinite_and_nonpositive_treated_as_bad() -> None:
    prices, spreads, ofis = _cols(60)
    prices[10] = float("nan")
    prices[20] = float("inf")
    prices[25] = 0.0
    prices[26] = -3.0
    spreads[35] = float("inf")
    result = extract_features(prices, spreads, ofis)
    assert result.matrix is not None
    assert np.isfinite(result.matrix).all()


def test_no_fake_zero_rows() -> None:
    prices, spreads, ofis = _cols(60)
    ofis[45] = float("nan")
    result = extract_features(prices, spreads, ofis)
    assert result.matrix is not None
    assert not (np.abs(result.matrix) == 0).all(axis=1).any()


def test_length_mismatch_reported_not_raised() -> None:
    prices, spreads, ofis = _cols(30)
    result = extract_features(prices, spreads[:-1], ofis)
    assert result.matrix is None
    assert result.reason == "column_length_mismatch"


def test_too_few_rows_and_all_bad() -> None:
    assert extract_features([100.0], [0.1], [0.0]).reason == "too_few_rows"
    bad = extract_features([None] * 10, [None] * 10, [None] * 10)
    assert bad.matrix is None
    assert bad.reason == "no_finite_rows"


def test_non_numeric_cells_are_bad_not_crash() -> None:
    prices, spreads, ofis = _cols(40)
    prices[5] = "oops"  # type: ignore[assignment]
    result = extract_features(prices, spreads, ofis)
    assert result.matrix is not None


def test_scaler_standardises_and_is_pure(rng: np.random.Generator) -> None:
    raw = rng.normal(loc=[1e-4, 100.0, 5e-4, 0.0], scale=[1e-4, 20.0, 1e-4, 0.3], size=(300, 4))
    original = raw.copy()
    scaler = Scaler.fit(raw)
    scaled = scaler.transform(raw)
    assert np.allclose(scaled.mean(axis=0), 0.0, atol=1e-9)
    assert np.allclose(scaled.std(axis=0), 1.0, atol=1e-9)
    assert np.array_equal(raw, original)


def test_scaler_constant_column_does_not_divide_by_zero() -> None:
    raw = np.column_stack([np.linspace(0, 1, 20), np.full(20, 0.05)])
    scaled = Scaler.fit(raw).transform(raw)
    assert np.isfinite(scaled).all()
    assert np.allclose(scaled[:, 1], 0.0)
