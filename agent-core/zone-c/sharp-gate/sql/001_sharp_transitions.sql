-- 001_sharp_transitions.sql  --  run as afe_audit_owner AFTER audit-logger's 000/001 (roles must exist).
-- Own schema `sharp` (not `audit`, whose DDL is locked by audit-logger's 900_ddl_guard.sql). Append-only event log
-- of SHARP proposal transitions. Same immutability pattern as audit.audit_events; tamper-evidence of the content
-- comes from the hash-chained audit log, which receives a copy of every transition.
\set ON_ERROR_STOP on
SELECT (current_user = 'afe_audit_owner') AS is_owner \gset
\if :is_owner
\else
  \echo ERROR: run this migration as afe_audit_owner
  SELECT 1 / 0;
\endif

BEGIN;
CREATE SCHEMA IF NOT EXISTS sharp AUTHORIZATION afe_audit_owner;
REVOKE ALL ON SCHEMA sharp FROM PUBLIC;

CREATE TABLE sharp.transitions (
    id          bigserial   PRIMARY KEY,
    proposal_id text        NOT NULL CHECK (proposal_id <> ''),
    version     integer     NOT NULL CHECK (version >= 1),
    kind        text        NOT NULL CHECK (kind IN ('submitted', 'approved', 'rejected')),
    from_state  text,
    to_state    text        NOT NULL,
    actor       text        NOT NULL CHECK (actor <> ''),
    occurred_at timestamptz NOT NULL,
    detail      jsonb       NOT NULL,
    CONSTRAINT sharp_transitions_version_key UNIQUE (proposal_id, version)
);

CREATE FUNCTION sharp.reject_mutation() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, sharp
AS $$
BEGIN
    RAISE EXCEPTION 'sharp.transitions is append-only: % is forbidden', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER sharp_no_update BEFORE UPDATE ON sharp.transitions
    FOR EACH STATEMENT EXECUTE FUNCTION sharp.reject_mutation();
CREATE TRIGGER sharp_no_delete BEFORE DELETE ON sharp.transitions
    FOR EACH STATEMENT EXECUTE FUNCTION sharp.reject_mutation();
CREATE TRIGGER sharp_no_truncate BEFORE TRUNCATE ON sharp.transitions
    FOR EACH STATEMENT EXECUTE FUNCTION sharp.reject_mutation();
ALTER TABLE sharp.transitions ENABLE ALWAYS TRIGGER sharp_no_update;
ALTER TABLE sharp.transitions ENABLE ALWAYS TRIGGER sharp_no_delete;
ALTER TABLE sharp.transitions ENABLE ALWAYS TRIGGER sharp_no_truncate;

REVOKE ALL ON sharp.transitions FROM PUBLIC;
GRANT USAGE ON SCHEMA sharp TO afe_audit_app;
GRANT SELECT, INSERT ON sharp.transitions TO afe_audit_app;
-- nextval only (USAGE); no UPDATE privilege on the sequence, so setval is not possible.
GRANT USAGE ON SEQUENCE sharp.transitions_id_seq TO afe_audit_app;
COMMIT;
