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
    MANIFEST_TSV="$(mktemp)"
    load_script "$FIXTURE_DIR"
    capture_manifest "$MANIFEST_TSV"
    # shellcheck disable=SC2086
    "$PGBIN/pg_dump" --no-owner --no-acl $flags zabbix \
        | "$PGBIN/psql" -X -q -d portal >/dev/null 2>&1
    load_manifest "$MANIFEST_TSV"
    rm -f "$MANIFEST_TSV"
}

# Drive the real script's functions with its database calls redirected at the
# fixture. Everything below "source" is the shipped code, unmodified.
load_script() {
    PORTAL_TABLES_FILE="$1"
    # shellcheck source=/dev/null
    source "$ROOT/scripts/split_portal_db.sh"
    run_psql()  { "$PGBIN/psql"  -X -U zabbix "$@"; }
    # load_manifest writes through run_psql_stdin. Overriding only run_psql left
    # it calling docker, so the manifest was never written and every comparison
    # had nothing to check against.
    run_psql_stdin() { "$PGBIN/psql" -X -U zabbix "$@"; }
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
psql_q "DROP TABLE changelog;" portal >/dev/null

# And a stray VIEW, which pg_tables cannot see at all. The check used
# pg_tables, so it reported "all expected" on UAT while counting a view it
# could not see, and any Zabbix view in the copy would have passed unnoticed.
psql_q "CREATE TABLE zbase (id serial PRIMARY KEY, v int);" portal >/dev/null
psql_q "INSERT INTO zbase(v) SELECT 1;" portal >/dev/null
psql_q "CREATE VIEW zabbix_some_view AS SELECT id FROM zbase;" portal >/dev/null
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -ne 0 ] && grep -q LEAK <<<"$out"; then
    ok "a stray view in the copy is caught, not just stray tables"
else
    bad "stray view not caught: rc=$rc out=$out"
fi
psql_q "DROP VIEW zabbix_some_view; DROP TABLE zbase;" portal >/dev/null

# The count must include views, or it understates what was copied. The view has
# to be one the list actually claims, otherwise LEAK fires - correctly - and the
# count line is never printed.
cp "$FIXTURE_DIR" "$FIXTURE_DIR.bak"
printf 'atm_portal_view\n' >> "$FIXTURE_DIR"
load_script "$FIXTURE_DIR"
psql_q "CREATE VIEW atm_portal_view AS SELECT 1 AS x;" portal >/dev/null
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -eq 0 ] && grep -qE '1 view' <<<"$out"; then
    ok "the reported count includes views"
else
    bad "view not counted: rc=$rc out=$out"
fi
psql_q "DROP VIEW atm_portal_view;" portal >/dev/null
mv "$FIXTURE_DIR.bak" "$FIXTURE_DIR"
load_script "$FIXTURE_DIR"

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

# ── 8. staleness must pass; corruption must still fail ───────────────────────
# This is the UAT failure of 2026-10-18. network-correlator writes to
# atm_network_metrics every 120 seconds, so by the time verify ran, a
# byte-perfect copy was 57 rows behind - with the row delta and the max(id)
# delta identical, so provably just staleness - and verify called it
# "NOT usable". Demanding equality against a source that is still being written
# to means the check fails on every healthy system, and a check that always
# fails is one people stop reading.
reset_fixture; copy_fixture
psql_q "INSERT INTO atm_transactions(amount) SELECT 1 FROM generate_series(1,7);" zabbix
out=$(verify); rc=$?
if [ "$rc" -eq 0 ] && grep -q 'source has since grown' <<<"$out"; then
    ok "a copy that is merely behind a live source still verifies"
else
    bad "staleness treated as corruption: rc=$rc out=$out"
fi

# The other half: being lenient must not mean being blind.
psql_q "DELETE FROM atm_transactions WHERE id > 45;" portal
out=$(verify); rc=$?
if [ "$rc" -ne 0 ] && grep -q MISMATCH <<<"$out"; then
    ok "a copy that really lost rows still fails"
else
    bad "real row loss not caught: rc=$rc out=$out"
fi

# and a copy with no manifest has nothing trustworthy to compare against
psql_q "DROP TABLE portal_split_manifest;" portal >/dev/null
out=$(verify); rc=$?
if [ "$rc" -ne 0 ] && grep -qi 'manifest' <<<"$out"; then
    ok "a copy with no manifest refuses to pass rather than guessing"
else
    bad "missing manifest not caught: rc=$rc out=$out"
fi

# ── 9. a query that ERRORS must never look like an empty result ──────────────
# I introduced this one myself: the leakage query built a string with
# c.relname || c.relkind, but relkind is of type "char", so text||"char" is
# ambiguous and the query fails. stderr was discarded, so the query returned
# nothing, nothing is also what a clean copy returns, and the copy was reported
# as "all expected" by a check that had not run.
psql_q "CREATE TABLE zz_leak (id serial PRIMARY KEY);" portal >/dev/null
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -ne 0 ] && grep -q LEAK <<<"$out"; then
    ok "a real leak is still reported after the error-handling change"
else
    bad "leak detection regressed: rc=$rc out=$out"
fi
psql_q "DROP TABLE zz_leak;" portal >/dev/null

# Now make the queries genuinely fail, which is what the type error did. Every
# query errors at once: if any of them is allowed to look like an empty result,
# the copy reads as clean.
run_psql() { "$PGBIN/psql" -X -U zabbix "$@" -c 'SELECT nonexistent_column_xyz FROM nowhere;' >/dev/null 2>&1; return 1; }
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -ne 0 ] && ! grep -q 'all expected' <<<"$out"; then
    ok "a query that errors is reported as an error, not as a clean copy"
else
    bad "a broken query still reads as clean: rc=$rc out=$out"
fi
# and verify() must refuse too, rather than comparing markers as if they were counts
out=$(verify); rc=$?
if [ "$rc" -ne 0 ] && ! grep -qE 'All [0-9]+ tables match' <<<"$out"; then
    ok "verify() also refuses when its queries fail"
else
    bad "verify() passed on failing queries: rc=$rc out=$out"
fi
unset -f run_psql
# Put the real runner back. Without this, run_psql is simply undefined, every
# query fails, and the tests below pass for the wrong reason - which is exactly
# how they passed while the swallow-errors version was still in place.
load_script "$FIXTURE_DIR"

# The precise regression, and the one that actually happened. A REAL leak is
# sitting in the copy, and the query that would report it is broken - exactly
# the state after the relkind type error, where the leak list came back empty
# and the copy was called clean. Only the leak query fails; the count queries
# still work, so nothing else gives the failure away.
#
# Self-contained: it resets and re-copies first. Relying on whatever the tests
# above left behind is how this assertion ended up passing while the leak table
# it was checking for did not exist at all.
reset_fixture; copy_fixture
psql_q "CREATE TABLE zz_real_leak (id serial PRIMARY KEY);" portal >/dev/null

load_script "$FIXTURE_DIR"
# No "is the leak table actually there" pre-check. The assertion at the end -
# the same copy flagged as leaking once the query works - is that proof, and a
# pre-check of its own just tested whatever state the tests above left behind.
# Call psql directly: a helper saved as "run_psql() { run_psql; }" recurses into
# the override it was meant to preserve, so every query fails and the assertion
# below passes for entirely the wrong reason. That happened once already.
# Match on "relname ||" - the concatenation that only the leak list does. A
# looser pattern on "relname" also matches every count query, because they all
# join pg_namespace on c.relnamespace, and "relnamespace" contains "relname".
# That made the whole check fail for the wrong reason and this test pass while
# proving nothing.
run_psql() {
    case " $* " in
        *"relname ||"*) return 1 ;;             # the leak list, broken
        *) "$PGBIN/psql" -X -U zabbix "$@" ;;
    esac
}
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -ne 0 ] && ! grep -q 'all expected' <<<"$out"; then
    ok "a broken leak query cannot certify a copy that does leak"
else
    bad "a leaking copy was certified by a broken query: rc=$rc out=$out"
fi
# sanity: with a working runner the very same copy must be flagged as leaking
unset -f run_psql
load_script "$FIXTURE_DIR"
out=$(verify_no_zabbix_leakage); rc=$?
if [ "$rc" -ne 0 ] && grep -q LEAK <<<"$out"; then
    ok "and the same copy is flagged as leaking once the query works"
else
    bad "leak not detected with a working query: rc=$rc out=$out"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]