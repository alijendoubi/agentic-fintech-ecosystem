-- 000_bootstrap_roles.sql  --  run ONCE as a PostgreSQL superuser (psql 15+, needs \getenv).
--
-- Creates the two non-superuser roles that separate schema ownership from runtime use:
--   afe_audit_owner : owns the audit schema/objects, used ONLY to run migrations (never by services)
--   afe_audit_app   : runtime role used by the services. INSERT + SELECT only. No DDL, no UPDATE/DELETE/TRUNCATE.
-- Passwords are read from the environment so they never appear on a command line:
--   AFE_AUDIT_OWNER_PASSWORD, AFE_AUDIT_APP_PASSWORD (>= 16 chars each, and different from each other).
\set ON_ERROR_STOP on
\getenv owner_password AFE_AUDIT_OWNER_PASSWORD
\getenv app_password AFE_AUDIT_APP_PASSWORD

SELECT CASE WHEN length(:'owner_password') >= 16 AND length(:'app_password') >= 16
                 AND :'owner_password' <> :'app_password'
            THEN 'ok' ELSE 'bad' END AS pw_state \gset
\echo Checking role passwords (pw_state = :pw_state)
-- Division by zero aborts the script (ON_ERROR_STOP) when the password policy is not met.
SELECT 1 / (:'pw_state' = 'ok')::int AS password_policy_satisfied;

SELECT 'CREATE ROLE afe_audit_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'afe_audit_owner') \gexec
SELECT 'CREATE ROLE afe_audit_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'afe_audit_app') \gexec
SELECT format('ALTER ROLE afe_audit_owner PASSWORD %L', :'owner_password') \gexec
SELECT format('ALTER ROLE afe_audit_app PASSWORD %L', :'app_password') \gexec

-- Database-level privileges: only the two roles (and superusers) can connect; only the owner may create schemas.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO afe_audit_owner, afe_audit_app', current_database()) \gexec
SELECT format('GRANT CREATE ON DATABASE %I TO afe_audit_owner', current_database()) \gexec
-- No role may create objects in public (defence in depth; PG15+ already defaults to this).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
