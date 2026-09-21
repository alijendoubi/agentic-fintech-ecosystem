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
* History is append-only; approvals are frozen records folded from immutable events. `fold` re-verifies, on every read
  (`SharpGate.get`), the same invariants the database enforces at insert time (sequential versions, legal transitions,
  no stage skips, submitter is the proposer and never approves, one stage per approver, rejection needs a reason) and
  that every transition is backed by a matching audit record. Any inconsistency raises `StoreError`: an invalid
  history is never reported as advanced or `PROMOTED` (fail closed). `SharpGate` therefore needs an `audit_lookup`
  (`PostgresAuditLookup` reads `audit.audit_events`).
* Every transition is written to the audit logger (`AuditSink.record`, satisfied by `afe_audit.AuditLogger`) BEFORE
  it is stored, and the stored row carries the audit record's `seq`/`hash`; audit failure => `AuditFailureError`,
  nothing stored. Audit log and store are two transactions, so the pair is not atomic; instead the gate compensates.
  On ANY failure of the store step it first checks whether the row landed anyway (lost commit acknowledgement: then
  the transition happened and is returned), otherwise writes an explicit `sharp.transition_aborted` audit record
  (orphaned `audit_seq`/`audit_hash`, error class, `store_outcome` = `absent` | `unverified`) and re-raises the
  original error. If that record cannot be written either, `AbortNotRecordedError` (an `AuditFailureError`, chained
  from the original error, carrying `audit_seq`) is raised instead: nothing is swallowed, and an operator must
  reconcile the orphaned audit record. An audit record with no store row always means "not performed".
* Concurrency: optimistic (`expected_version`); racing approvals yield exactly one winner (`ConcurrencyError`).

## Stores

* `InMemoryProposalStore` (tests / single process).
* `PostgresProposalStore` over its own append-only table `sharp.transitions` (own schema because `audit`'s DDL is
  locked). Migrations, in order: `001_sharp_transitions.sql`, `002_sharp_enforce_transitions.sql`,
  `003_sharp_audit_reference.sql` (all as `afe_audit_owner`, after audit-logger's migrations), then
  `900_sharp_ddl_guard.sql` (as a superuser, LAST: once installed the owner can no longer change schema `sharp`).
  `sharp.transitions` must be empty when 003 is applied (rows without audit references cannot be trusted).
  All triggers are `ENABLE ALWAYS` (they fire even under `session_replication_role = replica`):
  * statement-level triggers reject UPDATE/DELETE/TRUNCATE for every role; the runtime role `afe_audit_app` has
    INSERT+SELECT only; `UNIQUE(proposal_id, version)`;
  * a BEFORE INSERT state-machine trigger enforces, for every role including the owner and superusers, the rules
    listed under "Enforced rules" that are database-checkable: version sequence, legal transition from the previous
    row, one-stage approvals (so `PROMOTED` needs all six), submitter != approver, distinct approvers (NFKC/trim/lower
    in SQL; `fold` uses casefold and is the stricter, authoritative check), terminal states, rejection reason;
  * a BEFORE INSERT audit-reference trigger refuses a row unless `audit.audit_events` holds the record it names, with
    the same event type, actor, timestamp and full payload; each audit record can back only one row;
  * `900_sharp_ddl_guard.sql` event triggers refuse any DDL on schema `sharp` by non-superusers (disable/replace/drop
    triggers, rules, rename or drop the schema). Break-glass is superuser-only (same procedure as audit-logger's guard).
  Tamper-evidence of the content otherwise comes from the hash-chained audit copy.

## Limits (what this does NOT protect against)

* `actor` and `occurred_at` are client-asserted: the database checks that they are consistent with the audit record,
  but the audit record is also written by the client. A holder of the app role that can write the audit chain can
  still forge a fully self-consistent history using ANY distinct, non-proposer identity names; it will be a real,
  permanent, hash-chained entry (detectable by review, not preventable here).
* Approver AUTHORIZATION (who may sign which stage) is enforced only by the injected `ApproverAuthorizer` in the
  gate, not in the database or in `fold`: identities can rotate, and the authorization decision is not recorded or
  signed. Closing this needs signed approvals or a recorded role snapshot per transition (TODO(owner)).
* The audit log is trusted as a source: `fold` checks that the referenced record exists and matches, not the chain's
  integrity (run `ChainVerifier` / anchors for that). A superuser can still remove the guard or disable triggers
  (deliberate break-glass); that is only detectable afterwards.
* An audit record the gate wrote for a transition that then failed to store (see `sharp.transition_aborted`) could be
  replayed as a row by a compromised app role, but only with exactly the content the gate had already validated, and
  only where it is still a legal next transition.
* If the store outcome cannot be verified after a failure (`store_outcome: unverified`) the abort record states that
  it is unverified; it is not a proof of "not performed".
* The `sharp` migration files are not wired into `infrastructure/docker-compose.yml` / init scripts (only audit-logger's
  `sql/` is mounted there); whoever deploys `sharp.transitions` must run them in the order above.

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
