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

# Which database actually holds the portal tables?
#
# Before the split that is "zabbix", where the portal shared a database with
# Zabbix's own configuration. Afterwards it is "portal". Hardcoding either one
# is how the nightly backup ends up covering the wrong database: nothing fails,
# the file is written, the log looks normal, and portal data is simply absent
# from every backup until the day a restore is needed.
#
# So detect it. portal_schema_version is a portal-only marker, created by
# txn_sources.py, and it travels with the copy - which is exactly what makes it
# usable as the test.
PORTAL_DB=""
for candidate in portal zabbix; do
    if docker exec zabbix-db psql -U zabbix -d "$candidate" -tAc \
        "SELECT to_regclass('public.portal_schema_version') IS NOT NULL;" 2>/dev/null \
        | grep -qx t; then
        PORTAL_DB="$candidate"
        break
    fi
done
if [ -z "$PORTAL_DB" ]; then
    echo "ERROR: could not find portal_schema_version in either the portal or"
    echo "       the zabbix database. Refusing to guess - a backup that silently"
    echo "       covers the wrong database is worse than no backup."
    exit 1
fi
if [ "$PORTAL_DB" = "zabbix" ]; then
    echo "Portal tables live in database 'zabbix' (pre-split layout)."
else
    echo "Portal tables live in database '$PORTAL_DB' (split layout)."
fi

echo "Backing up PostgreSQL..."

# Backup atm_locations (your manually entered data)
docker exec zabbix-db psql -U zabbix -d "$PORTAL_DB" \
  -c "COPY atm_locations TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_locations.csv"

# Backup atm_transactions (your transaction data)
docker exec zabbix-db psql -U zabbix -d "$PORTAL_DB" \
  -c "COPY atm_transactions TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_transactions.csv"

# Backup atm_current_state (live ATM states)
docker exec zabbix-db psql -U zabbix -d "$PORTAL_DB" \
  -c "COPY atm_current_state TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_current_state.csv"

# Full database dump for complete restore (ALL custom tables so a
# fresh clone reproduces anomalies, users, audit and schedules too).
# C10: cash_replenishments (financial evidence, never auto-deleted),
# atm_state_history (availability-report source), ticket_seen and
# fault_type_map were previously omitted — losing them on restore.
docker exec zabbix-db pg_dump -U zabbix "$PORTAL_DB" \
  --no-owner --no-acl \
# Table list comes from config/portal_tables.txt, shared with
# scripts/split_portal_db.sh. The two lists drifted once already: this one
# was missing txn_source_policy, which decides whether simulated
# transactions are visible, so a restore from THIS file would have quietly
# re-shown invented money. The full dump below masked the gap entirely.
# shellcheck source=portal_tables.sh
source "$(dirname "$0")/portal_tables.sh"
# Unquoted on purpose: the flags carry no spaces, so word splitting is exactly
# what is wanted - and it avoids eval with nested quotes, which silently
# produced ZERO -t flags when this was written the first way.
PORTAL_T_FLAGS="$(portal_dump_flags | tr '\n' ' ')"
# shellcheck disable=SC2086
docker exec zabbix-db pg_dump -U zabbix "$PORTAL_DB" --no-owner --no-acl $PORTAL_T_FLAGS \
  > "$BACKUP_DIR/atm_custom_tables.sql"

# Full Zabbix database backup (includes hosts, items, triggers, history).
# This one is always "zabbix" and is never redirected: Zabbix's own
# configuration must keep coming from the database Zabbix actually uses.
echo "Backing up full Zabbix database..."
docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl \
  | gzip > "$BACKUP_DIR/zabbix_full_$DATE.sql.gz"
echo "  $BACKUP_DIR/zabbix_full_$DATE.sql.gz"

# Once split, the portal's data lives in its own database and the Zabbix dump
# above no longer contains ANY of it. Without this line every nightly backup
# would be complete-looking and portal-free, and nobody would find out until a
# restore. This is the single most important line in this file after the switch.
if [ "$PORTAL_DB" != "zabbix" ]; then
    echo "Backing up full portal database..."
    docker exec zabbix-db pg_dump -U zabbix "$PORTAL_DB" --no-owner --no-acl \
      | gzip > "$BACKUP_DIR/portal_full_$DATE.sql.gz"
    echo "  $BACKUP_DIR/portal_full_$DATE.sql.gz"
    # Keep 7 of these too, for the same disk reason as the Zabbix dumps.
    ls -t "$BACKUP_DIR"/portal_full_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm
fi

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
# atm_delete_archive is created lazily on first delete — ensure it exists
# so the verification (and future dumps) sees every table in the list.
docker exec zabbix-db psql -U zabbix -d "$PORTAL_DB" \
  -c "CREATE TABLE IF NOT EXISTS atm_delete_archive (id SERIAL PRIMARY KEY, atm_id VARCHAR(20) NOT NULL, deleted_at TIMESTAMP DEFAULT NOW(), deleted_by VARCHAR(100) DEFAULT '', location JSONB, cash_loads JSONB, txn_count INT DEFAULT 0, anomaly_count INT DEFAULT 0, state_history_count INT DEFAULT 0)"
docker exec -i zabbix-db psql -U zabbix -d "$PORTAL_DB" << 'SQLEOF'
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
