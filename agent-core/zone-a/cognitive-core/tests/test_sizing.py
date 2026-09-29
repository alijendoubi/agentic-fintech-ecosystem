"""ALI-160: vol-scaled position sizing (formula, fail-closed inputs, config, runner wiring)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from cognitive_core.config import ConfigError
from cognitive_core.runner_config import RunnerSettings
from cognitive_core.service import CycleOutcome
from cognitive_core.sizing import VolScaledSizer
from cognitive_core.tests.runner_fakes import make_harness, runner_settings, snapshot_json

SIZER_ENV = {
    "COGNITIVE_SIZER": "vol_scaled",
    "COGNITIVE_SIZER_EQUITY_USD": "100000",
    "COGNITIVE_SIZER_RISK_PER_TRADE": "0.01",
    "COGNITIVE_SIZER_MAX_ORDER_NOTIONAL_USD": "25000",
}


def sizer(cap: str = "25000") -> VolScaledSizer:
    return VolScaledSizer(
        equity_usd=Decimal("100000"),
        risk_per_trade=Decimal("0.01"),
        max_order_notional_usd=Decimal(cap),
    )


def test_formula_floors_the_risk_budget() -> None:
    # 100000 * 0.01 * 0.72 = 720 dollars per 1-sigma; 720 / (190 * 0.22 = 41.8) = 17.22 -> 17
    assert sizer().shares(price=190.0, realized_vol=0.22, omega=0.72) == 17


def test_notional_cap_binds_on_low_volatility() -> None:
    # by risk: 1000 / (100 * 0.001) = 10000 shares; cap 2000 / 100 = 20 shares
    assert sizer(cap="2000").shares(price=100.0, realized_vol=0.001, omega=1.0) == 20


def test_size_grows_with_confidence_and_shrinks_with_volatility() -> None:
    s = sizer(cap="1e9")
    assert s.shares(price=50.0, realized_vol=0.2, omega=0.9) > s.shares(
        price=50.0, realized_vol=0.2, omega=0.5
    )
    assert s.shares(price=50.0, realized_vol=0.4, omega=0.9) < s.shares(
        price=50.0, realized_vol=0.2, omega=0.9
    )


@pytest.mark.parametrize(
    ("price", "vol", "omega"),
    [
        (0.0, 0.2, 0.7),
        (-5.0, 0.2, 0.7),
        (100.0, 0.0, 0.7),
        (100.0, 0.2, 0.0),
        (100.0, 0.2, 1.5),
        (math.nan, 0.2, 0.7),
        (100.0, math.inf, 0.7),
    ],
)
def test_bad_inputs_size_to_zero(price: float, vol: float, omega: float) -> None:
    assert sizer().size(price=price, realized_vol=vol, omega=omega) == 0.0


@pytest.mark.parametrize(
    ("equity", "risk", "cap"),
    [
        ("0", "0.01", "1000"),
        ("1000", "0", "1000"),
        ("1000", "0.06", "1000"),
        ("1000", "0.01", "-1"),
    ],
)
def test_invalid_parameters_are_refused(equity: str, risk: str, cap: str) -> None:
    with pytest.raises(ValueError):
        VolScaledSizer(
            equity_usd=Decimal(equity),
            risk_per_trade=Decimal(risk),
            max_order_notional_usd=Decimal(cap),
        )


def test_config_default_is_fixed_quantity() -> None:
    assert RunnerSettings.from_env({}).sizer is None


@pytest.mark.parametrize("missing", sorted(k for k in SIZER_ENV if k != "COGNITIVE_SIZER"))
def test_vol_scaled_has_no_defaults(missing: str) -> None:
    env = {k: v for k, v in SIZER_ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        RunnerSettings.from_env(env)


def test_config_rejects_bad_values_and_stray_parameters() -> None:
    with pytest.raises(ConfigError, match="risk_per_trade"):
        RunnerSettings.from_env({**SIZER_ENV, "COGNITIVE_SIZER_RISK_PER_TRADE": "0.5"})
    with pytest.raises(ConfigError, match="not a decimal"):
        RunnerSettings.from_env({**SIZER_ENV, "COGNITIVE_SIZER_EQUITY_USD": "lots"})
    with pytest.raises(ConfigError, match="fixed' or 'vol_scaled"):
        RunnerSettings.from_env({"COGNITIVE_SIZER": "kelly"})
    with pytest.raises(ConfigError, match="COGNITIVE_SIZER is fixed"):
        RunnerSettings.from_env({"COGNITIVE_SIZER_EQUITY_USD": "100000"})


def test_production_accepts_vol_scaled_without_fixed_quantity() -> None:
    settings = RunnerSettings.from_env({**SIZER_ENV, "ENVIRONMENT": "production"})
    assert isinstance(settings.sizer, VolScaledSizer)
    with pytest.raises(ConfigError, match="COGNITIVE_ORDER_QUANTITY"):
        RunnerSettings.from_env({"ENVIRONMENT": "production"})


@pytest.mark.asyncio
async def test_runner_sizes_after_the_judge() -> None:
    # Judge omega 0.72, snapshot mid 190 / vol 0.22 -> 17 shares (fixed quantity 10 ignored)
    h = make_harness(runner_settings(**SIZER_ENV))
    assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.EMITTED
    (signal,) = h.sink.sent
    assert signal.quantity == 17.0


@pytest.mark.asyncio
async def test_runner_abstains_when_the_sizer_returns_zero() -> None:
    h = make_harness(runner_settings(**SIZER_ENV))
    outcome = await h.runner.handle_message(snapshot_json(realized_volatility=0.0))
    assert outcome is CycleOutcome.ABSTAINED
    assert h.sink.sent == []
