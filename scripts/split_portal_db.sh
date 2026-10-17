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
    echo "Checking the copy against the source as it stood when the copy was taken."
    echo "MISMATCH means the copy does not hold what the source held - stop."
    echo
    if [ -z "$(sql_of "$TARGET_DB" "SELECT to_regclass('public.$MANIFEST_TABLE');")" ] \
       || [ "$(sql_of "$TARGET_DB" "SELECT to_regclass('public.$MANIFEST_TABLE') IS NULL;")" = "t" ]; then
        echo "  ERROR    $TARGET_DB has no $MANIFEST_TABLE."
        echo "           That copy was made by an older version of this script, which"
        echo "           recorded nothing. Re-run without --verify to rebuild it."
        return 1
    fi
    local failed=0 t expected dst now checked=0 drifted=0
    while read -r t; do
        [ -z "$t" ] && continue
        checked=$((checked+1))
        expected=$(manifest_value table "$t")
        dst=$(sql_of "$TARGET_DB" "SELECT count(*) FROM public.$t;")
        if [ -z "$expected" ] || [ -z "$dst" ]; then
            printf '  ERROR    %-26s expected=%s copy=%s\n' "$t" "${expected:-<none>}" "${dst:-<unreadable>}"
            failed=1
            continue
        fi
        if [ "$expected" = "$dst" ]; then
            now=$(sql_of "$SOURCE_DB" "SELECT count(*) FROM public.$t;")
            if [ -n "$now" ] && [ "$now" != "$expected" ]; then
                drifted=$((drifted+1))
                printf '  ok       %-26s %s (source has since grown to %s)\n' "$t" "$dst" "$now"
            else
                printf '  ok       %-26s %s\n' "$t" "$dst"
            fi
        else
            printf '  MISMATCH %-26s snapshot=%s copy=%s\n' "$t" "$expected" "$dst"
            failed=1
        fi
    done < <(portal_table_names)
    local expected_total
    expected_total=$(portal_table_count)
    if [ "$checked" -eq 0 ]; then
        echo "  ERROR    no tables were compared - the table list is empty"
        failed=1
    elif [ "$checked" -ne "$expected_total" ]; then
        # Not hypothetical: a query that consumed stdin ended this loop early and
        # the run reported "All 1 tables match" with a zero exit code.
        echo "  ERROR    compared $checked of $expected_total tables - the loop was cut short"
        failed=1
    fi
    echo
    if [ "$failed" -eq 0 ]; then
        echo "All $checked tables match the snapshot taken at copy time."
        if [ "$drifted" -gt 0 ]; then
            echo "$drifted table(s) have received new rows since - normal for a live"
            echo "source, and the reason this compares against the snapshot."
        fi
        echo "Do NOT switch any service yet - leave that for a quiet window."
    else
        echo "The copy does NOT hold what the source held when it was taken."
    fi
    return $failed
}

portal_table_array_literal() {
    portal_table_names | sed "s/.*/'&'/" | paste -sd, -
}

# Sequences belonging to a portal table. Zabbix's own sequences (changelog, the
# proxy_* family) live in the same database and are deliberately not copied, so
# including them would make a perfect copy report a failure.
owned_sequences() {
    sql_of "$SOURCE_DB" "
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
          AND t.relname = ANY(ARRAY[$(portal_table_array_literal)])
        ORDER BY 1;"
}

# A point-in-time copy cannot be checked for equality against a source that keeps
# being written to. network-correlator adds to atm_network_metrics every 120
# seconds, so a perfectly good copy is ALWAYS behind by the time verify runs, and
# demanding equality makes the check cry wolf on a healthy system. A check that
# always fails is one people learn to ignore, which is worse than no check.
#
# So the source's row counts and sequence positions are recorded inside the copy
# at dump time, and verify compares against those. That is the question that can
# actually be answered: did the copy capture what the source held when it was
# taken? How far the source has moved since is then reported separately, because
# that is expected, not a fault.
MANIFEST_TABLE=portal_split_manifest

capture_manifest() {
    local out="$1" t s
    : > "$out"
    while read -r t; do
        [ -z "$t" ] && continue
        printf 'table\t%s\t%s\n' "$t" \
            "$(sql_of "$SOURCE_DB" "SELECT count(*) FROM public.$t;")" >> "$out"
    done < <(portal_table_names)
    while read -r s; do
        [ -z "$s" ] && continue
        printf 'sequence\t%s\t%s\n' "$s" \
            "$(sql_of "$SOURCE_DB" "SELECT last_value || ':' || is_called FROM public.$s;")" >> "$out"
    done <<<"$(owned_sequences)"
}

load_manifest() {
    local f="$1" kind name value
    {
        echo "CREATE TABLE IF NOT EXISTS $MANIFEST_TABLE ("
        echo "  kind text NOT NULL, name text NOT NULL,"
        echo "  value text, dumped_at timestamptz NOT NULL DEFAULT now(),"
        echo "  PRIMARY KEY (kind, name));"
        echo "DELETE FROM $MANIFEST_TABLE;"
        while IFS="$(printf '\t')" read -r kind name value; do
            [ -z "$name" ] && continue
            printf "INSERT INTO %s (kind,name,value) VALUES ('%s','%s','%s');\n" \
                "$MANIFEST_TABLE" "$kind" "$name" "$value"
        done < "$f"
    } | run_psql_stdin -d "$TARGET_DB" -q 2>/dev/null
}

manifest_value() {
    sql_of "$TARGET_DB" \
        "SELECT value FROM $MANIFEST_TABLE WHERE kind='$1' AND name='$2';"
}

verify_sequences() {
    # Same reasoning as verify(): a sequence that the source has advanced past
    # since the copy was taken is staleness, not corruption. fault_type_map is
    # re-seeded by init_audit_schema() with ON CONFLICT DO UPDATE on every portal
    # start, which burns one sequence value per row while leaving the row count
    # alone - so its position drifts constantly and means nothing.
    #
    # A copy that is genuinely BEHIND will collide on the next insert, so this
    # still matters - it is just measured against the snapshot, not the clock.
    local nseq checked=0 failed=0 s expected dv now
    nseq=$(owned_sequences | grep -c .)
    if [ "$nseq" -eq 0 ]; then
        echo "  (no portal sequences in $SOURCE_DB - nothing to compare)"
        return 0
    fi
    echo "Sequences owned by portal tables ($nseq):"
    while read -r s; do
        [ -z "$s" ] && continue
        checked=$((checked+1))
        if [ "$(sql_of "$TARGET_DB" "SELECT to_regclass('public.$s') IS NULL;")" = "t" ]; then
            printf '  MISSING  %-26s not present in the copy\n' "$s"
            failed=1
            continue
        fi
        expected=$(manifest_value sequence "$s")
        dv=$(sql_of "$TARGET_DB" "SELECT last_value || ':' || is_called FROM public.$s;")
        if [ -z "$expected" ] || [ -z "$dv" ]; then
            printf '  ERROR    %-26s snapshot=%s copy=%s\n' "$s" "${expected:-<none>}" "${dv:-<unreadable>}"
            failed=1
        elif [ "$expected" = "$dv" ]; then
            now=$(sql_of "$SOURCE_DB" "SELECT last_value || ':' || is_called FROM public.$s;")
            if [ -n "$now" ] && [ "$now" != "$expected" ]; then
                printf '  ok       %-26s %s (source has since moved to %s)\n' "$s" "$dv" "$now"
            else
                printf '  ok       %-26s %s\n' "$s" "$dv"
            fi
        else
            # Compare the numbers. Only treating is_called=false as "behind" let a
            # copy sitting at 5:true pass against a snapshot of 50:true - which
            # collides on the very next insert, which is the entire point.
            # %% strips the longest *suffix* and ## the longest *prefix, so the
            # number before the colon is ${v%%:*} and the flag after it is
            # ${v##*:}. Using ## for the number gave "true", the numeric test
            # errored, 2>/dev/null swallowed it, and the copy was reported ok.
            local sl dl sc dc
            sl=${expected%%:*}; dl=${dv%%:*}
            sc=${expected##*:}; dc=${dv##*:}
            # A non-numeric value is not something to guess about.
            if ! [[ "$sl" =~ ^[0-9]+$ && "$dl" =~ ^[0-9]+$ ]]; then
                printf '  ERROR    %-26s snapshot=%s copy=%s (unreadable position)\n' \
                    "$s" "$expected" "$dv"
                failed=1
            elif [ "$dl" -lt "$sl" ]; then
                printf '  BEHIND   %-26s snapshot=%s copy=%s\n' "$s" "$expected" "$dv"
                failed=1
            elif [ "$dl" -eq "$sl" ] && [ "$dc" = "false" ] && [ "$sc" = "true" ]; then
                # Same number, but never handed out in the copy: the next insert
                # reuses a value that already exists.
                printf '  BEHIND   %-26s snapshot=%s copy=%s (never called)\n' "$s" "$expected" "$dv"
                failed=1
            else
                # Ahead of the snapshot is fine: rows landed after the copy ran.
                printf '  ok       %-26s %s (snapshot was %s)\n' "$s" "$dv" "$expected"
            fi
        fi
    done <<<"$(owned_sequences)"
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
          AND tablename NOT IN ($(portal_table_array_literal), '$MANIFEST_TABLE')
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
# Recorded BEFORE the dump, so it describes the source at the moment the dump
# read it. The dump runs immediately after.
MANIFEST_TSV="$(mktemp)"
capture_manifest "$MANIFEST_TSV"

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

echo "-- recording what the source held at copy time"
if ! load_manifest "$MANIFEST_TSV"; then
    echo "WARNING: could not write $MANIFEST_TABLE. Without it, verify has"
    echo "nothing trustworthy to compare against and will refuse to pass."
    # Not fatal here: the dump itself succeeded. Stage 2 will say so plainly
    # rather than passing on a comparison it cannot make.
fi
rm -f "$MANIFEST_TSV"


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
