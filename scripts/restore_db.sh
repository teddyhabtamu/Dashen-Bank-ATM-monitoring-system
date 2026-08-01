#!/bin/bash
# Restore PostgreSQL custom tables on new machine
# Safely drops existing tables before restoring

BACKUP_DIR="$(dirname "$0")/../config/postgres"

echo "Actively polling PostgreSQL readiness status..."
# Loop up to 30 times checking if Postgres container is processing queries
for i in {1..30}; do
    if docker exec zabbix-db pg_isready -U zabbix -d zabbix &>/dev/null; then
        echo "PostgreSQL is up and accepting connections!"
        break
    fi
    echo "Database still warming up... waiting 3s (Attempt $i/30)"
    sleep 3
done

echo "Dropping existing custom tables if they exist..."
docker exec -i zabbix-db psql -U zabbix -d zabbix << 'SQLEOF'
DROP TABLE IF EXISTS atm_network_correlation CASCADE;
DROP TABLE IF EXISTS atm_network_events CASCADE;
DROP TABLE IF EXISTS atm_network_metrics CASCADE;
DROP TABLE IF EXISTS atm_anomalies CASCADE;
DROP TABLE IF EXISTS atm_transactions CASCADE;
DROP TABLE IF EXISTS atm_current_state CASCADE;
DROP TABLE IF EXISTS atm_locations CASCADE;
DROP TABLE IF EXISTS scheduled_reports CASCADE;
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS app_users CASCADE;
SQLEOF

echo "Restoring from backup..."
if [ -f "$BACKUP_DIR/atm_custom_tables.sql" ]; then
    docker exec -i zabbix-db psql -U zabbix -d zabbix < "$BACKUP_DIR/atm_custom_tables.sql"
else
    echo "No backup file found at $BACKUP_DIR/atm_custom_tables.sql to pipe."
fi

echo ""
echo "Verifying restore..."
docker exec -i zabbix-db psql -U zabbix -d zabbix << 'SQLEOF'
SELECT 'atm_locations'        as table_name, COUNT(*) as rows FROM atm_locations
UNION ALL
SELECT 'atm_transactions'     , COUNT(*) FROM atm_transactions
UNION ALL
SELECT 'atm_current_state'    , COUNT(*) FROM atm_current_state
UNION ALL
SELECT 'atm_anomalies'       , COUNT(*) FROM atm_anomalies
UNION ALL
SELECT 'atm_network_events'   , COUNT(*) FROM atm_network_events
UNION ALL
SELECT 'atm_network_correlation', COUNT(*) FROM atm_network_correlation
UNION ALL
SELECT 'atm_network_metrics'  , COUNT(*) FROM atm_network_metrics
UNION ALL
SELECT 'app_users'            , COUNT(*) FROM app_users
UNION ALL
SELECT 'audit_log'           , COUNT(*) FROM audit_log
UNION ALL
SELECT 'scheduled_reports'    , COUNT(*) FROM scheduled_reports;
SQLEOF
