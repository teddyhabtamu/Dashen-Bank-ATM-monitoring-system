#!/usr/bin/env bash
# Exercises split_portal_db.sh's verify steps against a real postgres.
#
# These three checks had all been wrong in ways that only a live server could
# reveal:
#
#   * it called Zabbix's changelog/proxy sequences a failure, so a byte-perfect
#     copy reported BEHIND and exited non-zero on a good database
#   * it read sequences through pg_sequences, where last_value is NULL until a
#     sequence has been called - so a MISSING sequence scored the same as an
#     empty one and passed
#   * nothing checked whether the copy had picked up tables it should not have,
#     which is exactly what happens when the pg_dump flag list resolves empty
#
# None of that was caught by pytest, because pytest never ran a pg_dump. So the
# fixture here builds a source database shaped like the real one - portal tables
# with serial columns, sitting in the same database as Zabbix's own tables - and
# runs the real functions against it.
#
# Usage: tests/test_split_verify.sh <pgbin-dir> <port> <socket-dir>
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PGBIN="${1:?pg bin dir, e.g. /usr/lib/postgresql/18/bin}"
PORT="${2:-55432}"
SOCK="${3:-/tmp/opencode/pgtest}"
export PGHOST="$SOCK" PGPORT="$PORT" PGUSER=zabbix

FIXTURE_DIR=/tmp/opencode/pgfixture
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }

psql_q()   { "$PGBIN/psql" -X -q -tAc "$1" -d "${2:-zabbix}" 2>/dev/null; }
# stderr kept, so a failure can actually be seen
psql_raw() { "$PGBIN/psql" -X -q -tAc "$1" -d "${2:-zabbix}" 2>&1; }

# The five portal tables this fixture creates. The real list has 18; the verify
# logic does not care how many, only which ones it is comparing.
FIXTURE_TABLES=(atm_transactions atm_anomalies scheduled_reports portal_schema_version txn_source_policy)

reset_fixture() {
    "$PGBIN/psql" -X -q -d postgres -c "DROP DATABASE IF EXISTS zabbix;" >/dev/null 2>&1
    "$PGBIN/psql" -X -q -d postgres -c "DROP DATABASE IF EXISTS portal;"  >/dev/null 2>&1
    "$PGBIN/psql" -X -q -d postgres -c "CREATE DATABASE zabbix;"           >/dev/null 2>&1
    "$PGBIN/psql" -X -q -d zabbix >/dev/null 2>&1 <<SQL
CREATE TABLE atm_transactions (id serial PRIMARY KEY, amount numeric);
CREATE TABLE atm_anomalies (id serial PRIMARY KEY, atm_id int);
CREATE TABLE scheduled_reports (id serial PRIMARY KEY, name text);
CREATE TABLE portal_schema_version (version int);
CREATE TABLE txn_source_policy (source text PRIMARY KEY);
-- Zabbix's own tables, in the same database, deliberately NOT copied.
CREATE TABLE changelog (changelogid serial PRIMARY KEY, objectid int);
CREATE TABLE proxy_history (proxy_history_id serial PRIMARY KEY, hostid int);
CREATE TABLE proxy_dhistory (proxy_dhistory_id serial PRIMARY KEY);
CREATE TABLE proxy_autoreg_host (proxy_autoreg_host_id serial PRIMARY KEY);
INSERT INTO atm_transactions(amount) SELECT 1 FROM generate_series(1,50);
INSERT INTO atm_anomalies(atm_id) SELECT 1 FROM generate_series(1,10);
INSERT INTO scheduled_reports(name) VALUES ('r1'),('r2');
INSERT INTO changelog(objectid) SELECT 1 FROM generate_series(1,200);
SQL
}

# copy_fixture <target-table-list-file>
copy_fixture() {
    "$PGBIN/psql" -X -q -d postgres -c "CREATE DATABASE portal;" >/dev/null 2>&1
    local flags="" t
    for t in "${FIXTURE_TABLES[@]}"; do flags="$flags -t $t"; done
    # shellcheck disable=SC2086
    "$PGBIN/pg_dump" --no-owner --no-acl $flags zabbix \
        | "$PGBIN/psql" -X -q -d portal >/dev/null 2>&1
}

# Drive the real script's functions with its database calls redirected at the
# fixture. Everything below "source" is the shipped code, unmodified.
load_script() {
    PORTAL_TABLES_FILE="$1"
    # shellcheck source=/dev/null
    source "$ROOT/scripts/split_portal_db.sh"
    run_psql()  { "$PGBIN/psql"  -X -U zabbix "$@"; }
    run_pg_dump() { "$PGBIN/pg_dump" -U zabbix "$@"; }
    SOURCE_DB=zabbix
    TARGET_DB=portal
    DB_USER=zabbix
}

printf 'split_portal_db.sh verify steps, against a real postgres\n\n'

cat > "$FIXTURE_DIR" <<'EOF'
atm_transactions
atm_anomalies
scheduled_reports
portal_schema_version
txn_source_policy
EOF

# ── 1. a good copy must pass, and must not mention Zabbix's sequences ─────────
reset_fixture
copy_fixture
load_script "$FIXTURE_DIR"
out=$(verify_sequences); rc=$?
if [ "$rc" -eq 0 ] && grep -q '^  ok  *atm_' <<<"$out"; then
    ok "verify_sequences passes on a correct copy, having actually compared it"
else
    bad "verify_sequences did not really compare: rc=$rc out=$out"
fi
if ! grep -q changelog <<<"$out"; then ok "Zabbix's changelog sequence is not judged"
else bad "changelog_changelogid_seq was judged - this is the UAT false alarm"; fi
if ! grep -q 'proxy_' <<<"$out"; then ok "Zabbix's proxy sequences are not judged"
else bad "proxy_* sequences were judged"; fi

# ── 2. a sequence that never got copied must be reported MISSING ──────────────
# The column default owns the sequence, so the default has to go first -
# otherwise DROP silently refuses and the MISSING branch is never reached.
psql_q "ALTER TABLE atm_transactions ALTER COLUMN id DROP DEFAULT;" portal >/dev/null
psql_q "DROP SEQUENCE atm_transactions_id_seq;" portal >/dev/null
if [ -n "$(psql_q "SELECT to_regclass('public.atm_transactions_id_seq');" portal)" ]; then
    bad "fixture could not drop the sequence - MISSING branch untested"
else
    ok "fixture removed the sequence from the copy"
fi
out=$(verify_sequences); rc=$?
if [ "$rc" -ne 0 ] && grep -q MISSING <<<"$out"; then
    ok "a sequence absent from the copy is reported MISSING, not silently ok"
else
    bad "absent sequence not caught: rc=$rc out=$out"
fi
reset_fixture; copy_fixture

# ── 3. a sequence left behind must be reported BEHIND ─────────────────────────
psql_q "SELECT setval('atm_transactions_id_seq', 5, true);" portal >/dev/null
out=$(verify_sequences); rc=$?
if [ "$rc" -ne 0 ] && grep -q BEHIND <<<"$out"; then
    ok "a sequence left at 5 while the source is at 50 is reported BEHIND"
else
    bad "lagging sequence not caught: rc=$rc out=$out"
fi
# this is the duplicate-key failure, so assert the collision is real
collided=$(psql_raw "INSERT INTO atm_transactions(amount) VALUES (1) RETURNING id;" portal)
if echo "$collided" | grep -qi "duplicate key"; then
    ok "and that really does collide, as the check claims"
else
    bad "expected a duplicate key on insert, got: $collided"
fi
reset_fixture; copy_fixture

# ── 4. the copy must be proven to contain nothing extra ───────────────────────
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -eq 0 ] && grep -q 'all expected' <<<"$out"; then
    ok "verify_no_zabbix_leakage passes on a scoped copy"
else
    bad "leak check did not really run: rc=$rc out=$out"
fi

# now simulate the empty-flag-list disaster: whole database copied
psql_q "CREATE TABLE changelog (changelogid serial PRIMARY KEY, objectid int);" portal >/dev/null
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -ne 0 ] && grep -q LEAK <<<"$out"; then
    ok "a copy containing Zabbix tables is caught"
else
    bad "Zabbix leakage not caught: rc=$rc out=$out"
fi

# ── 5. row-count comparison still works both ways ────────────────────────────
psql_q "DELETE FROM atm_anomalies WHERE id > 7;" portal >/dev/null
out=$(verify); rc=$?
if [ "$rc" -ne 0 ] && grep -q MISMATCH <<<"$out"; then
    ok "verify catches a table whose rows do not match"
else
    bad "row mismatch not caught: rc=$rc out=$out"
fi

# ── 6. an unreadable database must NOT be reported as a pass ─────────────────
# This one was live: both sides came back as the same placeholder, string
# equality said "match", and a copy nobody could read was announced as
# "All tables match". Point TARGET_DB at a database that does not exist.
# BOTH sides unreadable, which is what a stopped postgres container looks like.
# Breaking only one side still shows a mismatch, so it does not exercise this.
SOURCE_DB=no_such_database
TARGET_DB=no_such_database
out=$(verify); rc=$?
if [ "$rc" -ne 0 ] && grep -q 'ERROR' <<<"$out" && ! grep -q 'All .* tables match' <<<"$out"; then
    ok "an unreachable database is reported as an error, never as a match"
else
    bad "unreachable database silently passed: rc=$rc out=$out"
fi
SOURCE_DB=zabbix
TARGET_DB=portal

# and an empty table list must not come back clean either
PORTAL_TABLES_FILE=/tmp/opencode/_empty_list
: > /tmp/opencode/_empty_list
out=$(verify); rc=$?
if [ "$rc" -ne 0 ]; then ok "an empty table list is a failure, not a clean bill"
else bad "empty table list passed: $out"; fi
PORTAL_TABLES_FILE="$FIXTURE_DIR"

# ── 7. sql_of must not swallow the caller's stdin ────────────────────────────
# The real run_psql forwards stdin with `docker exec -i` and psql consumes it.
# Inside a `while read` loop that ate the remaining table names: the UAT verify
# compared ONE table of eighteen, printed "All 1 tables match" and exited 0.
#
# This overrides run_psql with something that deliberately eats stdin, the way
# psql does, so the isolation in sql_of is what is under test - not the local
# psql, which would never have shown the problem.
run_psql() { cat >/dev/null; printf '1\n'; }
n=0
while read -r _t; do n=$((n+1)); sql_of zabbix "SELECT 1;" >/dev/null; done \
    < <(printf 'a\nb\nc\nd\ne\n')
if [ "$n" -eq 5 ]; then
    ok "sql_of does not consume the caller's stdin"
else
    bad "sql_of ate the loop's stdin: read $n of 5 lines"
fi

# And the guard that turns a short loop into a failure. Simulating it by making
# run_psql eat stdin no longer works - that is the bug that is now fixed, so the
# loop runs to completion. Instead, shorten the list itself: portal_table_names
# yields one name while portal_table_count still reports five, which is exactly
# what "the loop was cut short" means.
# portal_table_count is derived from portal_table_names, so overriding only the
# names would leave the expected count at 1 too and the guard would never see a
# discrepancy. Pin both, to what a cut-short loop actually looks like.
portal_table_names() { printf 'atm_transactions\n'; }
portal_table_count() { printf '5\n'; }
out=$(verify); rc=$?
if [ "$rc" -ne 0 ] && grep -q 'cut short' <<<"$out"; then
    ok "a verify that skipped tables fails instead of reporting a match"
else
    bad "short loop not caught: rc=$rc out=$out"
fi
unset -f portal_table_names portal_table_count
load_script "$FIXTURE_DIR"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]