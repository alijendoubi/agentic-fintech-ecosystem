-- 900_ddl_guard.sql  --  run as a PostgreSQL SUPERUSER after 001/002 (event triggers require superuser).
--
-- Hardening for "even the table owner cannot weaken it": the immutability triggers reject UPDATE/DELETE/TRUNCATE
-- for every role, but the owner could still remove or disable them. These event triggers block any DDL touching the
-- audit schema's objects by a non-superuser. Superusers keep a deliberate break-glass path.
-- To run a future schema migration a superuser must remove event triggers afe_audit_ddl_guard_end/_drop, migrate,
-- and re-run this file. Such a change stays detectable through the hash chain + externally anchored head hashes.
\set ON_ERROR_STOP on
SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super \gset
\if :is_super
\else
  \echo ERROR: run this script as a superuser
  SELECT 1 / 0;
\endif

CREATE OR REPLACE FUNCTION public.afe_audit_ddl_guard_end() RETURNS event_trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    r record;
BEGIN
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RETURN;
    END IF;
    FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        IF r.schema_name = 'audit'
           AND r.command_tag IN ('ALTER TABLE', 'ALTER FUNCTION', 'CREATE FUNCTION', 'ALTER TRIGGER', 'ALTER SCHEMA')
        THEN
            RAISE EXCEPTION 'audit schema is DDL-locked: % on % is forbidden for non-superusers',
                r.command_tag, r.object_identity USING ERRCODE = 'insufficient_privilege';
        END IF;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.afe_audit_ddl_guard_drop() RETURNS event_trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    r record;
BEGIN
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RETURN;
    END IF;
    FOR r IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
        IF r.schema_name = 'audit' OR r.object_identity LIKE '%audit.audit_events%' OR r.object_identity = 'audit' THEN
            RAISE EXCEPTION 'audit schema is DDL-locked: dropping % is forbidden for non-superusers', r.object_identity
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END LOOP;
END;
$$;

DROP EVENT TRIGGER IF EXISTS afe_audit_ddl_guard_end;
DROP EVENT TRIGGER IF EXISTS afe_audit_ddl_guard_drop;
CREATE EVENT TRIGGER afe_audit_ddl_guard_end ON ddl_command_end
    EXECUTE FUNCTION public.afe_audit_ddl_guard_end();
CREATE EVENT TRIGGER afe_audit_ddl_guard_drop ON sql_drop
    EXECUTE FUNCTION public.afe_audit_ddl_guard_drop();
