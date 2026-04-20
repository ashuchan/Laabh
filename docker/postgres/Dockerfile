FROM timescale/timescaledb:latest-pg16

# Copy schema and seed SQL into init directory
COPY database/schema.sql /docker-entrypoint-initdb.d/01-schema.sql
COPY database/seed.sql /docker-entrypoint-initdb.d/02-seed.sql
