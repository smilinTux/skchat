-- skchat_bridge_role.sql
-- Dedicated, least-privilege Postgres LOGIN role for the skchat Telegram
-- bridges' memory path. It replaces the shared `postgres` superuser DSN that is
-- currently inlined in the live drop-ins
-- (skchat-telegram-opus.service.d/override.conf and the lumina twin).
--
-- WHY A SCOPED ROLE INSTEAD OF ROTATING THE SHARED PASSWORD:
--   postgres://postgres:...@localhost:5432/skmemory is the shared superuser that
--   skmemory and skingest also use. Rotating it would ripple across unrelated
--   services. Instead the bridges get their own credential with exactly the
--   privileges their memory path needs and nothing more.
--
-- WHAT THE BRIDGE MEMORY PATH ACTUALLY DOES
-- (skmemory/skmemory/backends/pgvector_backend.py, database `skmemory`):
--   * memories table: SELECT / INSERT / UPDATE / DELETE
--       - save():  INSERT ... ON CONFLICT (id) DO UPDATE   (INSERT + UPDATE)
--       - load(), list_memories(), search*(), find_similar(), health_check(): SELECT
--       - delete(): DELETE
--   * docs table: SELECT only
--       - hybrid_search_docs() RAG grounding is read-only; docs rows are written
--         by skingest, never by the bridge.
--   memories.id is application-supplied (no serial / no column default) so NO
--   sequence USAGE is required. docs.id uses docs_id_seq, but the bridge never
--   INSERTs into docs, so that sequence is deliberately omitted. The
--   file_locations table is untouched by the bridge and is omitted entirely.
--   hybrid_search_memories/hybrid_search_docs are plain SQL functions (EXECUTE is
--   granted to PUBLIC by default and they run with the caller's privileges), so
--   the table grants above are sufficient. No extension-level grants are needed:
--   the vector / tsvector operators the queries use are available to PUBLIC.
--
-- IDEMPOTENT + PARAMETERISED PASSWORD:
--   The password is passed as a psql variable so it never lives in this file.
--     docker exec -i skmem-pg psql -U postgres -d skmemory \
--       -v bridge_pw="$(read -rs P; echo "$P")" -f skchat_bridge_role.sql
--   or, non-interactively from the provisioning tooling:
--     psql "postgresql://postgres:...@localhost:5432/skmemory" \
--       -v bridge_pw="<STRONG_PASSWORD>" -f skchat_bridge_role.sql
--   The value is substituted with psql's :'bridge_pw' quoting, which safely
--   emits a single-quoted SQL string literal (handles embedded quotes).
--
-- Rollback: deploy/sql/skchat_bridge_role_rollback.sql

\set ON_ERROR_STOP on

-- Guard: refuse to run without a password variable (avoids creating a
-- passwordless login role by accident).
SELECT CASE
  WHEN :'bridge_pw' = '' THEN
    (SELECT 1 / (0 * length('set -v bridge_pw=<STRONG_PASSWORD> before running this file')))
  ELSE 1 END AS bridge_pw_required;

-- Create the role once (LOGIN, no password yet), then always (re)set its
-- attributes + password. Splitting it this way keeps the CREATE idempotent while
-- letting the same file rotate the password on a re-run.
DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'skchat_bridge') THEN
    CREATE ROLE skchat_bridge LOGIN;
    RAISE NOTICE 'created role skchat_bridge';
  ELSE
    RAISE NOTICE 'role skchat_bridge already exists (will update attributes + password)';
  END IF;
END
$do$;

-- Explicitly strip every elevated attribute (fail-closed least privilege) and
-- set the password from the psql variable.
ALTER ROLE skchat_bridge
  WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'bridge_pw';

-- Connect + schema visibility.
GRANT CONNECT ON DATABASE skmemory TO skchat_bridge;
GRANT USAGE ON SCHEMA public TO skchat_bridge;

-- Exactly the DML the bridge memory path needs.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.memories TO skchat_bridge;
GRANT SELECT ON TABLE public.docs TO skchat_bridge;

-- Verify the grant set (visible in psql output; no secret material).
SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privs
FROM information_schema.role_table_grants
WHERE grantee = 'skchat_bridge'
GROUP BY table_name
ORDER BY table_name;
