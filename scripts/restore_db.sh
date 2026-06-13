#!/bin/bash
# Restore PostgreSQL custom tables on new machine
# Run AFTER docker compose up -d and waiting 2 minutes

BACKUP_DIR="$(dirname "$0")/../config/postgres"

echo "Waiting for PostgreSQL to be ready..."
sleep 30

echo "Restoring custom tables..."

# Create tables and restore data
docker exec -i zabbix-db psql -U zabbix -d zabbix \
  < "$BACKUP_DIR/atm_custom_tables.sql"

echo "Restore complete."
echo "Verifying..."

docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "SELECT COUNT(*) as atm_locations FROM atm_locations;"

docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "SELECT COUNT(*) as transactions FROM atm_transactions;"
