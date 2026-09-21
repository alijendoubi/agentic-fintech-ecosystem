-- 003_sharp_audit_reference.sql  --  run as afe_audit_owner AFTER 002_sharp_enforce_transitions.sql.
-- Every transition row must reference the record the gate wrote to the hash-chained audit log (audit.audit_events)
-- for it: audit_seq + audit_hash. A BEFORE INSERT trigger (ENABLE ALWAYS) refuses the row unless that record exists
-- and says exactly what the row says (event type 'sharp.<kind>', same actor, same proposal / version / states /
-- occurred_at / detail). One audit record can back only one row (UNIQUE). afe_sharp.models.fold re-verifies the same
-- binding on every read, so a transition without a matching audit record is neither stored nor believed.
-- If sharp.transitions already holds rows they predate audit references: this migration then fails on the NOT NULL
-- columns and the operator must decide what to do with them (they cannot be trusted as promotions).
\set ON_ERROR_STOP on
SELECT (current_user = 'afe_audit_owner') AS is_owner \gset
\if :is_owner
\else
  \echo ERROR: run this migration as afe_audit_owner
  SELECT 1 / 0;
\endif

BEGIN;

ALTER TABLE sharp.transitions
    ADD COLUMN audit_seq  bigint   NOT NULL,
    ADD COLUMN audit_hash char(64) NOT NULL CHECK (audit_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT sharp_transitions_audit_seq_key UNIQUE (audit_seq);

CREATE FUNCTION sharp.enforce_audit_reference() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, sharp
AS $$
DECLARE
    rec record;
BEGIN
    SELECT a.hash, a.event_type, a.actor, a.payload INTO rec
        FROM audit.audit_events a WHERE a.seq = NEW.audit_seq;
    IF NOT FOUND OR rec.hash <> NEW.audit_hash THEN
        RAISE EXCEPTION 'sharp transition rejected: no audit record % with that hash', NEW.audit_seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF rec.event_type <> 'sharp.' || NEW.kind OR rec.actor <> NEW.actor
       OR (rec.payload ->> 'occurred_at')::timestamptz IS DISTINCT FROM NEW.occurred_at
       OR rec.payload - 'occurred_at' IS DISTINCT FROM jsonb_build_object(
              'proposal_id', NEW.proposal_id, 'version', NEW.version, 'kind', NEW.kind,
              'from_state', NEW.from_state, 'to_state', NEW.to_state, 'detail', NEW.detail)
    THEN
        RAISE EXCEPTION 'sharp transition rejected: audit record % does not match this transition', NEW.audit_seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER sharp_enforce_audit_reference BEFORE INSERT ON sharp.transitions
    FOR EACH ROW EXECUTE FUNCTION sharp.enforce_audit_reference();
ALTER TABLE sharp.transitions ENABLE ALWAYS TRIGGER sharp_enforce_audit_reference;
COMMIT;
