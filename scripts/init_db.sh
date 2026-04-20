#!/bin/bash
# ============================================================================
# Laabh — Database Initialization
# 1. Creates role + database + extensions
# 2. Runs Alembic migrations (which apply schema.sql + seed.sql)
# ============================================================================
set -e

DB_NAME="${DB_NAME:-laabh}"
DB_USER="${DB_USER:-laabh}"
DB_PASS="${DB_PASS:-laabh}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

echo "=== Laabh Database Setup ==="

echo "Creating role + database (idempotent)..."
sudo -u postgres psql <<EOF
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
    END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')
\gexec

GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
EOF

echo "Installing extensions as superuser..."
sudo -u postgres psql -d "${DB_NAME}" <<EOF
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "timescaledb";
EOF

echo "Running Alembic migrations..."
alembic upgrade head

echo ""
echo "=== Database setup complete ==="
echo "Connection: postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
