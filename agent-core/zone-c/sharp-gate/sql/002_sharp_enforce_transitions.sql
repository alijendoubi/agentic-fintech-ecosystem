-- 002_sharp_enforce_transitions.sql  --  run as afe_audit_owner AFTER 001_sharp_transitions.sql.
-- Enforces the SHARP state machine at INSERT time, in the database, so that a compromised holder of the runtime role
-- (afe_audit_app has INSERT) cannot store a forged history that reads as PROMOTED, e.g. submitted + approved +
-- approved rows with arbitrary actors. The same invariants are re-verified on read by afe_sharp.models.fold.
--
-- Enforced here: valid kind / stage names; sequential version from 1; the first row is a 'submitted' that creates DRAFT
-- and whose actor is the proposer named in its detail; every later row starts from the previous row's state; nothing
-- follows PROMOTED / REJECTED; an approval moves exactly one stage forward (so PROMOTED needs all six approvals);
-- the approver is not the submitter and has not approved another stage of the same proposal; a rejection goes to
-- REJECTED and carries a reason.
-- NOT enforced here: which identities are AUTHORIZED per stage (an application-side, configurable role mapping) and
-- the truth of actor / occurred_at (client-asserted). See README "Limits".
\set ON_ERROR_STOP on
SELECT (current_user = 'afe_audit_owner') AS is_owner \gset
\if :is_owner
\else
  \echo ERROR: run this migration as afe_audit_owner
  SELECT 1 / 0;
\endif

BEGIN;

ALTER TABLE sharp.transitions ADD CONSTRAINT sharp_transitions_states_check CHECK (
    to_state IN ('DRAFT', 'COMPLIANCE', 'LEGAL', 'BACKTEST', 'RISK', 'CANARY', 'PROMOTED', 'REJECTED')
    AND (from_state IS NULL
         OR from_state IN ('DRAFT', 'COMPLIANCE', 'LEGAL', 'BACKTEST', 'RISK', 'CANARY', 'PROMOTED', 'REJECTED'))
);

-- Comparison form of an identity (NFKC, trimmed, lower-cased). Python's fold uses casefold(), which folds a few more
-- characters than lower(); the SQL form is never STRICTER than fold, so fold stays the authoritative check.
CREATE FUNCTION sharp.identity_key(identity text) RETURNS text
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$ SELECT lower(regexp_replace(normalize(identity, NFKC), '^\s+|\s+$', '', 'g')) $$;

-- Position in the promotion pipeline; NULL for REJECTED / unknown.
CREATE FUNCTION sharp.stage_rank(stage text) RETURNS integer
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT CASE stage
        WHEN 'DRAFT' THEN 0 WHEN 'COMPLIANCE' THEN 1 WHEN 'LEGAL' THEN 2 WHEN 'BACKTEST' THEN 3
        WHEN 'RISK' THEN 4 WHEN 'CANARY' THEN 5 WHEN 'PROMOTED' THEN 6
    END
$$;

CREATE FUNCTION sharp.enforce_transition() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, sharp
AS $$
DECLARE
    prev      sharp.transitions;
    submitter text;
BEGIN
    -- Same key the Python store uses; serialises concurrent writers of one proposal.
    PERFORM pg_advisory_xact_lock(hashtext(NEW.proposal_id));
    SELECT t.* INTO prev FROM sharp.transitions t
        WHERE t.proposal_id = NEW.proposal_id ORDER BY t.version DESC LIMIT 1;

    IF NOT FOUND THEN
        IF NEW.version <> 1 OR NEW.kind <> 'submitted' OR NEW.from_state IS NOT NULL
           OR NEW.to_state <> 'DRAFT' THEN
            RAISE EXCEPTION 'sharp transition rejected: first row must be version 1, submitted, creating DRAFT'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF jsonb_typeof(NEW.detail) IS DISTINCT FROM 'object'
           OR NEW.detail ->> 'proposal_id' IS DISTINCT FROM NEW.proposal_id
           OR NEW.detail ->> 'proposer_id' IS DISTINCT FROM NEW.actor THEN
            RAISE EXCEPTION 'sharp transition rejected: submitted row must name this proposal and its proposer as actor'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.version <> prev.version + 1 THEN
        RAISE EXCEPTION 'sharp transition rejected: expected version %, got %', prev.version + 1, NEW.version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF prev.to_state IN ('PROMOTED', 'REJECTED') THEN
        RAISE EXCEPTION 'sharp transition rejected: proposal is already %', prev.to_state
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.from_state IS DISTINCT FROM prev.to_state THEN
        RAISE EXCEPTION 'sharp transition rejected: from_state % does not match current state %',
            NEW.from_state, prev.to_state USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.kind = 'approved' THEN
        IF sharp.stage_rank(NEW.to_state) IS DISTINCT FROM sharp.stage_rank(NEW.from_state) + 1 THEN
            RAISE EXCEPTION 'sharp transition rejected: % -> % skips or reverses a stage', NEW.from_state, NEW.to_state
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT t.actor INTO submitter FROM sharp.transitions t
            WHERE t.proposal_id = NEW.proposal_id AND t.version = 1;
        IF sharp.identity_key(NEW.actor) = sharp.identity_key(submitter) THEN
            RAISE EXCEPTION 'sharp transition rejected: the submitter cannot approve their own proposal'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM sharp.transitions t
                   WHERE t.proposal_id = NEW.proposal_id AND t.kind = 'approved'
                     AND sharp.identity_key(t.actor) = sharp.identity_key(NEW.actor)) THEN
            RAISE EXCEPTION 'sharp transition rejected: this identity already approved another stage'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF NEW.kind = 'rejected' THEN
        IF NEW.to_state <> 'REJECTED' THEN
            RAISE EXCEPTION 'sharp transition rejected: a rejection must end in REJECTED'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF jsonb_typeof(NEW.detail -> 'reason') IS DISTINCT FROM 'string'
           OR btrim(NEW.detail ->> 'reason') = '' THEN
            RAISE EXCEPTION 'sharp transition rejected: a rejection needs a reason'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSE
        RAISE EXCEPTION 'sharp transition rejected: kind % is only valid as the first row', NEW.kind
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER sharp_enforce_transition BEFORE INSERT ON sharp.transitions
    FOR EACH ROW EXECUTE FUNCTION sharp.enforce_transition();
-- ENABLE ALWAYS: fires even under session_replication_role = replica (the usual trigger bypass).
ALTER TABLE sharp.transitions ENABLE ALWAYS TRIGGER sharp_enforce_transition;
COMMIT;
