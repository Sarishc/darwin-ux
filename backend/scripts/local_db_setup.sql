-- DarwinUX LOCAL DEVELOPMENT database setup. Run with `make db-setup`.
--
-- Creates the `darwin` role and the darwin_dev / darwin_test databases on the
-- local Homebrew PostgreSQL 17 server. Safe to run repeatedly (idempotent).
-- The password below is a LOCAL ONLY placeholder, not a secret; production
-- credentials live in AWS Secrets Manager.
--
-- Run as your macOS user (the Homebrew superuser), connected to `postgres`.

\set ON_ERROR_STOP on

-- Guard: refuse to touch anything that is not a PostgreSQL 17 server.
SELECT current_setting('server_version_num')::int / 10000 = 17 AS is_pg17 \gset
\if :is_pg17
  \echo 'PostgreSQL 17 detected.'
\else
  \echo 'ERROR: this is not PostgreSQL 17 — refusing to continue. Is 14 or 16 running on this port?'
  \quit 1
\endif

-- Application role: can log in and own its databases; nothing more.
SELECT 'CREATE ROLE darwin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD ''darwin_local_only'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'darwin') \gexec

SELECT 'CREATE DATABASE darwin_dev OWNER darwin'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'darwin_dev') \gexec

SELECT 'CREATE DATABASE darwin_test OWNER darwin'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'darwin_test') \gexec

\echo 'Done: role darwin, databases darwin_dev and darwin_test.'
