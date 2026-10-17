#!/bin/bash
# Restore PostgreSQL from backup.
#
# DESTRUCTIVE. Requires --confirm and always takes a timestamped pre-restore
# dump of whatever it is about to overwrite.
#
# ── WHY THIS FILE WAS REWRITTEN ───────────────────────────────────────────────
# The previous version had six separate faults, and one would have silently
# undone the database split:
#
#   1. It restored only the zabbix database. Since the split that is where no
#      portal data lives, so a restore would bring back nothing.
#   2. Its table list was maintained by hand and was missing five relations:
#      atm_cassette_levels, atm_transactions_visible, fleet_report_cache,
#      portal_schema_version and txn_source_policy. Those would have been lost -
#      including the view nearly every page queries, and the table that decides
#      whether simulated transactions are on screen.
#   3. The same five were absent from its DROP list, so they would have survived
#      with stale data while the other fifteen were replaced. The result is a
#      database that is half restored and half from three months ago, which is
#      worse than either state alone.
#   4. It restored atm_custom_tables.sql, which backup_db.sh now writes from the
#      PORTAL database. So it would have loaded portal data back into zabbix and
#      undone the split, while reporting success throughout.
#   5. Its pre-restore safety dump covered 14 tables from zabbix, so the live
#      portal data it was about to destroy was never actually captured.
#   6. It printed counts afterwards and failed on nothing. A restore that lost
#      most of the data still exited 0.
#
# The table list now comes from config/portal_tables.txt, the same file the
# backup and the split read, so it cannot drift from them.
#
# ── USAGE ────────────────────────────────────────────────────────────────────
#   bash scripts/restore_db.sh --confirm
#       Restores the portal database from the newest portal_full_*.sql.gz, or
#       from atm_custom_tables.sql if no full dump is present. This is the
#       normal case, and it never touches the zabbix database.
#
#   bash scripts/restore_db.sh --confirm --also-zabbix
#       Additionally replaces the WHOLE zabbix database - Zabbix's own
#       configuration, hosts, items and history included. Deliberately separate
#       and deliberately loud, because it is not a portal restore.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Overridable so the restore can be rehearsed against a fixture rather than
# only against production. The default is unchanged for normal use.
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$0")/../config/postgres}"
# shellcheck source=portal_tables.sh
source "$HERE/portal_tables.sh"

CONFIRMED=0
ALSO_ZABBIX=0
for arg in "$@"; do
    case "$arg" in
        --confirm)      CONFIRMED=1 ;;
        --also-zabbix)  ALSO_ZABBIX=1 ;;
    esac
done

if [ "$CONFIRMED" -ne 1 ]; then
    echo "REFUSING to run: this overwrites the portal database."
    echo "Usage: $0 --confirm [--also-zabbix]"
    echo "It takes a pre-restore dump first, so the current contents are recoverable."
    exit 1
fi

RELATIONS=$(portal_table_count)
echo "Portal relations in the list of record: $RELATIONS"

echo "Actively polling PostgreSQL readiness..."
ready=0
for _ in $(seq 1 30); do
    if docker exec zabbix-db pg_isready -U zabbix -d zabbix &>/dev/null; then
        echo "PostgreSQL is up and accepting connections!"
        ready=1
        break
    fi
    echo "Database still warming up... waiting 3s"
    sleep 3
done
if [ "$ready" -ne 1 ]; then
    echo "ERROR: PostgreSQL never became ready. Aborting before any change."
    exit 1
fi

PORTAL_DB="$(detect_portal_db || true)"
if [ -z "$PORTAL_DB" ]; then
    echo "ERROR: could not find portal_schema_version in either database."
    echo "       Refusing to guess which one to restore into."
    exit 1
fi
echo "Portal tables live in database: $PORTAL_DB"

# Newest match wins, and an explicit absence is reported rather than skipped.
newest() { ls -t "$BACKUP_DIR"/$1 2>/dev/null | head -1; }
PORTAL_DUMP="$(newest 'portal_full_*.sql.gz')"
ZBX_DUMP="$(newest 'zabbix_full_*.sql.gz')"
SCOPED_DUMP="$BACKUP_DIR/atm_custom_tables.sql"

if [ -z "$PORTAL_DUMP" ]; then
    if [ -f "$SCOPED_DUMP" ]; then
        PORTAL_DUMP="$SCOPED_DUMP"
        echo "No portal_full_*.sql.gz found; falling back to the table-scoped dump."
        echo "NOTE: that dump carries only the portal relations, not the manifest,"
        echo "      so the restored database will have no split_snapshot record."
    else
        echo "ERROR: no portal backup found. Looked for:"
        echo "         $BACKUP_DIR/portal_full_*.sql.gz"
        echo "         $SCOPED_DUMP"
        echo "Refusing to continue - a restore with nothing to restore from that"
        echo "reports success and leaves you with an empty database."
        exit 1
    fi
fi

# ── validate the backup BEFORE destroying anything ──────────────────────────
# Rehearsing this script against a fixture, where BACKUP_DIR pointed somewhere
# without a dump, dropped the database and then restored an empty one. The
# verification caught it afterwards and refused to claim success - but by then
# the data was already gone and only the pre-restore dump stood between that
# and an unrecoverable loss.
#
# A corrupt or truncated gzip fails at the worst possible moment if you only
# find out halfway through loading it. So: is the file there, is it non-empty,
# and does it decompress? All three, before the DROP.
echo ""
echo "Checking the backup is usable before touching anything..."
if [ ! -s "$PORTAL_DUMP" ]; then
    echo "ERROR: $PORTAL_DUMP is missing or empty."
    echo "       Nothing has been changed."
    exit 1
fi
case "$PORTAL_DUMP" in
    *.gz)
        if ! gzip -t "$PORTAL_DUMP" 2>/dev/null; then
            echo "ERROR: $PORTAL_DUMP is not a readable gzip archive."
            echo "       It would fail partway through loading. Nothing has been"
            echo "       changed - fix or replace the backup and try again."
            exit 1
        fi
        ;;
esac
SIZE=$(du -h "$PORTAL_DUMP" | cut -f1)
echo "  backup verified: $PORTAL_DUMP ($SIZE)"

# ── safety dump ──────────────────────────────────────────────────────────────
PRE="pre_restore_$(date +%Y%m%d_%H%M%S).sql"
echo "Taking a pre-restore dump of '$PORTAL_DB' so this is reversible..."
if ! docker exec zabbix-db pg_dump -U zabbix "$PORTAL_DB" --no-owner --no-acl \
        | gzip > "$BACKUP_DIR/$PRE.gz"; then
    echo "ERROR: the pre-restore dump failed. Aborting before any change."
    echo "       Overwriting without a way back is not a restore."
    exit 1
fi
echo "  pre-restore dump: $BACKUP_DIR/$PRE.gz"

# ── restore ──────────────────────────────────────────────────────────────────
echo ""
echo "Restoring '$PORTAL_DB' from: $PORTAL_DUMP"
# DROP DATABASE rather than dropping tables one by one. A hand-maintained DROP
# list is how the previous version left five relations behind holding stale
# data; replacing the database wholesale cannot be half-done.
if ! docker exec zabbix-db psql -U zabbix -d postgres \
        -c "DROP DATABASE IF EXISTS \"$PORTAL_DB\"" \
        -c "CREATE DATABASE \"$PORTAL_DB\""; then
    echo "ERROR: could not recreate '$PORTAL_DB'. The pre-restore dump is at"
    echo "       $BACKUP_DIR/$PRE.gz - restore from it before doing anything else."
    exit 1
fi

case "$PORTAL_DUMP" in
    *.gz) RESTORE_CMD=(zcat "$PORTAL_DUMP") ;;
    *)    RESTORE_CMD=(cat "$PORTAL_DUMP") ;;
esac

echo "Loading dump (this takes a moment)..."
if ! "${RESTORE_CMD[@]}" | docker exec -i zabbix-db psql -U zabbix -d "$PORTAL_DB" -q 2>&1 \
        | grep -viE '^$|set_config|setval|^-+\+?$|^\(1 row\)|^ +[0-9]+$|unrestrict|restrict' | head -20; then
    echo "(no errors reported above)"
fi

# ── verify, and fail on it ───────────────────────────────────────────────────
# Counting rows and printing them proves nothing: the previous version did
# exactly that and would have exited 0 having restored almost nothing. What
# matters is that every relation the list of record names is actually present.
echo ""
echo "Verifying every relation from the list of record is present..."
missing=""
while read -r t; do
    [ -z "$t" ] && continue
    got=$(docker exec zabbix-db psql -U zabbix -d "$PORTAL_DB" -tAc \
        "SELECT to_regclass('public.$t') IS NOT NULL;" 2>/dev/null)
    if [ "$got" != "t" ]; then
        missing="$missing $t"
    fi
done < <(portal_table_names)

if [ -n "$missing" ]; then
    echo "FAILED. These relations are in config/portal_tables.txt but are NOT in"
    echo "the restored database:$missing"
    echo ""
    echo "The restore did NOT complete. Do not start the portal against it."
    echo "The previous contents are in $BACKUP_DIR/$PRE.gz"
    exit 1
fi
echo "All $RELATIONS relations present."

echo ""
echo "Row counts in the restored database:"
while read -r t; do
    [ -z "$t" ] && continue
    n=$(docker exec zabbix-db psql -U zabbix -d "$PORTAL_DB" -tAc \
        "SELECT count(*) FROM public.$t;" 2>/dev/null)
    printf '  %-28s %s\n' "$t" "${n:-<unreadable>}"
done < <(portal_table_names)

if [ "$ALSO_ZABBIX" -eq 1 ]; then
    echo ""
    echo "================================================================"
    echo " --also-zabbix: replacing the WHOLE zabbix database."
    echo " Zabbix's own configuration, hosts, items, triggers and history"
    echo " are inside it. This is NOT a portal restore."
    echo "================================================================"
    if [ -z "$ZBX_DUMP" ]; then
        echo "ERROR: no zabbix_full_*.sql.gz found. Nothing done."
        exit 1
    fi
    ZPRE="pre_restore_zabbix_$(date +%Y%m%d_%H%M%S).sql.gz"
    echo "Pre-restore dump of zabbix to $BACKUP_DIR/$ZPRE.gz"
    if ! docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl \
            | gzip > "$BACKUP_DIR/$ZPRE.gz"; then
        echo "ERROR: pre-restore dump failed. Aborting before any change."
        exit 1
    fi
    docker exec zabbix-db psql -U zabbix -d postgres \
        -c "DROP DATABASE IF EXISTS zabbix WITH (FORCE)" -c "CREATE DATABASE zabbix" || exit 1
    zcat "$ZBX_DUMP" | docker exec -i zabbix-db psql -U zabbix -d zabbix -q \
        2>&1 | grep -viE '^$|set_config|setval|^-+\+?$|^\(1 row\)|^ +[0-9]+$' | head -10
    echo "Zabbix database restored from $ZBX_DUMP"
    echo "Start Zabbix only after confirming it accepts connections."
fi

echo ""
echo "Restore complete. Nothing was switched: the services still point wherever"
echo "PORTAL_DB_NAME says. Restart them yourself once you are satisfied."
