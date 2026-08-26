#!/bin/bash
# Runs once, on first container init, against the bootstrap superuser.
# Creates the two app roles the project connects as. app_owner is used for
# migrations/seeding (Phase 0.2+); app_readonly is the only role generation/
# and clarify/ code are allowed to use (see src/t2sql/db/connection.py).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE app_owner LOGIN PASSWORD '$APP_OWNER_PASSWORD';
    GRANT ALL PRIVILEGES ON DATABASE "$POSTGRES_DB" TO app_owner;
    ALTER DATABASE "$POSTGRES_DB" OWNER TO app_owner;
    GRANT ALL ON SCHEMA public TO app_owner;

    -- SELECT-only, short statement timeout. No CREATE on public, no
    -- superuser/replication bits, so pg_catalog write paths are unreachable
    -- (Postgres denies catalog writes to non-superusers regardless).
    CREATE ROLE app_readonly LOGIN PASSWORD '$APP_READONLY_PASSWORD';
    ALTER ROLE app_readonly SET statement_timeout = '5000';
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO app_readonly;
    GRANT USAGE ON SCHEMA public TO app_readonly;
    ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public GRANT SELECT ON TABLES TO app_readonly;
EOSQL
