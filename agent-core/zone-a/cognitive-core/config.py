"""Explicit, validated settings for the cognitive-core debate graph (ADR-002).

Nothing here touches the environment or the filesystem at import time. Call
`load_settings()` from the composition root; pass `dotenv_path=` to opt in to a
`.env` file. Every invalid value raises `ConfigError` (fail closed): a zero or
negative latency budget or omega threshold must never silently disable a safety
control.

LLM route (ADR-003, Option 2 accepted 2026-09-29): `COGNITIVE_LLM_ROUTE` selects
`gateway` (default) or `bedrock`.

* `gateway`: the debate nodes call the internal LLM gateway (LiteLLM proxy,
  `infrastructure/llm-gateway/config.yaml`) at `COGNITIVE_LLM_GATEWAY_URL` with the
  bearer key `COGNITIVE_LLM_GATEWAY_KEY`. Zone A then holds no provider credentials;
  the gateway maps fixed role aliases to Bedrock model ids, so `COGNITIVE_*_MODEL` are
  NOT used on this route. A missing URL or key makes the client factories raise
  `ConfigError`, which the runner hits at startup (the graph is built before the loop).
* `bedrock`: the previous direct `ChatBedrockConverse` path (IAM role, no API keys),
  kept for backward compatibility and tests. `ENVIRONMENT=production` refuses it, and
  also refuses a non-https gateway URL (the key would cross the network in clear).

Bedrock model IDs below are PLACEHOLDER defaults: ADR-002 names the model
families (Mistral Large 2, Claude Sonnet 4.6, Claude Haiku 4.5) but the exact
Bedrock model IDs / inference-profile prefixes differ per region and account.
Verify against the Bedrock console and override through the COGNITIVE_*_MODEL
environment variables (bedrock route) or the gateway config.yaml (gateway route)
before any live use.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

# --- Spec ceilings (docs/specs/phase_2_cognitive_core.md, ADR-002) ---------------------
BLUE_RED_COMBINED_CEILING_S = 1.5  # spec: Blue + Red < 1,500 ms
OVERALL_DEADLINE_CEILING_S = 2.2  # spec: 1.5 s (Blue+Red) + 0.5 s (Judge) + 0.2 s (Compression)
MAX_SUMMARY_TOKENS_CEILING = 200  # ADR-002: compression output strictly <= 200 tokens
# Safe floor for the abstain threshold. Below 0.5 the Judge would be trading on
# less-than-even confidence. TODO(owner): confirm the floor; the default is 0.55.
OMEGA_THRESHOLD_FLOOR = 0.5

# --- Placeholder Bedrock model IDs: VERIFY AGAINST THE BEDROCK CONSOLE ------------------
_FLOAT_EPS = 1e-9
_DEFAULT_MISTRAL_LARGE_ID = "mistral.mistral-large-2407-v1:0"  # verify against the Bedrock console
_DEFAULT_SONNET_ID = "anthropic.claude-sonnet-4-6"  # verify against the Bedrock console
_DEFAULT_HAIKU_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"  # verify against the Bedrock console

# --- LLM gateway (ADR-003 Option 2) ------------------------------------------------------
LLMRoute = Literal["gateway", "bedrock"]
ENVIRONMENT_VAR = "ENVIRONMENT"
# Same minimum as the repo's other shared secrets (check-env.sh, regime-detector HMAC key).
MIN_GATEWAY_KEY_CHARS = 32
# Same list as regime_detector/config.py and zone-c/hitl-interface/src/lib/config.ts (ALI-21).
PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "change_me",
    "changeme",
    "replace-me",
    "replaceme",
    "placeholder",
    "your-secret",
    "yoursecret",
    "example",
    "default",
    "password",
)

ModelId = Annotated[str, Field(min_length=1, max_length=256)]
PositiveSeconds = Annotated[float, Field(gt=0.0, le=30.0, allow_inf_nan=False)]


class ConfigError(ValueError):
    """Raised when an environment value is missing-valued, unparsable or unsafe."""


class CognitiveSettings(BaseModel):
    """Immutable, fully validated runtime settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Models (all via Bedrock per ADR-002; IAM role auth, no API keys).
    blue_model: ModelId
    red_model: ModelId
    judge_model: ModelId
    compression_model: ModelId
    reflector_model: ModelId
    bedrock_region: str | None = None
    # Interface VPC endpoint URL for bedrock-runtime, if not resolved by private DNS.
    bedrock_endpoint_url: str | None = None

    # LLM route (ADR-003). Gateway URL/key are required by the gateway client factories.
    llm_route: LLMRoute = "gateway"
    llm_gateway_url: str | None = None
    llm_gateway_key: SecretStr | None = None

    # Latency budgets in seconds.
    blue_latency_budget_s: PositiveSeconds
    red_latency_budget_s: PositiveSeconds
    judge_latency_budget_s: PositiveSeconds
    compression_latency_budget_s: PositiveSeconds
    overall_deadline_s: float = Field(gt=0.0, le=OVERALL_DEADLINE_CEILING_S, allow_inf_nan=False)
    reflector_timeout_s: PositiveSeconds

    # Token budgets.
    blue_red_max_tokens: int = Field(gt=0, le=8192)
    judge_max_tokens: int = Field(gt=0, le=8192)
    compression_max_tokens: int = Field(gt=0, le=MAX_SUMMARY_TOKENS_CEILING)
    reflector_max_tokens: int = Field(gt=0, le=8192)

    omega_threshold: float = Field(ge=OMEGA_THRESHOLD_FLOOR, le=1.0, allow_inf_nan=False)

    # How long a signal stays valid for Aegis (TradeSignal.valid_until_ns).
    # TODO(owner): confirm the validity window with the Aegis freshness policy.
    signal_ttl_ms: int = Field(gt=0, le=60_000)

    @model_validator(mode="after")
    def _budgets_within_spec(self) -> CognitiveSettings:
        blue_red = self.blue_latency_budget_s + self.red_latency_budget_s
        if blue_red > BLUE_RED_COMBINED_CEILING_S + _FLOAT_EPS:
            raise ValueError(
                f"blue+red latency budgets sum to {blue_red:.3f}s, "
                f"spec ceiling is {BLUE_RED_COMBINED_CEILING_S}s"
            )
        if self.bedrock_endpoint_url is not None and not self.bedrock_endpoint_url.startswith(
            "https://"
        ):
            raise ValueError("bedrock_endpoint_url must be an https:// URL")
        if self.llm_gateway_url is not None:
            _check_gateway_url(self.llm_gateway_url)
        if self.llm_gateway_key is not None:
            _check_gateway_key(self.llm_gateway_key.get_secret_value())
        return self

    @property
    def summary_char_cap(self) -> int:
        """Approximation used across the package: ~4 characters per token."""
        return self.compression_max_tokens * 4


def _check_gateway_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("llm_gateway_url must be an http:// or https:// URL")
    if not parts.hostname:
        raise ValueError("llm_gateway_url has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("llm_gateway_url must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            "llm_gateway_url must be a bare base URL such as http://llm-gateway:4000 "
            "(the client appends /v1/chat/completions)"
        )


def _check_gateway_key(key: str) -> None:
    # Messages never include the key itself.
    if len(key) < MIN_GATEWAY_KEY_CHARS:
        raise ValueError(f"llm_gateway_key must be at least {MIN_GATEWAY_KEY_CHARS} characters")
    lowered = key.lower()
    if any(fragment in lowered for fragment in PLACEHOLDER_FRAGMENTS):
        raise ValueError("llm_gateway_key looks like a placeholder; set a random key")


def _str(raw: str) -> str:
    return raw.strip()


def _optional_str(raw: str) -> str | None:
    stripped = raw.strip()
    return stripped or None


def _optional_secret(raw: str) -> SecretStr | None:
    stripped = raw.strip()
    return SecretStr(stripped) if stripped else None


def _route(raw: str) -> LLMRoute:
    value = raw.strip().lower()
    if value == "gateway":
        return "gateway"
    if value == "bedrock":
        return "bedrock"
    raise ValueError("must be 'gateway' or 'bedrock'")


def _float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("must be finite")
    return value


# field name -> (env var, parser, default as env-style string or None)
_SPEC: dict[str, tuple[str, Callable[[str], Any], str | None]] = {
    "blue_model": ("COGNITIVE_BLUE_MODEL", _str, _DEFAULT_MISTRAL_LARGE_ID),
    "red_model": ("COGNITIVE_RED_MODEL", _str, _DEFAULT_MISTRAL_LARGE_ID),
    "judge_model": ("COGNITIVE_JUDGE_MODEL", _str, _DEFAULT_SONNET_ID),
    "compression_model": ("COGNITIVE_COMPRESSION_MODEL", _str, _DEFAULT_HAIKU_ID),
    "reflector_model": ("COGNITIVE_REFLECTOR_MODEL", _str, _DEFAULT_HAIKU_ID),
    "bedrock_region": ("COGNITIVE_BEDROCK_REGION", _optional_str, None),
    "bedrock_endpoint_url": ("COGNITIVE_BEDROCK_ENDPOINT_URL", _optional_str, None),
    "llm_route": ("COGNITIVE_LLM_ROUTE", _route, "gateway"),
    "llm_gateway_url": ("COGNITIVE_LLM_GATEWAY_URL", _optional_str, None),
    "llm_gateway_key": ("COGNITIVE_LLM_GATEWAY_KEY", _optional_secret, None),
    # 0.75 + 0.75 = 1.5 s keeps Blue+Red inside the spec ceiling (ADR-002 lists 0.8 s each,
    # which sums to 1.6 s and contradicts the phase-2 spec; the spec wins, owner to reconcile).
    "blue_latency_budget_s": ("COGNITIVE_BLUE_LATENCY_BUDGET_S", _float, "0.75"),
    "red_latency_budget_s": ("COGNITIVE_RED_LATENCY_BUDGET_S", _float, "0.75"),
    "judge_latency_budget_s": ("COGNITIVE_JUDGE_LATENCY_BUDGET_S", _float, "0.5"),
    "compression_latency_budget_s": ("COGNITIVE_COMPRESSION_LATENCY_BUDGET_S", _float, "0.2"),
    "overall_deadline_s": ("COGNITIVE_OVERALL_DEADLINE_S", _float, "2.2"),
    "reflector_timeout_s": ("COGNITIVE_REFLECTOR_TIMEOUT_S", _float, "5.0"),
    "blue_red_max_tokens": ("COGNITIVE_BLUE_RED_MAX_TOKENS", int, "4096"),
    "judge_max_tokens": ("COGNITIVE_JUDGE_MAX_TOKENS", int, "2048"),
    "compression_max_tokens": ("COGNITIVE_COMPRESSION_MAX_TOKENS", int, "200"),
    "reflector_max_tokens": ("COGNITIVE_REFLECTOR_MAX_TOKENS", int, "1024"),
    "omega_threshold": ("COGNITIVE_OMEGA_THRESHOLD", _float, "0.55"),
    "signal_ttl_ms": ("COGNITIVE_SIGNAL_TTL_MS", int, "2000"),
}


def _merged_env(env: Mapping[str, str] | None, dotenv_path: str | Path | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    if dotenv_path is not None:
        path = Path(dotenv_path)
        if not path.is_file():
            raise ConfigError(f"dotenv file not found: {path}")
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update(os.environ if env is None else env)
    return merged


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
) -> CognitiveSettings:
    """Build validated settings.

    `env=None` reads `os.environ`; pass a mapping to be hermetic (tests). A `.env`
    file is loaded only when `dotenv_path` is given, and real environment
    variables override it. Raises `ConfigError` on any invalid value.
    """
    source = _merged_env(env, dotenv_path)
    values: dict[str, Any] = {}
    for field, (var, parse, default) in _SPEC.items():
        raw = source.get(var, default)
        if raw is None:
            values[field] = None
            continue
        try:
            values[field] = parse(raw)
        except ValueError as exc:
            shown = "<redacted>" if field == "llm_gateway_key" else repr(raw)
            raise ConfigError(f"{var}={shown} is not valid: {exc}") from exc
    try:
        settings = CognitiveSettings(**values)
    except ValidationError as exc:
        # Pydantic echoes input values; the gateway key must never reach a log line.
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid cognitive-core settings: {details}") from None
    if source.get(ENVIRONMENT_VAR, "").strip().lower() == "production":
        _check_production_route(settings)
    return settings


def _check_production_route(settings: CognitiveSettings) -> None:
    """ADR-003 Option 2 is the accepted route: production must use the gateway over TLS."""
    if settings.llm_route != "gateway":
        raise ConfigError(
            "ENVIRONMENT=production requires COGNITIVE_LLM_ROUTE=gateway (ADR-003 Option 2); "
            "the direct Bedrock route would need an ADR amendment"
        )
    url = settings.llm_gateway_url
    if url is None or settings.llm_gateway_key is None:
        raise ConfigError(
            "ENVIRONMENT=production requires COGNITIVE_LLM_GATEWAY_URL and "
            "COGNITIVE_LLM_GATEWAY_KEY"
        )
    if not url.startswith("https://"):
        raise ConfigError(
            "ENVIRONMENT=production requires an https:// COGNITIVE_LLM_GATEWAY_URL "
            "(the gateway key must not cross the network in clear)"
        )
