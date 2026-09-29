#!/bin/bash
# Restore PostgreSQL custom tables on new machine
# DESTRUCTIVE: drops custom tables before restoring. Requires --confirm
# and always takes a timestamped pre-restore dump first (C10).

BACKUP_DIR="$(dirname "$0")/../config/postgres"

if [ "$1" != "--confirm" ]; then
  echo "REFUSING: this drops 14 custom tables before restoring."
  echo "Usage: $0 --confirm   (takes a pre-restore dump first)"
  exit 1
fi

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
echo "Taking pre-restore dump first (rollback safety)..."
PRE="pre_restore_$(date +%Y%m%d_%H%M%S).sql"
docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl \
  -t atm_locations -t atm_transactions -t atm_current_state -t atm_anomalies \
  -t atm_network_events -t atm_network_correlation -t atm_network_metrics \
  -t app_users -t audit_log -t scheduled_reports -t cash_replenishments \
  -t atm_state_history -t ticket_seen -t fault_type_map -t atm_delete_archive \
  > "$BACKUP_DIR/$PRE" && echo "  pre-restore dump: $BACKUP_DIR/$PRE"
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
DROP TABLE IF EXISTS cash_replenishments CASCADE;
DROP TABLE IF EXISTS atm_state_history CASCADE;
DROP TABLE IF EXISTS ticket_seen CASCADE;
DROP TABLE IF EXISTS fault_type_map CASCADE;
DROP TABLE IF EXISTS atm_delete_archive CASCADE;
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
SELECT 'scheduled_reports'    , COUNT(*) FROM scheduled_reports
UNION ALL
SELECT 'cash_replenishments'  , COUNT(*) FROM cash_replenishments
UNION ALL
SELECT 'atm_state_history'    , COUNT(*) FROM atm_state_history
UNION ALL
SELECT 'ticket_seen'          , COUNT(*) FROM ticket_seen
UNION ALL
SELECT 'fault_type_map'       , COUNT(*) FROM fault_type_map
UNION ALL
SELECT 'atm_delete_archive'  , COUNT(*) FROM atm_delete_archive;
SQLEOF
