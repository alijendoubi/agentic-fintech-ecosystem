# ADR-003: How Zone A Reaches LLM Providers (and what "Zone A holds zero API keys" means)

**Date:** 2026-09-19
**Status:** Proposed (NOT accepted; drafted for the owner to decide)
**Deciders:** Ali Jendoubi (Lead) — decision pending
**Tracks:** ALI-41
**Related:** ADR-001, ADR-002 (Accepted; see the status note appended to ADR-002)

> This ADR describes options and a PROPOSED recommendation. Statements about data protection, contracts or regulation **require qualified legal review**. Vendor facts not visible in the repo are marked TODO(owner).

---

## Context: the repo contradicts itself

| Source | What it says |
|---|---|
| `agent-core/README.md`, `CLAUDE.md`, ADR-001 | Zone A holds **zero** API keys; only Aegis signs |
| ADR-002 | All models are reached through an **AWS Bedrock VPC endpoint**; no traffic leaves the VPC; Zone A has "no outbound internet" |
| `infrastructure/docker-compose.yml`, `cognitive-core` service | Injects `MISTRAL_API_KEY` and `ANTHROPIC_API_KEY` into the container |
| `zone-a/cognitive-core/llm_clients.py` | Uses `langchain_mistralai.ChatMistralAI` and `langchain_anthropic.ChatAnthropic`, i.e. the providers' **direct** APIs, which read those keys from the environment. Its docstring claims "no raw keys live in this module" and that Bedrock or Mistral's EU endpoint is used |
| `zone-a/cognitive-core/config.py` | Judge/compression/reflector model ids look like `anthropic.claude-sonnet-4-6-bedrock`, which are not identifiers the direct Anthropic client would accept as written (TODO(owner): confirm the real ids for whichever route is chosen) |
| `docs/regulatory/third-party-ict-register.md` | TP-003 Anthropic "via AWS Bedrock"; TP-004 Mistral via a **direct** "Commercial API License" (contradicts ADR-002's "all via Bedrock") |
| Compose networks | `cognitive-core` is attached only to `zone-a-internal` and `zone-ab-grpc`, both `internal: true`. As written it has **no route to any external API** at all, so neither the Bedrock route nor the direct-API route can work in this compose file |

Two separate questions are tangled:
1. **What the "zero keys" rule is protecting.** Its security value is that Zone A, which processes LLM output and is exposed to prompt injection, can never authorise or sign a trade. LLM-provider keys are a different class: a leaked LLM key costs money and leaks prompts, it cannot place an order.
2. **Where LLM traffic leaves the system** and how it is authenticated.

Also open: whether "Mistral Large 2" and the named Claude models are offered on Bedrock in the chosen region (TODO(owner): verify; the third-party register treats Mistral as a direct API customer).

## Options

### Option 1: Bedrock for all models via VPC endpoint with an IAM role (ADR-002 as written)

- Zone A authenticates with an IAM role (no static provider keys); traffic stays on AWS private networking.
- Requires the runtime to be **on AWS** (ECS/EKS/EC2). A laptop/Docker Compose dev environment has no VPC endpoint; developers would need static AWS credentials or a different dev path.
- Requires model availability on Bedrock for every node (TODO(owner): verify Mistral Large 2 and the Claude versions in the intended region), and changes to `llm_clients.py` (e.g. Bedrock client classes instead of `ChatMistralAI`/`ChatAnthropic`) and model ids.
- Keeps the third-party register simple (AWS as the single LLM subprocessor plus model vendors), but TP-004 must be rewritten.

Compose/network implications: compose cannot express a VPC endpoint. For the compose (dev) stack:
```yaml
# cognitive-core: remove provider keys, add region/role config
environment:
  - AWS_REGION=${AWS_REGION}
  # no MISTRAL_API_KEY / ANTHROPIC_API_KEY
# add an egress network that is NOT internal, restricted at the host firewall to the Bedrock endpoint CIDR/FQDN only
networks:
  zone-a-internal: {}
  zone-ab-grpc: {}
  zone-a-llm-egress: {}      # new, internal: false; host firewall allows only the Bedrock endpoint
```
In production the equivalent is an interface VPC endpoint plus security-group egress rules (IaC, `phase_5_canary_production.md` §6).

### Option 2: Key-holding LLM gateway (egress proxy) outside Zone A

- Provider keys live only in a small gateway service. Zone A calls the gateway with no provider key (at most a short-lived internal token). The gateway forwards to the provider's API with an FQDN allow-list, logs usage and enforces token budgets (ADR-002 cost controls become enforceable centrally).
- Keeps the "zero keys in Zone A" statement literally true for provider keys, works in Docker Compose, and needs only base-URL configuration in the SDKs (`ChatMistralAI` and `ChatAnthropic` accept custom endpoints; dummy key strings satisfy the SDK; TODO(owner): verify against the pinned versions).
- Adds a component (an off-the-shelf LLM proxy such as LiteLLM, or a small allow-listing reverse proxy; evaluation is TODO(owner)) and a new critical dependency for the trading path (add to the DORA asset inventory).
- Prompts still go to Mistral/Anthropic direct APIs: data-residency, retention and DPA questions for TP-003/TP-004 **require qualified legal review**.

Compose changes:
```yaml
networks:
  zone-a-llm:            # shared only by cognitive-core and llm-gateway
    internal: true
  llm-egress:            # gateway -> internet, restricted by host firewall to provider FQDNs
services:
  llm-gateway:           # new service (image/build TODO)
    environment:
      - MISTRAL_API_KEY=${MISTRAL_API_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    networks: [zone-a-llm, llm-egress]
  cognitive-core:
    environment:
      - MISTRAL_API_BASE=http://llm-gateway:8081/mistral       # variable names depend on SDK
      - ANTHROPIC_BASE_URL=http://llm-gateway:8081/anthropic
      - MISTRAL_API_KEY=unused-placeholder
      - ANTHROPIC_API_KEY=unused-placeholder
    networks: [zone-a-internal, zone-ab-grpc, zone-a-llm]      # no llm-egress
```

### Option 3: Keep key injection; redefine the rule and open egress narrowly

- Change the documented principle to "Zone A holds no **trading, signing or broker** credentials; LLM-provider keys are permitted, scoped, budget-capped and rotated". Keep the keys in `cognitive-core`; give it egress to provider FQDNs only (forward proxy or firewall allow-list).
- Smallest change; matches the code as written; loses the literal "zero keys" property, and a compromised Zone A could exfiltrate LLM keys and prompts.

Compose changes: add a non-internal network `zone-a-llm-egress` to `cognitive-core` with FQDN filtering outside compose (compose cannot filter by FQDN); update README/CLAUDE.md/ADR-001 wording.

### Option 4: Status quo

Not viable: the container has keys but no route out, and the documents contradict the config. Listed only to state that doing nothing leaves the contradiction and a non-working network topology.

## PROPOSED recommendation (owner to confirm)

**Option 2 for development, canary and production while the runtime is Docker-based**, because it preserves the "no provider keys in Zone A" property, works without an AWS-only runtime, and centralises budgets and logging. **Option 1 remains valid if the owner chooses to run in AWS** and confirms model availability; it should then replace Option 2 with no change to Zone A beyond the client library. **Option 3** is the fallback if the added gateway component is judged not worth its operational risk; in that case the documents must say plainly that Zone A holds LLM keys.

Independent of the option chosen:
1. Fix documentation to match the decision (README, CLAUDE.md, ADR-002 status note, third-party register TP-003/TP-004, RTS6 template §5).
2. Fix the compose network so `cognitive-core` has exactly one intended route to its LLM endpoint and none other.
3. The LLM path is not on the Aegis critical path (a Zone A outage results in no signal, hence no trade; DORA doc §3.1 "fallback to abstain").

## Consequences

- Positive: a single, true statement about keys in Zone A; enforceable egress; central token budgeting (Option 2).
- Negative: new component and dependency (Option 2), AWS lock-in and dev-experience cost (Option 1), weaker isolation claim (Option 3).
- The decision changes what the third-party register and outsourcing tables (RTS6 §5, DORA §6) must list; any change requires re-review of contracts and data-processing terms (**requires qualified legal review**).
- Code changes belong to the cognitive-core and compose owners; this ADR only specifies them.

## Open items for the owner

- [ ] Choose an option and set Status accordingly.
- [ ] TODO(owner): confirm model availability/ids for the chosen route and region.
- [ ] TODO(owner): confirm what data the prompts contain (market data licensing terms may restrict sending vendor data to third parties; see register TP-005 "redistribution restrictions").
