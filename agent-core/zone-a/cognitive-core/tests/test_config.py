from pathlib import Path

import pytest

from cognitive_core import config
from cognitive_core.config import ConfigError, load_settings


def test_defaults_are_valid_and_within_spec() -> None:
    s = load_settings({})
    assert s.omega_threshold == pytest.approx(0.55)
    assert s.blue_latency_budget_s + s.red_latency_budget_s <= 1.5 + 1e-9
    assert s.overall_deadline_s <= config.OVERALL_DEADLINE_CEILING_S
    assert s.compression_max_tokens == 200


def test_import_does_not_touch_environment_or_dotenv() -> None:
    # explicit loading: the module exposes no import-time constants read from the env
    assert not hasattr(config, "OMEGA_THRESHOLD")
    assert not hasattr(config, "load_dotenv")


def test_settings_are_frozen() -> None:
    s = load_settings({})
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on frozen assignment
        s.omega_threshold = 0.1  # type: ignore[misc]


@pytest.mark.parametrize("value", ["0", "-1", "0.1", "0.49", "1.01", "nan", "inf", "", "abc"])
def test_invalid_omega_threshold_raises(value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({"COGNITIVE_OMEGA_THRESHOLD": value})


def test_omega_threshold_boundaries_accepted() -> None:
    assert load_settings({"COGNITIVE_OMEGA_THRESHOLD": "0.5"}).omega_threshold == 0.5
    assert load_settings({"COGNITIVE_OMEGA_THRESHOLD": "1.0"}).omega_threshold == 1.0


@pytest.mark.parametrize("value", ["0", "-0.5", "nan", "inf", "31", "x"])
@pytest.mark.parametrize(
    "var",
    [
        "COGNITIVE_BLUE_LATENCY_BUDGET_S",
        "COGNITIVE_RED_LATENCY_BUDGET_S",
        "COGNITIVE_JUDGE_LATENCY_BUDGET_S",
        "COGNITIVE_COMPRESSION_LATENCY_BUDGET_S",
        "COGNITIVE_OVERALL_DEADLINE_S",
        "COGNITIVE_REFLECTOR_TIMEOUT_S",
    ],
)
def test_invalid_latency_budgets_raise(var: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({var: value})


def test_blue_red_sum_above_spec_ceiling_raises() -> None:
    with pytest.raises(ConfigError, match="spec ceiling"):
        load_settings(
            {"COGNITIVE_BLUE_LATENCY_BUDGET_S": "0.8", "COGNITIVE_RED_LATENCY_BUDGET_S": "0.8"}
        )


def test_overall_deadline_above_spec_raises() -> None:
    with pytest.raises(ConfigError):
        load_settings({"COGNITIVE_OVERALL_DEADLINE_S": "5"})


@pytest.mark.parametrize("value", ["0", "-1", "201", "1.5", "x"])
def test_invalid_compression_tokens_raise(value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({"COGNITIVE_COMPRESSION_MAX_TOKENS": value})


def test_empty_model_id_raises() -> None:
    with pytest.raises(ConfigError):
        load_settings({"COGNITIVE_JUDGE_MODEL": "  "})


def test_model_ids_overridable_and_stripped() -> None:
    s = load_settings({"COGNITIVE_JUDGE_MODEL": " my.model "})
    assert s.judge_model == "my.model"


def test_optional_fields_blank_become_none() -> None:
    s = load_settings({"COGNITIVE_BEDROCK_REGION": " ", "COGNITIVE_BEDROCK_ENDPOINT_URL": ""})
    assert s.bedrock_region is None and s.bedrock_endpoint_url is None


def test_endpoint_url_must_be_https() -> None:
    with pytest.raises(ConfigError, match="https"):
        load_settings({"COGNITIVE_BEDROCK_ENDPOINT_URL": "http://vpce.example"})
    ok = load_settings({"COGNITIVE_BEDROCK_ENDPOINT_URL": "https://vpce.example"})
    assert ok.bedrock_endpoint_url == "https://vpce.example"


@pytest.mark.parametrize("value", ["0", "-5", "60001"])
def test_invalid_signal_ttl_raises(value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({"COGNITIVE_SIGNAL_TTL_MS": value})


def test_load_reads_os_environ_when_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNITIVE_OMEGA_THRESHOLD", "0.9")
    assert load_settings().omega_threshold == pytest.approx(0.9)


def test_dotenv_is_opt_in_and_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("COGNITIVE_OMEGA_THRESHOLD=0.8\nCOGNITIVE_SIGNAL_TTL_MS=3000\n")
    monkeypatch.delenv("COGNITIVE_OMEGA_THRESHOLD", raising=False)
    monkeypatch.delenv("COGNITIVE_SIGNAL_TTL_MS", raising=False)
    assert load_settings().omega_threshold == pytest.approx(0.55)  # not loaded implicitly
    s = load_settings(dotenv_path=dotenv)
    assert s.omega_threshold == pytest.approx(0.8) and s.signal_ttl_ms == 3000
    monkeypatch.setenv("COGNITIVE_OMEGA_THRESHOLD", "0.7")
    assert load_settings(dotenv_path=dotenv).omega_threshold == pytest.approx(0.7)


def test_missing_dotenv_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings({}, dotenv_path=tmp_path / "nope.env")


def test_summary_char_cap() -> None:
    assert load_settings({"COGNITIVE_COMPRESSION_MAX_TOKENS": "50"}).summary_char_cap == 200
