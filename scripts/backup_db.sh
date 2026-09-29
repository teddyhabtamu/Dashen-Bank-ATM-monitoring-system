#!/bin/bash
# Backup PostgreSQL data
# Run this before moving to new machine

BACKUP_DIR="$(dirname "$0")/../config/postgres"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d_%H%M%S)

# DB credentials for the GLPI dump below (best effort — script still works
# for Postgres-only backups when .env is absent).
if [ -f "$(dirname "$0")/../.env" ]; then
  set -a; . "$(dirname "$0")/../.env"; set +a
fi

echo "Backing up PostgreSQL..."

# Backup atm_locations (your manually entered data)
docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "COPY atm_locations TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_locations.csv"

# Backup atm_transactions (your transaction data)
docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "COPY atm_transactions TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_transactions.csv"

# Backup atm_current_state (live ATM states)
docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "COPY atm_current_state TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_current_state.csv"

# Full database dump for complete restore (ALL custom tables so a
# fresh clone reproduces anomalies, users, audit and schedules too).
# C10: cash_replenishments (financial evidence, never auto-deleted),
# atm_state_history (availability-report source), ticket_seen and
# fault_type_map were previously omitted — losing them on restore.
docker exec zabbix-db pg_dump -U zabbix zabbix \
  --no-owner --no-acl \
  -t atm_locations \
  -t atm_transactions \
  -t atm_current_state \
  -t atm_anomalies \
  -t atm_network_events \
  -t atm_network_correlation \
  -t atm_network_metrics \
  -t app_users \
  -t audit_log \
  -t scheduled_reports \
  -t cash_replenishments \
  -t atm_state_history \
  -t ticket_seen \
  -t fault_type_map \
  -t atm_delete_archive \
  > "$BACKUP_DIR/atm_custom_tables.sql"

# Full Zabbix database backup (includes hosts, items, triggers, history)
echo "Backing up full Zabbix database..."
docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl \
  | gzip > "$BACKUP_DIR/zabbix_full_$DATE.sql.gz"

# GLPI tickets/SLAs/groups live in MariaDB, not Postgres — without this,
# a rebuild loses every ticket and must re-run the GLPI wizard (C10).
if docker exec glpi-db true 2>/dev/null; then
  echo "Backing up GLPI database..."
  docker exec glpi-db mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" glpi --single-transaction \
    2>/dev/null | gzip > "$BACKUP_DIR/glpi_$DATE.sql.gz" || \
  docker exec glpi-db mysqldump -u glpi -p"$MYSQL_PASSWORD" glpi --single-transaction \
    | gzip > "$BACKUP_DIR/glpi_$DATE.sql.gz"
  ls -t "$BACKUP_DIR"/glpi_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm
  echo "  $BACKUP_DIR/glpi_$DATE.sql.gz"
else
  echo "  WARNING: glpi-db not running — GLPI backup skipped"
fi

echo "  $BACKUP_DIR/zabbix_full_$DATE.sql.gz"

# Keep only the last 7 full backups to avoid filling disk
ls -t "$BACKUP_DIR"/zabbix_full_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm

echo "Backup complete:"
echo "  $BACKUP_DIR/atm_locations.csv"
echo "  $BACKUP_DIR/atm_transactions.csv"
echo "  $BACKUP_DIR/atm_current_state.csv"
echo "  $BACKUP_DIR/atm_custom_tables.sql"

# Verify backup integrity
echo ""
echo "Verifying backup counts..."
docker exec -i zabbix-db psql -U zabbix -d zabbix << 'SQLEOF'
SELECT 'atm_locations'        as table_name, COUNT(*) as rows FROM atm_locations
UNION ALL
SELECT 'atm_transactions'     , COUNT(*) FROM atm_transactions
UNION ALL
SELECT 'atm_current_state'    , COUNT(*) FROM atm_current_state
UNION ALL
SELECT 'atm_anomalies'        , COUNT(*) FROM atm_anomalies
UNION ALL
SELECT 'atm_network_events'   , COUNT(*) FROM atm_network_events
UNION ALL
SELECT 'atm_network_correlation', COUNT(*) FROM atm_network_correlation
UNION ALL
SELECT 'atm_network_metrics'  , COUNT(*) FROM atm_network_metrics
UNION ALL
SELECT 'app_users'            , COUNT(*) FROM app_users
UNION ALL
SELECT 'audit_log'            , COUNT(*) FROM audit_log
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


# NOTE: backups are NOT committed to git — database dumps contain user data
# and this repository may be public. Copy them to secure storage instead:
#   scp config/postgres/zabbix_full_*.sql.gz config/postgres/glpi_*.sql.gz <backup-server>:/backups/
# NOT covered here (by design, noted for the restore runbook): Grafana
# dashboards (grafana-data volume — provisioned from config/grafana in git),
# OpenSearch EJ indices (os-data volume — weekly snapshots per the prod
# guide), glpi-root volume, filebeat.yml and .env secrets. Copy .env to
# sealed off-host storage whenever it changes: losing it means regenerating
# every password and re-wiring the Zabbix GLPI media.
