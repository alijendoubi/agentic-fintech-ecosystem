"""Env config for the cognitive-core debate graph. See ADR-002 for rationale."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _float_env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# Model IDs (ADR-002)
BLUE_MODEL = os.environ.get("COGNITIVE_BLUE_MODEL", "mistral-large-2411")
RED_MODEL = os.environ.get("COGNITIVE_RED_MODEL", "mistral-large-2411")
JUDGE_MODEL = os.environ.get("COGNITIVE_JUDGE_MODEL", "anthropic.claude-sonnet-4-6-bedrock")
COMPRESSION_MODEL = os.environ.get("COGNITIVE_COMPRESSION_MODEL", "anthropic.claude-haiku-4-5-bedrock")
REFLECTOR_MODEL = os.environ.get("COGNITIVE_REFLECTOR_MODEL", "anthropic.claude-haiku-4-5-bedrock")

# Latency budgets in seconds (ADR-002 "Latency Budget Allocation")
BLUE_LATENCY_BUDGET_S = _float_env("COGNITIVE_BLUE_LATENCY_BUDGET_S", 0.8)
RED_LATENCY_BUDGET_S = _float_env("COGNITIVE_RED_LATENCY_BUDGET_S", 0.8)
JUDGE_LATENCY_BUDGET_S = _float_env("COGNITIVE_JUDGE_LATENCY_BUDGET_S", 0.5)
COMPRESSION_LATENCY_BUDGET_S = _float_env("COGNITIVE_COMPRESSION_LATENCY_BUDGET_S", 0.2)

# Token budgets (ADR-002 "Cost Controls")
BLUE_RED_MAX_TOKENS = _int_env("COGNITIVE_BLUE_RED_MAX_TOKENS", 4096)
JUDGE_MAX_TOKENS = _int_env("COGNITIVE_JUDGE_MAX_TOKENS", 2048)
COMPRESSION_MAX_TOKENS = _int_env("COGNITIVE_COMPRESSION_MAX_TOKENS", 200)

# Decision threshold — Judge must abstain below this omega regardless of side.
OMEGA_THRESHOLD = _float_env("COGNITIVE_OMEGA_THRESHOLD", 0.55)
