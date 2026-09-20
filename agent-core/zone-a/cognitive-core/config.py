"""Explicit, validated settings for the cognitive-core debate graph (ADR-002).

Nothing here touches the environment or the filesystem at import time. Call
`load_settings()` from the composition root; pass `dotenv_path=` to opt in to a
`.env` file. Every invalid value raises `ConfigError` (fail closed): a zero or
negative latency budget or omega threshold must never silently disable a safety
control.

Bedrock model IDs below are PLACEHOLDER defaults: ADR-002 names the model
families (Mistral Large 2, Claude Sonnet 4.6, Claude Haiku 4.5) but the exact
Bedrock model IDs / inference-profile prefixes differ per region and account.
Verify against the Bedrock console and override through the COGNITIVE_*_MODEL
environment variables before any live use.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
        return self

    @property
    def summary_char_cap(self) -> int:
        """Approximation used across the package: ~4 characters per token."""
        return self.compression_max_tokens * 4


def _str(raw: str) -> str:
    return raw.strip()


def _optional_str(raw: str) -> str | None:
    stripped = raw.strip()
    return stripped or None


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
            raise ConfigError(f"{var}={raw!r} is not valid: {exc}") from exc
    try:
        return CognitiveSettings(**values)
    except ValidationError as exc:
        raise ConfigError(f"invalid cognitive-core settings: {exc}") from exc
