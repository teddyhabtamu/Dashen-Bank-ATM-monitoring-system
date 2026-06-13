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

echo "Backup complete:"
echo "  $BACKUP_DIR/atm_locations.csv"
echo "  $BACKUP_DIR/atm_transactions.csv"
echo "  $BACKUP_DIR/atm_custom_tables.sql"
