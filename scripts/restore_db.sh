#!/bin/bash
# Restore PostgreSQL custom tables on new machine
# Safely drops existing tables before restoring

BACKUP_DIR="$(dirname "$0")/../config/postgres"

echo "Waiting for PostgreSQL to be ready..."
sleep 10

echo "Dropping existing custom tables if they exist..."
docker exec zabbix-db psql -U zabbix -d zabbix -c "
DROP TABLE IF EXISTS atm_transactions CASCADE;
DROP TABLE IF EXISTS atm_locations CASCADE;
DROP SEQUENCE IF EXISTS atm_transactions_id_seq CASCADE;
"

echo "Restoring from backup..."
docker exec -i zabbix-db psql -U zabbix -d zabbix \
  < "$BACKUP_DIR/atm_custom_tables.sql"

echo ""
echo "Verifying restore..."
docker exec zabbix-db psql -U zabbix -d zabbix -c "
SELECT 'atm_locations' as table_name,
  COUNT(*) as rows FROM atm_locations
UNION ALL
SELECT 'atm_transactions',
  COUNT(*) FROM atm_transactions;
"
