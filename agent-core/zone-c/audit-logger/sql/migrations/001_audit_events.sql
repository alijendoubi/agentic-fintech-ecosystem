-- 001_audit_events.sql  --  run as afe_audit_owner (NOT a superuser).
-- Append-only, hash-chained audit table. See ../../README.md for the canonical serialisation rules.
\set ON_ERROR_STOP on
SELECT (current_user = 'afe_audit_owner') AS is_owner \gset
\if :is_owner
\else
  \echo ERROR: run this migration as afe_audit_owner
  SELECT 1 / 0;
\endif

BEGIN;

CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION afe_audit_owner;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

-- seq is declared bigserial per the design, but the writer always supplies seq = head + 1 under the chain lock and
-- the insert trigger REJECTS anything else: the chain is gapless and seq is part of the hash preimage.
CREATE TABLE audit.audit_events (
    seq         bigserial   PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    event_type  text        NOT NULL CHECK (event_type <> ''),
    actor       text        NOT NULL CHECK (actor <> ''),
    payload     jsonb       NOT NULL,
    canonical   text        NOT NULL,   -- exact bytes (as text) that were hashed
    prev_hash   char(64)    NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash        char(64)    NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT audit_events_hash_key UNIQUE (hash),
    CONSTRAINT audit_events_prev_hash_key UNIQUE (prev_hash)   -- a second row may never extend the same parent (no forks)
);

CREATE FUNCTION audit.genesis_hash() RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT repeat('0', 64) $$;

CREATE FUNCTION audit.chain_lock_key() RETURNS bigint
LANGUAGE sql IMMUTABLE AS $$ SELECT hashtextextended('afe.audit.chain', 0) $$;

-- Returns NULL when the row is internally consistent and correctly linked to expected_prev_hash,
-- otherwise a short defect code. The same checks are implemented independently in Python (afe_audit.verifier).
CREATE FUNCTION audit.row_defect(rec audit.audit_events, expected_prev_hash text) RETURNS text
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, audit
AS $$
DECLARE
    c jsonb;
BEGIN
    IF rec.hash <> encode(sha256(convert_to(rec.canonical, 'UTF8')), 'hex') THEN
        RETURN 'hash_mismatch';
    END IF;
    BEGIN
        c := rec.canonical::jsonb;
    EXCEPTION WHEN others THEN
        RETURN 'canonical_unparseable';
    END;
    IF jsonb_typeof(c) IS DISTINCT FROM 'object' THEN
        RETURN 'canonical_unparseable';
    END IF;
    IF rec.prev_hash <> expected_prev_hash THEN
        RETURN 'prev_hash_link_broken';
    END IF;
    IF c ->> 'prev_hash' IS DISTINCT FROM rec.prev_hash THEN
        RETURN 'canonical_prev_hash_mismatch';
    END IF;
    IF c ->> 'seq' IS DISTINCT FROM rec.seq::text THEN
        RETURN 'seq_mismatch';
    END IF;
    IF c ->> 'event_type' IS DISTINCT FROM rec.event_type THEN
        RETURN 'event_type_mismatch';
    END IF;
    IF c ->> 'actor' IS DISTINCT FROM rec.actor THEN
        RETURN 'actor_mismatch';
    END IF;
    IF c -> 'payload' IS DISTINCT FROM rec.payload THEN
        RETURN 'payload_mismatch';
    END IF;
    BEGIN
        IF (c ->> 'occurred_at')::timestamptz IS DISTINCT FROM rec.occurred_at THEN
            RETURN 'timestamp_mismatch';
        END IF;
    EXCEPTION WHEN others THEN
        RETURN 'timestamp_mismatch';
    END;
    RETURN NULL;
END;
$$;

-- Serialises writers and validates every insert against the current head. Even a compromised app role
-- cannot insert a row that is not the exact, correctly hashed successor of the current head.
CREATE FUNCTION audit.enforce_chain_on_insert() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, audit
AS $$
DECLARE
    head_seq  bigint;
    head_hash text;
    defect    text;
BEGIN
    PERFORM pg_advisory_xact_lock(audit.chain_lock_key());
    SELECT e.seq, e.hash INTO head_seq, head_hash FROM audit.audit_events e ORDER BY e.seq DESC LIMIT 1;
    IF NOT FOUND THEN
        head_seq := 0;
        head_hash := audit.genesis_hash();
    END IF;
    IF NEW.seq IS DISTINCT FROM head_seq + 1 THEN
        RAISE EXCEPTION 'audit chain: expected seq %, got %', head_seq + 1, NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    defect := audit.row_defect(NEW, head_hash);
    IF defect IS NOT NULL THEN
        RAISE EXCEPTION 'audit chain: rejected insert (%)', defect
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION audit.reject_mutation() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, audit
AS $$
BEGIN
    RAISE EXCEPTION 'audit.audit_events is append-only: % is forbidden', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER audit_events_chain_insert
    BEFORE INSERT ON audit.audit_events
    FOR EACH ROW EXECUTE FUNCTION audit.enforce_chain_on_insert();

-- Statement-level so that even a no-op UPDATE/DELETE (matching zero rows) is rejected.
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON audit.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON audit.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();

-- ENABLE ALWAYS: fire even when session_replication_role = replica (a common way to bypass triggers).
ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER audit_events_no_update;
ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER audit_events_no_delete;
ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER audit_events_no_truncate;

-- Runtime role: SELECT + INSERT only. No sequence privileges (the writer supplies seq explicitly).
REVOKE ALL ON audit.audit_events FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO afe_audit_app;
GRANT SELECT, INSERT ON audit.audit_events TO afe_audit_app;

COMMIT;
