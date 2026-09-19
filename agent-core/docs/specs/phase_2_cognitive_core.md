# Phase 2 Spec — Cognitive Core

_Zone A (Python) | Latency budget: Blue+Red < 1,500ms, Judge < 500ms, Compression < 200ms (see ADR-002)_

---

## Scope

Build the LangGraph debate pipeline that turns a `MarketSnapshot` + `RegimeLabel` into a `TradeSignal` (see `shared/proto/trade_signal.proto`) for Aegis PTC validation.

| Node | Model (ADR-002) | Role |
|---|---|---|
| Blue | Mistral Large 2 | Proposes trade thesis, CoT |
| Red | Mistral Large 2 (adversarial prompt) | Challenges thesis w/ historical failure patterns |
| Judge | Claude Sonnet (Bedrock) | Synthesizes debate, computes ω confidence |
| Compression | Claude Haiku (Bedrock) | ≤200-token debate summary for `TradeSignal.debate_summary` |
| Reflector | Claude Haiku (Bedrock) | Post-trade analysis, drafts SHARP rubric change proposals (never auto-applies — human gate per `docs/processes/sharp-promotion.md`) |

## Module Map

```
zone-a/cognitive-core/
  models.py        — DebateState, BlueThesis, RedChallenge, JudgeVerdict, TradeSignal,
                      SignalSide/SignalStatus enums (mirrors trade_signal.proto), RubricChangeProposal
  config.py        — env config: model IDs, token budgets, latency budgets (ADR-002)
  llm_clients.py    — LLMClient protocol + lazy factories per node (no network call at import time)
  prompts.py        — prompt templates for Blue/Red/Judge/Compression/Reflector
  graph.py          — LangGraph StateGraph: blue -> red -> judge -> compression -> END
                      each node wrapped in asyncio.wait_for() per ADR-002 latency budget
  reflector.py      — build_rubric_change_proposal(); output requires human sign-off, no auto-deploy
  tests/
    test_models.py
    test_graph.py
    test_reflector.py
```

## Debate Flow

```
DebateState(market_context, regime_label, regime_confidence)
  → blue_node   → BlueThesis(side, rationale, key_factors)          [budget 800ms, else force completion]
  → red_node    → RedChallenge(counter_factors, failure_patterns)   [budget 800ms, else force completion]
  → judge_node  → JudgeVerdict(omega, side, p_success, p_failure,
                                reward_estimate, risk_estimate)     [budget 500ms, else default abstain]
  → compression_node → debate_summary (<=200 tokens)                [budget 200ms, else skip -> raw summary]
  → TradeSignal (status=SIGNAL_ABSTAIN if omega < OMEGA_THRESHOLD, else SIGNAL_PENDING)
```

- `OMEGA_THRESHOLD` default `0.55` (env-configurable) — below this, Judge must abstain regardless of side.
- Blue/Red run on the same model family by design (ADR-002) — adversarial framing comes from the prompt, not model diversity.
- Judge's `debate_summary` fallback (on Compression timeout) is `BlueThesis.rationale[:200] + RedChallenge.counter_factors[:200]`, truncated to the same 200-token cap enforced elsewhere — never left empty.

## Reflector (post-trade, offline path — not in the hot debate loop)

Input: closed trade outcome (`TradeSignal` + realized P&L + regime at close) + underperforming pattern detected.
Output: `RubricChangeProposal` — natural-language draft only. Per `sharp-promotion.md`, this MUST pass Compliance → Legal → Backtesting → Risk → Canary before any live effect. The Reflector code path has no write access to live prompts/weights — it only produces the proposal object.

## Tests (TDD anchors)

- `test_omega_bounds` — `JudgeVerdict.omega` rejected outside `[0, 1]` (pydantic validation)
- `test_signal_side_enum_matches_proto` — `SignalSide` values line up with `trade_signal.proto` (BUY/SELL/SELL_SHORT/UNKNOWN)
- `test_abstain_below_threshold` — omega < `OMEGA_THRESHOLD` forces `SignalStatus.SIGNAL_ABSTAIN`
- `test_debate_summary_token_cap` — compression output over 200 tokens raises/truncates, never silently exceeds cap
- `test_graph_wiring_order` — graph visits blue → red → judge → compression in order (mocked LLM clients, no network)
- `test_latency_budget_breach_blue_red` — Blue/Red timeout still yields a forced-completion thesis/challenge, graph does not hang or raise
- `test_latency_budget_breach_judge` — Judge timeout defaults to abstain, not an exception
- `test_reflector_requires_human_gate` — `build_rubric_change_proposal()` output has no `applied`/`deployed` field and cannot mutate live config from this module alone
