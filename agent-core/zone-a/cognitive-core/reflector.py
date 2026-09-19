"""Reflector node — offline, post-trade path (not in the hot debate loop).

Per docs/processes/sharp-promotion.md, this module has no authority to change
live rubrics. `build_rubric_change_proposal` returns a draft object only;
promotion requires Compliance -> Legal -> Backtesting -> Risk -> Canary
sign-off outside this codebase.
"""

from __future__ import annotations

import asyncio
import uuid

import config
import prompts
from llm_clients import LLMClient, get_reflector_client
from models import RubricChangeProposal, TradeSignal


async def build_rubric_change_proposal(
    trade_signal: TradeSignal,
    outcome: str,
    observed_underperformance: str,
    client: LLMClient | None = None,
    timeout_s: float = 5.0,
) -> RubricChangeProposal:
    client = client or get_reflector_client()
    prompt = prompts.REFLECTOR_SYSTEM_PROMPT.format(
        trade_signal=trade_signal.model_dump_json(),
        outcome=outcome,
    )
    try:
        proposed_change = await asyncio.wait_for(
            client.ainvoke(prompt, max_tokens=config.COMPRESSION_MAX_TOKENS),
            timeout=timeout_s,  # offline path, not latency-budgeted like the hot loop
        )
    except (TimeoutError, asyncio.TimeoutError):
        proposed_change = "reflector timed out — no change proposed, escalate manually"

    return RubricChangeProposal(
        proposal_id=str(uuid.uuid4()),
        trigger_signal_id=trade_signal.signal_id,
        observed_underperformance=observed_underperformance,
        proposed_change=proposed_change,
        rationale=f"outcome={outcome}",
    )
