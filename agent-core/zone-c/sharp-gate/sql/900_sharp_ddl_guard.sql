-- 900_sharp_ddl_guard.sql  --  run as a PostgreSQL SUPERUSER after 001..003 (event triggers require superuser).
--
-- audit-logger's 900_ddl_guard.sql only locks schema 'audit'. Without this script the owner role of schema 'sharp'
-- (afe_audit_owner) could still DISABLE / replace / drop the append-only and state-machine triggers on
-- sharp.transitions, which would reopen the forged-promotion hole. These event triggers refuse ANY DDL touching schema
-- 'sharp' (or an object attached to it, e.g. a trigger or rule on sharp.transitions) for non-superusers.
-- Break-glass is deliberate and superuser-only: to migrate, a superuser removes event triggers afe_sharp_ddl_guard_end
-- and afe_sharp_ddl_guard_drop, migrates, and re-runs this file. Because every transition also lives in the
-- hash-chained audit log, such a change stays detectable.
-- ORDER: run it AFTER the sharp migrations (once installed, the owner can no longer create objects in 'sharp').
\set ON_ERROR_STOP on
SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super \gset
\if :is_super
\else
  \echo ERROR: run this script as a superuser
  SELECT 1 / 0;
\endif

CREATE OR REPLACE FUNCTION public.afe_sharp_ddl_guard_end() RETURNS event_trigger
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
        -- ALTER SCHEMA reports the NEW name (a rename would slip past the name match), and the only schemas the
        -- owner role holds are the locked ones, so every ALTER SCHEMA is refused.
        IF r.schema_name = 'sharp' OR r.object_identity ~ '(^|[ ])sharp(\.|$)'
           OR r.command_tag = 'ALTER SCHEMA' THEN
            RAISE EXCEPTION 'sharp schema is DDL-locked: % on % is forbidden for non-superusers',
                r.command_tag, r.object_identity USING ERRCODE = 'insufficient_privilege';
        END IF;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.afe_sharp_ddl_guard_drop() RETURNS event_trigger
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
        IF r.schema_name = 'sharp' OR r.object_identity ~ '(^|[ ])sharp(\.|$)' THEN
            RAISE EXCEPTION 'sharp schema is DDL-locked: dropping % is forbidden for non-superusers',
                r.object_identity USING ERRCODE = 'insufficient_privilege';
        END IF;
    END LOOP;
END;
$$;

DROP EVENT TRIGGER IF EXISTS afe_sharp_ddl_guard_end;
DROP EVENT TRIGGER IF EXISTS afe_sharp_ddl_guard_drop;
CREATE EVENT TRIGGER afe_sharp_ddl_guard_end ON ddl_command_end
    EXECUTE FUNCTION public.afe_sharp_ddl_guard_end();
CREATE EVENT TRIGGER afe_sharp_ddl_guard_drop ON sql_drop
    EXECUTE FUNCTION public.afe_sharp_ddl_guard_drop();
