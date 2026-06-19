#!/bin/bash
# Backup PostgreSQL data
# Run this before moving to new machine

BACKUP_DIR="$(dirname "$0")/../config/postgres"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d_%H%M%S)

echo "Backing up PostgreSQL..."

# Backup atm_locations (your manually entered data)
docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "COPY atm_locations TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_locations.csv"

# Backup atm_transactions (your transaction data)
docker exec zabbix-db psql -U zabbix -d zabbix \
  -c "COPY atm_transactions TO STDOUT WITH CSV HEADER" \
  > "$BACKUP_DIR/atm_transactions.csv"

# Full database dump for complete restore
docker exec zabbix-db pg_dump -U zabbix zabbix \
  --no-owner --no-acl \
  -t atm_locations \
  -t atm_transactions \
  > "$BACKUP_DIR/atm_custom_tables.sql"

# Full Zabbix database backup (includes hosts, items, triggers, history)
echo "Backing up full Zabbix database..."
docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl \
  | gzip > "$BACKUP_DIR/zabbix_full_$DATE.sql.gz"

echo "  $BACKUP_DIR/zabbix_full_$DATE.sql.gz"

# Keep only the last 7 full backups to avoid filling disk
ls -t "$BACKUP_DIR"/zabbix_full_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm

echo "Backup complete:"
echo "  $BACKUP_DIR/atm_locations.csv"
echo "  $BACKUP_DIR/atm_transactions.csv"
echo "  $BACKUP_DIR/atm_custom_tables.sql"


# Auto-commit backup to git (best-effort, won't fail the script if git fails)
cd "$(dirname "$0")/.."
git add config/postgres/ 2>/dev/null
git commit -m "Automated backup $(date +%Y-%m-%d)" 2>/dev/null
git push 2>/dev/null
echo "Backup committed to git (if changes existed)"
