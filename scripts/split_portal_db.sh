#!/usr/bin/env bash
# Copy the portal's tables into a separate database. Stage 1 of 4.
#
# WHY: docker-compose sets DB_NAME="zabbix" for the portal, so atm_transactions,
# atm_current_state, app_users, audit_log, txn_source_policy and the rest live in
# the SAME database as Zabbix's configuration and history. A Zabbix upgrade or
# restore can clobber portal data and vice versa, backup and restore are
# coupled, and the two have different release cadences.
#
# WHY pg_dump AND NOT CREATE TABLE AS: the first draft of this script copied
# tables with CREATE TABLE ... AS TABLE, which carries the columns and nothing
# else - no primary keys, no defaults, no indexes, no sequences. The next
# insert would have failed on a duplicate key, weeks later, with nothing in the
# log to explain it. pg_dump carries all of it.
#
# NOTHING LIVE IS TOUCHED. This creates a new database and populates it. The
# existing database is not altered, dropped or locked. Switching a service to
# point at the new database is stage 3, and it is not in this file.
#
# ── RUN ────────────────────────────────────────────────────────────────────
#   bash scripts/split_portal_db.sh            # create + populate (idempotent)
#   bash scripts/split_portal_db.sh --verify   # compare against the source
#   bash scripts/split_portal_db.sh --drop     # remove the copy, keep the original
#
# The full backup was restore-tested on 2026-10-17 before this was written.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
# shellcheck source=portal_tables.sh
source "$HERE/portal_tables.sh"

TARGET_DB="${SPLIT_TARGET_DB:-portal}"
SOURCE_DB="${SPLIT_SOURCE_DB:-zabbix}"
DB_USER="${SPLIT_DB_USER:-zabbix}"

psql_cmd() { docker exec -i zabbix-db psql -U "$DB_USER" -d "$1" ${2:+-c "$2"}; }

verify() {
    echo "Comparing row counts: $SOURCE_DB (source) vs $TARGET_DB (copy)"
    echo "MISMATCH means the copy is not trustworthy - stop and investigate."
    echo
    local failed=0 t
    while read -r t; do
        local src dst
        src=$(docker exec zabbix-db psql -U "$DB_USER" -d "$SOURCE_DB" -tAc \
              "SELECT count(*) FROM public.$t" 2>/dev/null) || src="ERR"
        dst=$(docker exec zabbix-db psql -U "$DB_USER" -d "$TARGET_DB" -tAc \
              "SELECT count(*) FROM public.$t" 2>/dev/null) || dst="ERR"
        if [ "$src" = "$dst" ]; then
            printf '  ok       %-26s %s\n' "$t" "$src"
        else
            printf '  MISMATCH %-26s source=%s copy=%s\n' "$t" "$src" "$dst"
            failed=1
        fi
    done < <(portal_table_names)
    echo
    if [ "$failed" = 0 ]; then
        echo "All tables match. Stage 1 and 2 are done."
        echo "Do NOT switch any service yet - leave that for a quiet window."
    else
        echo "One or more tables do not match. The copy is NOT usable."
    fi
    return $failed
}

verify_sequences() {
    # Row counts alone are not enough. If a sequence is not carried across, the
    # copy looks perfect until the next insert, which then fails with a duplicate
    # key on a primary key that already exists - the same class of failure as the
    # lost constraints, and just as hard to explain from the log.
    local src dst seqs
    seqs=$(docker exec zabbix-db psql -U "$DB_USER" -d "$SOURCE_DB" -tAc \
        "SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY 1" 2>/dev/null)
    if [ -z "$seqs" ]; then
        echo "  (no sequences in $SOURCE_DB - nothing to compare)"
        return 0
    fi
    echo "Sequences (max last_value; a lower value in the copy means the next"
    echo "insert will collide with an existing row):"
    local failed=0 s
    while read -r s; do
        [ -z "$s" ] && continue
        src=$(docker exec zabbix-db psql -U "$DB_USER" -d "$SOURCE_DB" -tAc \
              "SELECT coalesce(max(last_value),0) FROM pg_sequences WHERE sequencename='$s'" 2>/dev/null) || src="ERR"
        dst=$(docker exec zabbix-db psql -U "$DB_USER" -d "$TARGET_DB" -tAc \
              "SELECT coalesce(max(last_value),0) FROM pg_sequences WHERE sequencename='$s'" 2>/dev/null) || dst="ERR"
        if [ "$src" = "$dst" ]; then
            printf '  ok       %-26s %s\n' "$s" "$src"
        elif [ "$dst" != "ERR" ] && [ "$src" != "ERR" ] && [ "$dst" -ge "$src" ] 2>/dev/null; then
            # Ahead is fine: rows were added to the source after the dump.
            printf '  ok       %-26s %s (source now %s)\n' "$s" "$dst" "$src"
        else
            printf '  BEHIND   %-26s source=%s copy=%s\n' "$s" "$src" "$dst"
            failed=1
        fi
    done <<<"$seqs"
    return $failed
}

case "${1:-}" in
    --verify)
        verify
        rc=$?
        verify_sequences
        exit $(( rc | $? ))
        ;;
    --drop)
        echo "Removing $TARGET_DB. The original $SOURCE_DB is untouched."
        psql_cmd postgres "DROP DATABASE IF EXISTS $TARGET_DB"
        exit $?
        ;;
esac

echo "== Stage 1: create $TARGET_DB and copy the portal tables =="
echo "   source=$SOURCE_DB  tables=$(portal_table_count)"
echo

psql_cmd postgres "DROP DATABASE IF EXISTS $TARGET_DB" >/dev/null 2>&1
psql_cmd postgres "CREATE DATABASE $TARGET_DB" || exit 1

echo "-- dumping portal tables from $SOURCE_DB"
# --no-owner/--no-acl so the copy is not tied to a specific role's privileges.
#
# The flag count is checked first, deliberately. pg_dump with no -t flags does
# not dump nothing - it dumps the WHOLE database, so an empty list would quietly
# produce a full copy while the output claimed to be table-scoped.
T_FLAGS="$(portal_dump_flags | tr '\n' ' ')"
T_COUNT=$(printf '%s\n' $T_FLAGS | grep -c '^-t$')
if [ "$T_COUNT" -lt 15 ]; then
    echo "ABORT: only $T_COUNT -t flags resolved, expected $(portal_table_count)."
    echo "Refusing to run pg_dump, because with too few flags it would copy the"
    echo "entire $SOURCE_DB while reporting success."
    exit 1
fi
# shellcheck disable=SC2086  (flags carry no spaces; splitting is intended)
if ! docker exec zabbix-db pg_dump -U "$DB_USER" --no-owner --no-acl $T_FLAGS "$SOURCE_DB" \
        | docker exec -i zabbix-db psql -U "$DB_USER" -d "$TARGET_DB" -q 2>/tmp/split_err; then
    echo "COPY FAILED:"
    cat /tmp/split_err
    echo
    echo "$SOURCE_DB is untouched. Fix the error above and re-run."
    exit 1
fi

if [ -s /tmp/split_err ]; then
    echo "-- psql reported:"
    cat /tmp/split_err
fi

echo
echo "== Stage 2: verify =="
verify
rc=$?
echo
verify_sequences
rc=$(( rc | $? ))
echo
if [ $rc -eq 0 ]; then
    cat <<'NEXT'
Next, when you have a quiet window (NOT today if you are demonstrating):

  Stage 3 - point ONE service at the copy and prove it reads:
      DB_NAME: "portal"

  Stage 4 - switch the portal, and KEEP the old database:
      docker compose up -d report-portal
    Rollback is one env var and a restart.

Reminder: the Zabbix containers must keep DB_NAME=zabbix. Only the portal
services move. Changing it globally points Zabbix at an empty database.
NEXT
fi
exit $rc