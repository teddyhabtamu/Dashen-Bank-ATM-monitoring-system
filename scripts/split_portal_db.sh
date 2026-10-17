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

# BASH_SOURCE, not $0: when a test sources this file, $0 is the test script
# and the path below would resolve to the wrong directory.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
# shellcheck source=portal_tables.sh
source "$HERE/portal_tables.sh"

TARGET_DB="${SPLIT_TARGET_DB:-portal}"
SOURCE_DB="${SPLIT_SOURCE_DB:-zabbix}"
DB_USER="${SPLIT_DB_USER:-zabbix}"

psql_cmd() { run_psql -d "$1" ${2:+-c "$2"}; }

# sql_of <db> <sql> -> unaligned tuples, no header, blank line on error
sql_of() {
    # stdin is closed deliberately.
    #
    # run_psql used to forward stdin with `docker exec -i`, and psql consumes
    # whatever it is given. Inside the `while read` loops below that meant psql
    # ate the remaining table names, so the copy was compared against the source
    # for exactly ONE table and the run finished with "All 1 tables match" and
    # exit status 0 - on UAT, on 2026-10-18, immediately after a run that had
    # correctly compared all eighteen. A verification that quietly checks one
    # table is worse than none, because it looks like it worked.
    run_psql -d "$1" -tAc "$2" 2>/dev/null </dev/null
}

# Every database call goes through here, so a test can point the whole script at
# a different postgres by defining run_psql before sourcing it. The verify steps
# had produced two wrong verdicts before this existed and could only be checked
# by running the real copy on a live server.
run_psql() { docker exec zabbix-db psql -U "$DB_USER" "$@"; }
# No -i. Forwarding stdin is what let psql swallow a caller's loop input.
# The restore is the one place stdin really is the data - it reads a dump from a
# pipe - so that one goes through run_psql_stdin, which does forward it.
run_psql_stdin() { docker exec -i zabbix-db psql -U "$DB_USER" "$@"; }
run_pg_dump() { docker exec zabbix-db pg_dump -U "$DB_USER" "$@"; }

verify() {
    echo "Comparing row counts: $SOURCE_DB (source) vs $TARGET_DB (copy)"
    echo "MISMATCH means the copy is not trustworthy - stop and investigate."
    echo
    local failed=0 t src dst checked=0
    while read -r t; do
        [ -z "$t" ] && continue
        checked=$((checked+1))
        src=$(sql_of "$SOURCE_DB" "SELECT count(*) FROM public.$t;")
        dst=$(sql_of "$TARGET_DB" "SELECT count(*) FROM public.$t;")
        # An unreachable or erroring query used to be reported as "ok", because
        # both sides came back as the same placeholder string and the comparison
        # was a plain string equality. So a copy that could not be read at all
        # was announced as "All tables match" - the one situation where this
        # check exists to stop a switch happening, and it waved it through.
        if [ -z "$src" ] || [ -z "$dst" ]; then
            printf '  ERROR    %-26s source=%s copy=%s\n' "$t" "${src:-<unreadable>}" "${dst:-<unreadable>}"
            failed=1
        elif [ "$src" = "$dst" ]; then
            printf '  ok       %-26s %s\n' "$t" "$src"
        else
            printf '  MISMATCH %-26s source=%s copy=%s\n' "$t" "$src" "$dst"
            failed=1
        fi
    done < <(portal_table_names)
    local expected_total
    expected_total=$(portal_table_count)
    if [ "$checked" -eq 0 ]; then
        echo "  ERROR    no tables were compared - the table list is empty"
        failed=1
    elif [ "$checked" -ne "$expected_total" ]; then
        # This is not a hypothetical. A query that consumed stdin ended the loop
        # early, and the run reported "All 1 tables match" with a zero exit code.
        # Comparing fewer tables than the list names is a failure, always - the
        # tables that were skipped are exactly the ones nobody knows about.
        echo "  ERROR    compared $checked of $expected_total tables - the loop was cut short"
        failed=1
    fi
    echo
    if [ "$failed" = 0 ]; then
        echo "All $checked tables match. Stage 1 and 2 are done."
        echo "Do NOT switch any service yet - leave that for a quiet window."
    else
        echo "One or more tables do not match. The copy is NOT usable."
    fi
    return $failed
}

# "'a','b','c'" for the table list, so it can go straight into an ARRAY[...]
portal_table_array_literal() {
    portal_table_names | sed "s/.*/'&'/" | paste -sd, -
}

verify_sequences() {
    # Row counts alone are not enough. If a sequence is not carried across, the
    # copy looks perfect until the next insert, which then fails with a duplicate
    # key on a primary key that already exists - the same class of failure as the
    # lost constraints, and just as hard to explain from the log.
    #
    # Only sequences OWNED BY A PORTAL TABLE are checked. Checking every sequence
    # in the schema sounds stricter but is simply wrong: the source database also
    # holds Zabbix's own sequences (changelog_changelogid_seq and the proxy_*
    # family), which are deliberately not copied. Reporting those as BEHIND made
    # a perfect copy look broken - it did exactly that on UAT on 2026-10-17.
    #
    # "not present" and "present but never used" are also kept apart. Reading
    # last_value through pg_sequences yields NULL for a sequence that has never
    # been called, and coalescing that to 0 makes an absent sequence look
    # identical to an empty one. A check that passes when the thing it is
    # checking is missing is not a check.
    local tbls src dst s
    tbls=$(portal_table_array_literal)
    src=$(sql_of "$SOURCE_DB" "
        SELECT s.relname
        FROM pg_class s
        JOIN pg_namespace sn ON sn.oid = s.relnamespace
        JOIN pg_depend d ON d.objid = s.oid
                        AND d.classid = 'pg_class'::regclass
                        AND d.refclassid = 'pg_class'::regclass
                        AND d.deptype = 'a'
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_namespace tn ON tn.oid = t.relnamespace
        WHERE s.relkind = 'S' AND sn.nspname = 'public' AND tn.nspname = 'public'
          AND t.relname = ANY(ARRAY[$tbls])
        ORDER BY 1;")
    if [ -z "$src" ]; then
        echo "  (no portal sequences in $SOURCE_DB - nothing to compare)"
        return 0
    fi
    local nseq
    nseq=$(printf '%s\n' "$src" | grep -c .)
    echo "Sequences owned by portal tables ($nseq):"
    local failed=0
    while read -r s; do
        [ -z "$s" ] && continue
        if [ "$(sql_of "$TARGET_DB" "SELECT to_regclass('public.$s') IS NULL;")" = "t" ]; then
            printf '  MISSING  %-26s not present in the copy\n' "$s"
            failed=1
            continue
        fi
        local sv dv
        sv=$(sql_of "$SOURCE_DB" "SELECT last_value || ':' || is_called FROM public.$s;")
        dv=$(sql_of "$TARGET_DB" "SELECT last_value || ':' || is_called FROM public.$s;")
        if [ -z "$sv" ] || [ -z "$dv" ]; then
            printf '  ERROR    %-26s source=%s copy=%s\n' "$s" "${sv:-<unreadable>}" "${dv:-<unreadable>}"
            failed=1
        elif [ "$sv" = "$dv" ]; then
            printf '  ok       %-26s %s\n' "$s" "$sv"
        else
            local sl dl
            sl=${sv%%:*}; dl=${dv%%:*}
            if [ "${dv##*:}" = "false" ] || [ "$dl" -lt "$sl" ] 2>/dev/null; then
                printf '  BEHIND   %-26s source=%s copy=%s\n' "$s" "$sv" "$dv"
                failed=1
            else
                # Ahead is fine: rows landed in the source after the dump ran.
                printf '  ok       %-26s %s (source was %s)\n' "$s" "$dv" "$sv"
            fi
        fi
    done <<<"$src"
    local checked
    checked=$(printf '%s\n' "$src" | grep -c .)
    if [ "$checked" -ne "$nseq" ]; then
        echo "  ERROR    compared $checked of $nseq sequences - the loop was cut short"
        failed=1
    fi
    return $failed
}

verify_no_zabbix_leakage() {
    # The copy must contain the portal's tables and nothing else. If the flag
    # list ever resolves empty, pg_dump does not dump nothing - it dumps the whole
    # database - and this is what would catch it.
    local extra expected found
    extra=$(sql_of "$TARGET_DB" "
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename NOT IN ($(portal_table_array_literal))
        ORDER BY 1;")
    found=$(sql_of "$TARGET_DB" "SELECT count(*) FROM pg_tables WHERE schemaname='public';")
    expected=$(portal_table_count)
    if [ -z "$found" ] || [ "$found" -eq 0 ] 2>/dev/null; then
        # An empty list must not read as a pass. NOT IN () over an empty array is
        # how a copy with nothing in it would come back "clean".
        echo "  UNKNOWN  could not count tables in $TARGET_DB - treating as failure"
        return 1
    fi
    if [ -z "$extra" ]; then
        printf '  ok       %s has %s tables, all expected\n' "$TARGET_DB" "$found"
        return 0
    fi
    echo "  LEAK     $TARGET_DB contains tables that are not portal tables:"
    sed 's/^/             /' <<<"$extra"
    return 1
}

main() {
case "${1:-}" in
    --verify)
        verify
        rc=$?
        verify_sequences
        rc=$(( rc | $? ))
        echo
        verify_no_zabbix_leakage
        exit $(( rc | $? ))
        ;;
    --drop)
        echo "Removing $TARGET_DB. The original $SOURCE_DB is untouched."
        psql_cmd postgres "DROP DATABASE IF EXISTS $TARGET_DB"
        return $?
        ;;
esac


echo "== Stage 1: create $TARGET_DB and copy the portal tables =="
echo "   source=$SOURCE_DB  tables=$(portal_table_count)"
echo


psql_cmd postgres "DROP DATABASE IF EXISTS $TARGET_DB" >/dev/null 2>&1
psql_cmd postgres "CREATE DATABASE $TARGET_DB" || return 1


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
    return 1
fi
# shellcheck disable=SC2086  (flags carry no spaces; splitting is intended)
if ! run_pg_dump --no-owner --no-acl $T_FLAGS "$SOURCE_DB" \
        | run_psql_stdin -d "$TARGET_DB" -q 2>/tmp/split_err; then
    echo "COPY FAILED:"
    cat /tmp/split_err
    echo
    echo "$SOURCE_DB is untouched. Fix the error above and re-run."
    return 1
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
verify_no_zabbix_leakage
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
return $rc
}

# Sourced by tests/test_split_verify.sh to exercise the verify steps against
# a real postgres. Running it is what actually does the copy.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
