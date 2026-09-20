# sharp-gate (Zone C) — enforced SHARP promotion gate (ALI-56)

Implements `docs/processes/sharp-promotion.md` as a state machine. Nothing here is a verified regulatory-compliance
claim (needs qualified legal review). The gate records and enforces decisions; it does not itself deploy anything.

## States (exactly the doc's steps)

`DRAFT` (step 1, proposal recorded) → `COMPLIANCE` (step 2) → `LEGAL` (step 3) → `BACKTEST` (step 4) → `RISK`
(step 5) → `CANARY` (step 6) → `PROMOTED` (step 7); `REJECTED` from any non-terminal state. A state names the last gate
whose sign-off is recorded; `approve(proposal_id, approver, gate=<next stage>)` moves to `gate`.

## Enforced rules

* No stage skipping or replay (`StageSkipError`); each transition needs a recorded approval.
* The proposer cannot approve (`SelfApprovalError`); an identity may sign at most one stage of a proposal
  (`DistinctApproverError`, my reading of "distinct approver identity" — relax only with owner sign-off). Identities are
  compared NFKC/trim/casefold-normalised.
* Only identities the injected `ApproverAuthorizer` allows for that stage may act (`StaticRoleAuthorizer` takes an
  owner-supplied `stage -> identities` mapping; unlisted stages deny everyone). **No identity is hard-coded.**
  TODO(owner): the real role assignment (who is Compliance Officer / Legal / Risk Manager, and which CI identity signs
  the automated backtest and canary results).
* Rejection at any point needs a reason and an authorized identity for the pending stage; nothing is possible after
  `REJECTED` or `PROMOTED` (`ProposalClosedError`).
* History is append-only; approvals are frozen records folded from immutable events. Corrupt stored history fails
  `fold` with `StoreError`.
* Every transition is written to the audit logger (`AuditSink.record`, satisfied by `afe_audit.AuditLogger`) BEFORE
  it is stored; audit failure => `AuditFailureError`, nothing stored. If the store then fails, a best-effort
  `sharp.transition_aborted` audit record is written and the error re-raised.
* Concurrency: optimistic (`expected_version`); racing approvals yield exactly one winner (`ConcurrencyError`).

## Stores

* `InMemoryProposalStore` (tests / single process).
* `PostgresProposalStore` over its own append-only table `sharp.transitions` (`sql/001_sharp_transitions.sql`, run as
  `afe_audit_owner` after audit-logger's migrations; own schema because `audit`'s DDL is locked). Same pattern as the
  audit table: statement-level triggers reject UPDATE/DELETE/TRUNCATE for every role (ENABLE ALWAYS), runtime role
  `afe_audit_app` has INSERT+SELECT only, `UNIQUE(proposal_id, version)`. Tamper-evidence of content comes from the
  hash-chained audit copy. Not covered: the `sharp` schema has no DDL guard event trigger (TODO: extend 900_ddl_guard).

## Proposal schema (what the Zone A Reflector must supply)

`RubricChangeProposal(proposal_id, proposer_id, description, proposed_change, rationale, evidence_refs: tuple[str, ...])`
(all non-empty, >= 1 evidence ref). `RubricChangeProposal.from_reflector(mapping, proposer_id)` maps today's Zone A model
(`proposal_id`, `trigger_signal_id` -> `signal:<id>` evidence, `observed_underperformance` -> `description`,
`proposed_change`, `rationale`); `proposer_id` is the authenticated identity of the submitting service, supplied by the caller.

## Not implemented

Change Impact Assessment archiving, Legal's "escalate to full conformity assessment" branch, canary metric evaluation
and the automatic canary promotion (the gate only records the signed outcome), notifying the Reflector on rejection.

## Tests

```bash
cd agent-core/zone-c/sharp-gate
python -m pytest tests --cov=afe_sharp   # Postgres-backed runs start postgres:16-alpine via the audit-logger harness
python -m ruff check --config ../../../ruff.toml . && python -m mypy
```
