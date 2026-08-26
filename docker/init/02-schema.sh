#!/bin/bash
# Applies the schema as app_owner (created by 01-roles.sh) so app_owner ends up
# owning every table -- this is what makes the ALTER DEFAULT PRIVILEGES grant in
# 01-roles.sh apply to these tables automatically.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username app_owner --dbname "$POSTGRES_DB" \
    -f /docker-entrypoint-initdb.d/sql/schema.sql
