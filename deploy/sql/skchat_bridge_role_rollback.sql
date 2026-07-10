-- skchat_bridge_role_rollback.sql
-- Reverse deploy/sql/skchat_bridge_role.sql: revoke every privilege and drop the
-- dedicated skchat_bridge role.
--
-- Before running this, cut the live bridge drop-ins back to the shared postgres
-- DSN (or another valid credential) and restart the bridges, otherwise the
-- memory path will fail to authenticate once the role is gone.
--
--   docker exec -i skmem-pg psql -U postgres -d skmemory -f skchat_bridge_role_rollback.sql

\set ON_ERROR_STOP on

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'skchat_bridge') THEN
    RAISE NOTICE 'role skchat_bridge does not exist, nothing to roll back';
    RETURN;
  END IF;

  -- Revoke table + schema + database privileges before DROP so DROP ROLE does
  -- not fail on dependent objects.
  REVOKE ALL PRIVILEGES ON TABLE public.memories, public.docs FROM skchat_bridge;
  REVOKE ALL PRIVILEGES ON SCHEMA public FROM skchat_bridge;
  REVOKE ALL PRIVILEGES ON DATABASE skmemory FROM skchat_bridge;

  DROP ROLE skchat_bridge;
  RAISE NOTICE 'dropped role skchat_bridge';
END
$do$;
