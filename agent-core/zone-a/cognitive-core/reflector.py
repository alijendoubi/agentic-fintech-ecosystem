"""Reflector node: offline, post-trade path (not in the hot debate loop).

Per docs/processes/sharp-promotion.md this module has no authority to change live
rubrics. `build_rubric_change_proposal` returns a DRAFT `RubricChangeProposal`
(requires_human_signoff=True, full Compliance -> Legal -> Backtest -> Risk -> Canary gate)
or `None`. There is no apply/deploy code path anywhere in this package. Any failure
(timeout, provider error, malformed reply) returns `None`: failure text is never stored
as a proposal.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import structlog

from . import prompts
from .config import CognitiveSettings
from .llm_clients import LLMClient
from .models import ReflectorDraft, RubricChangeProposal, TradeOutcome, TradeSignal
from .parsing import parse_json_model

log = structlog.get_logger(__name__)
_LOG_ERROR_CHARS = 200


async def build_rubric_change_proposal(
    trade_signal: TradeSignal,
    outcome: TradeOutcome,
    observed_underperformance: str,
    *,
    client: LLMClient,
    settings: CognitiveSettings,
) -> RubricChangeProposal | None:
    """Draft a rubric change proposal for human review, or None if none can be produced."""
    prompt = prompts.REFLECTOR_SYSTEM_PROMPT.format(
        trade_signal=trade_signal.model_dump_json(),
        realised_pnl=outcome.realised_pnl,
        regime_at_close=outcome.regime_at_close.value,
        outcome_notes=prompts.wrap_untrusted("outcome", outcome.description),
        observed_underperformance=prompts.wrap_untrusted("analyst", observed_underperformance),
        debate_history=prompts.wrap_untrusted("debate", trade_signal.debate_summary),
    )
    try:
        raw = await asyncio.wait_for(
            client.ainvoke(prompt, max_tokens=settings.reflector_max_tokens),
            timeout=settings.reflector_timeout_s,
        )
        draft = parse_json_model(raw, ReflectorDraft)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - offline path must never raise into callers
        log.warning(
            "reflector_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:_LOG_ERROR_CHARS],
            signal_id=trade_signal.signal_id,
            outcome="no_proposal",
        )
        return None
    return RubricChangeProposal(
        proposal_id=str(uuid.uuid4()),
        trigger_signal_id=trade_signal.signal_id,
        created_at_ns=time.time_ns(),
        observed_underperformance=observed_underperformance,
        proposed_change=draft.proposed_change,
        rationale=draft.rationale,
        realised_pnl=outcome.realised_pnl,
        regime_at_close=outcome.regime_at_close,
        debate_history=trade_signal.debate_summary,
    )
