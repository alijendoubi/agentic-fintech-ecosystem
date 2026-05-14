# ADR-002: LLM Selection for Cognitive Core Nodes

**Date:** 2026-05-14
**Status:** Accepted
**Deciders:** Ali Jendoubi (Lead)

---

## Context

The Cognitive Core requires LLMs for three roles:
- **Blue Node (Strategist):** Proposes trade thesis with Chain-of-Thought reasoning
- **Red Node (Adversary):** Challenges thesis with historical failure patterns
- **Judge Node (Synthesis):** Weighs debate, computes confidence score ω

Two distinct model families are required to reduce correlated failure modes — if both Blue and Red use the same model, they may share the same reasoning blind spots.

## Decision

| Node | Model | Rationale |
|---|---|---|
| Blue Node | Mistral Large 2 | EU-native, strong CoT, commercial license clear for fintech |
| Red Node | Mistral Large 2 | Same family as Blue — adversarial framing through prompt, not model difference |
| Judge Node | Claude claude-sonnet-4-6 (Anthropic via AWS Bedrock) | Different model family from Mistral; strong synthesis and instruction following |
| Reflector Node | Claude Haiku 4.5 (Anthropic via AWS Bedrock) | Cost-efficient for post-trade analysis; no real-time latency requirement |
| Compression Node | Claude Haiku 4.5 | Cheap summarization; 200-token output target |

**Rejected:** DeepSeek-V3 — Chinese-developed model creates data sovereignty risk in EU/US trading environments and uncertain commercial licensing.

## Deployment

All models accessed via **AWS Bedrock VPC endpoint** — no traffic leaves the VPC boundary.

Zone A (cognitive-core container) → VPC Endpoint → AWS Bedrock → Model response

The VPC endpoint ensures Zone A's "no outbound internet" network policy is respected: Bedrock traffic stays within AWS network fabric.

## Cost Controls

- Max tokens per Blue/Red debate round: 4,096
- Max tokens per Judge synthesis: 2,048
- Compression Node output: strictly ≤200 tokens (enforced in code)
- Total token budget per trade decision: ~15,000 tokens (~$0.06 at Mistral Large 2 pricing)

## Latency Budget Allocation

| Node | Budget | Breach Action |
|---|---|---|
| Blue Node CoT | 800ms | Force completion |
| Red Node challenge | 800ms | Force completion |
| Judge synthesis | 500ms | Default to abstain |
| Compression | 200ms | Skip, use raw summaries |
