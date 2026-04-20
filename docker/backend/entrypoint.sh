#!/bin/bash
set -e

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL..."
until pg_isready -h postgres -U laabh; do
  sleep 2
done
echo "PostgreSQL ready."

# Run Alembic migrations (if any)
if [ -f alembic.ini ]; then
  alembic upgrade head || echo "Alembic migration skipped (no new migrations)"
fi

exec "$@"
