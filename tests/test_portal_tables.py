"""config/portal_tables.txt is the single list of record, and it has to stay so.

Two scripts depend on it: scripts/backup_db.sh builds the table-scoped dump, and
scripts/split_portal_db.sh copies the tables into a separate database. They each
carried their own list once, and drifted - the scoped backup was missing
txn_source_policy, which decides whether simulated transactions are on screen, so
restoring from it would have quietly re-shown invented money. The full dump hid
the gap entirely, which is why it survived.

The second test here guards a mistake actually made while writing the fix: a
hand-edited block in backup_db.sh pointed the SCOPED dump at the FULL dump's
filename. Every nightly full backup would have been replaced by a partial one,
silently, at 02:00 - and nothing in the file would have said so.
"""
import os
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
LIST_FILE = REPO / 'config' / 'portal_tables.txt'
BACKUP = REPO / 'scripts' / 'backup_db.sh'

# Zabbix creates and owns these. They must never be copied into the portal
# database, so a false positive from them would be actively harmful.
ZABBIX_OWNED = {
    'users', 'hosts', 'items', 'interfaces', 'triggers', 'config', 'functions',
    'media', 'images', 'history', 'trends', 'problems', 'events', 'sessions',
    'queue', 'data_cache', 'usermacro', 'dashboard', 'map', 'application',
    'autohost', 'maintenance', 'housekeeping', 'escalations', 'actions',
    'scripts', 'scriptoutput', 'alert_template', 'event_tag',
}

TABLE_NAME = r'[a-z_][a-z0-9_]*'


def listed_tables():
    out = []
    for line in LIST_FILE.read_text().split('\n'):
        line = line.split('#')[0].strip()
        if line:
            out.append(line)
    return out


def repo_create_table_statements():
    """Every table the repo creates, with the file it came from.

    Two passes, because one is not enough. The obvious regex over CREATE TABLE
    misses tables built from a name constant:

        TABLE = 'fleet_report_cache'
        ...
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("

    fleet_report_cache is exactly such a table, and a mutation test confirmed
    that with only the first pass, deleting it from the list of record passed
    every test. That is the worst possible failure mode: nobody would notice the
    table had quietly stopped being backed up, and the cache is part of the
    portal's data. So the second pass picks up name constants.
    """
    found = {}

    def record(name, f):
        name = name.lower()
        if name in ZABBIX_OWNED or name in ('as', 'if', 'is', 'would', 'not', 'table'):
            return
        found.setdefault(name, set()).add(str(f.relative_to(REPO)))

    # Root-level *.py matters as much as anything else: cash_watcher.py,
    # anomaly_detector.py and network_correlator.py live there, and omitting
    # them is how atm_cassette_levels went missing from the list for a whole
    # commit while the test suite stayed green.
    globs = ('*.py', 'config/postgres/*.sql', 'report-portal/*.py',
             'report-portal/blueprints/*.py', 'scripts/*.sql', 'scripts/*.py',
             'simulators/*.py', 'zabbix_modules/*.py')

    literal = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                         r'(?:\{?[A-Z_]+\}?\.)?(%s)' % TABLE_NAME, re.I)
    # A view the portal queries is just as load-bearing as a table, and the same
    # -t flag copies it - checked against a real pg_dump rather than assumed.
    view = re.compile(r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+'
                      r'(?:\{?[A-Z_]+\}?\.)?(?:public\.)?(%s)' % TABLE_NAME, re.I)
    # A module-level constant holding a table name: TABLE = 'fleet_report_cache'
    const = re.compile(r'^\s*(?:[A-Z][A-Z0-9_]*_)?TABLE\s*=\s*[\'"](%s)[\'"]'
                       % TABLE_NAME, re.I | re.M)
    for glob in globs:
        for f in sorted(REPO.glob(glob)):
            if 'split_portal_db' in f.name:
                continue      # our own migration consumer, not a source of truth
            text = f.read_text(errors='replace')
            for m in literal.finditer(text):
                record(m.group(1), f)
            for m in view.finditer(text):
                record(m.group(1), f)
            for m in const.finditer(text):
                record(m.group(1), f)
    return found


def test_the_list_is_not_empty_and_has_no_junk():
    tables = listed_tables()
    assert len(tables) >= 15, tables
    for t in tables:
        assert re.fullmatch(TABLE_NAME, t), f'{t!r} is not a bare table name'
        assert t not in ZABBIX_OWNED, f'{t} belongs to Zabbix, not to the portal'


def test_the_list_has_no_duplicates():
    tables = listed_tables()
    assert len(tables) == len(set(tables)), 'duplicates in the list of record'


def test_no_portal_table_is_missing_from_the_list():
    """The test that matters: a new table created anywhere must be added here,
    or it is neither backed up nor split out."""
    found = repo_create_table_statements()
    listed = set(listed_tables())
    missing = sorted(set(found) - listed)
    assert not missing, (
        'these tables are created in the repo but missing from '
        'config/portal_tables.txt, so they would be neither backed up nor copied '
        'into the portal database:\n  %s\n  add them to the list.'
        % '\n  '.join('%s (in %s)' % (m, ', '.join(sorted(found[m]))) for m in missing))


# Artifacts that legitimately mention a table name without creating it: the
# list itself, this test, and the migration that consumes the list.
OWN_ARTIFACTS = ('config/portal_tables.txt', 'tests/test_portal_tables.py',
                 'scripts/split_portal_db.sh')


def mentioned_outside_own_artifacts(name):
    """Is this table name referenced anywhere that is not our own bookkeeping?

    CREATE TABLE detection alone is not enough: fleet_report_cache is created as
    f"CREATE TABLE IF NOT EXISTS {TABLE} (" with TABLE assigned a few lines
    earlier, so the literal never sits beside CREATE TABLE. A broad reference
    check catches those without false-flagging anything, because the files that
    merely mention a name in passing are excluded.
    """
    for glob in ('*.py', 'config/postgres/*.sql', 'report-portal/*.py',
                 'report-portal/blueprints/*.py', 'scripts/*.py',
                 'simulators/*.py', 'zabbix_modules/*.py'):
        for f in sorted(REPO.glob(glob)):
            if str(f.relative_to(REPO)) in OWN_ARTIFACTS:
                continue
            if re.search(r'\b%s\b' % re.escape(name),
                          f.read_text(errors='replace'), re.I):
                return True
    return False


def test_the_list_has_no_tables_that_do_not_exist():
    """The mirror image of the drift test, and it catches a mistake I made twice:
    atm_devices and atm_incidents were invented and put in the backup list. A
    dump flag for a table that does not exist is silently ignored, so the backup
    quietly covers less than it appears to, and nothing says so."""
    invented = [t for t in listed_tables() if not mentioned_outside_own_artifacts(t)]
    assert not invented, (
        'these are in config/portal_tables.txt but nothing in the repo mentions '
        'them - they were removed, or they were never real:\n  %s'
        % '\n  '.join(invented))


def test_backup_and_split_share_the_list():
    """Neither script may carry its own copy of the table names."""
    for f in ('scripts/backup_db.sh', 'scripts/split_portal_db.sh'):
        src = (REPO / f).read_text()
        assert 'portal_tables.sh' in src, f'{f} must read the shared list'
        # A hand-maintained list is the drift we are eliminating.
        hand = re.findall(r'^\s*-t\s+([a-z_]+)\s*\\?\s*$', src, re.M)
        assert not hand, (
            '%s still hardcodes table names: %s' % (f, hand))


def test_no_two_dumps_write_to_the_same_file():
    """Guards a mistake actually made while writing this fix: a hand-edited
    block pointed the SCOPED dump at the FULL dump's filename. Every nightly full
    backup would have been replaced by a partial one, silently, at 02:00, and
    nothing in the file or the logs would have said so.

    One target per line, because "| gzip > X" also contains "> X" and a naive
    scan of the whole text counts the full dump twice - which is why the first
    version of this test failed for the wrong reason.
    """
    src = BACKUP.read_text()
    targets = {}
    for line in src.split('\n'):
        for m in re.finditer(r'>\s*"\$\{?BACKUP_DIR\}?/([^"]+)"', line):
            name = m.group(1)
            targets.setdefault(name, []).append(line.strip())
    assert targets, 'no dump targets found in backup_db.sh'
    # glpi_ legitimately appears twice: a fallback branch for two mariadb paths.
    dupes = {n: ls for n, ls in targets.items()
             if len(ls) > 1 and not n.startswith('glpi')}
    assert not dupes, ('two different dumps share an output path:\n  %s'
                       % '\n  '.join('%s -> %s' % (n, ls) for n, ls in dupes.items()))
    assert 'zabbix_full_$DATE.sql.gz' in targets, 'the full dump must still exist'
    assert 'atm_custom_tables.sql' in targets, 'the custom-tables dump must still exist'


def code_without_comments(path):
    """Drop comment lines before matching, so a comment explaining WHY a
    construct is banned does not read as the construct itself - which is exactly
    how this test failed on first run."""
    out = []
    for line in path.read_text().split('\n'):
        stripped = line.strip()
        if stripped.startswith('#') or not stripped:
            continue
        out.append(line.split('  #')[0])
    return '\n'.join(out)


def test_the_split_cannot_lose_constraints():
    """CREATE TABLE AS copies columns and nothing else - no primary keys, no
    defaults, no indexes - and the next insert fails on a duplicate key weeks
    later with nothing in the log to explain it."""
    src = code_without_comments(REPO / 'scripts' / 'split_portal_db.sh')
    # Precisely CTAS, not any CREATE TABLE. The script legitimately creates its
    # own portal_split_manifest table; what it must never do is copy DATA with
    # CREATE TABLE AS, which drops keys, defaults, indexes and sequences.
    assert not re.search(r'CREATE\s+TABLE[^;]*?\bAS\b', src, re.I), (
        'the split must use pg_dump, not CREATE TABLE AS')
    assert 'pg_dump' in src
    assert '--no-owner' in src and '--no-acl' in src, (
        'the copy must not inherit role-specific privileges')


def test_the_split_verifies_before_claiming_success():
    src = (REPO / 'scripts' / 'split_portal_db.sh').read_text()
    assert 'MISMATCH' in src, 'the comparison must be able to report a mismatch'
    assert '--verify' in src, 'verification must be re-runnable on its own'
    assert src.count('verify') >= 3


def test_the_split_never_touches_the_source_database():
    """Stages 1 and 2 create and populate a NEW database. Nothing may drop,
    truncate or alter the one the portal is running against."""
    src = (REPO / 'scripts' / 'split_portal_db.sh').read_text()
    drop_lines = [l for l in src.split('\n') if 'DROP DATABASE' in l]
    for l in drop_lines:
        assert 'IF EXISTS $TARGET_DB' in l, (
            'DROP must only ever target the copy: %s' % l.strip())
    assert 'TRUNCATE' not in src.upper(), 'the split must never truncate'
    # Deleting from the manifest to reload it is fine. Deleting from a portal
    # table would destroy the very data being verified.
    deletes = re.findall(r'DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_$]*)', src)
    for t in deletes:
        assert t == 'MANIFEST_TABLE', (
            'the split must never delete from %s' % t)
    assert not re.search(r'DELETE\s+FROM\s+(?!\$MANIFEST_TABLE)', src, re.I), (
        'the only DELETE allowed is the one clearing the manifest')
    assert 'DROP DATABASE' in src, '--drop must remain available'


def test_query_runner_does_not_forward_stdin():
    """`docker exec -i` hands psql the caller's stdin, and psql consumes it.

    Inside the `while read` loops in verify(), that ate the remaining table
    names: the UAT verify compared ONE table of eighteen, printed "All 1 tables
    match" and exited 0 - on 2026-10-18, straight after a run that had correctly
    compared all eighteen. I introduced the -i myself in 699bf18.

    The shell harness cannot catch this one, because it overrides run_psql to
    point at a local postgres, so the shipped definition is never exercised.
    Hence a source-level check as well.
    """
    src = (REPO / 'scripts' / 'split_portal_db.sh').read_text()
    m = re.search(r'^run_psql\(\)\s*\{(.*)\}', src, re.M)
    assert m, 'run_psql must still be defined and overridable'
    assert '-i' not in m.group(1), (
        'run_psql must not forward stdin: psql will swallow the verify loops\' '
        'input and the run will compare a fraction of the tables')
    # The restore genuinely does read stdin, from the dump pipe.
    assert re.search(r'^run_psql_stdin\(\)\s*\{.*-i', src, re.M), (
        'the restore reads a dump from a pipe and needs stdin forwarded')
    assert 'run_psql_stdin -d "$TARGET_DB"' in src, 'the restore must use the stdin runner'


def test_verify_refuses_to_pass_on_a_partial_comparison():
    """The script must not be able to report success after checking fewer tables
    than its own list names - that is the failure that produced a clean-looking
    run over one table."""
    src = code_without_comments(REPO / 'scripts' / 'split_portal_db.sh')
    assert 'cut short' in src, 'a short comparison loop must be an explicit failure'
    assert re.search(r'checked.*-ne.*expected_total', src), (
        'the compared count must be checked against the expected count')


def test_the_split_checks_sequences():
    """Row counts matching is not the same as a usable copy. A sequence that was
    not carried across leaves the copy looking perfect until the next insert,
    which then fails with a duplicate key on a primary key that already exists.

    It reads the sequence directly rather than through pg_sequences, because
    pg_sequences reports last_value as NULL for a sequence that has never been
    called - so a missing sequence and an empty one look identical, and the
    check would pass on the very thing it exists to catch."""
    src = code_without_comments(REPO / 'scripts' / 'split_portal_db.sh')
    assert 'last_value' in src, 'sequence positions must be compared'
    assert 'is_called' in src
    assert 'to_regclass' in src, 'a missing sequence must be distinguishable from an empty one'
    assert 'verify_sequences' in src


def test_the_split_only_checks_sequences_it_owns():
    """The source database also holds Zabbix's own sequences - changelog and the
    proxy_* family - which are deliberately not copied. Checking every sequence
    in the schema reported those as BEHIND and made a byte-perfect copy look
    broken. That happened on UAT on 2026-10-17."""
    src = code_without_comments(REPO / 'scripts' / 'split_portal_db.sh')
    assert 'pg_depend' in src, 'ownership must come from pg_depend'
    assert "deptype = 'a'" in src, "only auto-owned (serial) sequences belong to a table"
    assert 'pg_sequences' not in src, (
        'pg_sequences enumerates Zabbix sequences too, which is the bug this '
        'replaced')


def test_the_split_proves_nothing_extra_was_copied():
    """If the flag list ever resolves empty, pg_dump dumps the whole database
    rather than nothing, and reports success while doing it."""
    src = code_without_comments(REPO / 'scripts' / 'split_portal_db.sh')
    assert 'verify_no_zabbix_leakage' in src
    assert 'NOT IN' in src, 'the copy must be checked for tables it should not have'


def test_the_split_aborts_rather_than_dumping_everything():
    """pg_dump with no -t flags does not dump nothing, it dumps the entire
    database. So an empty or short flag list has to stop the run, not produce a
    'scoped' dump that is silently the whole thing."""
    src = code_without_comments(REPO / 'scripts' / 'split_portal_db.sh')
    assert re.search(r'ABORT', src), 'the script must refuse to run on a short flag list'
    assert re.search(r'-t \$', src) or 'T_COUNT' in src, 'the flag count must be checked'


def test_the_list_is_read_even_without_a_trailing_newline():
    """Guards the order of sed then grep in portal_table_names.

    sed always newline-terminates its output, so it repairs a list file whose
    last line has no trailing newline - that is the only reason the following
    grep is safe. An earlier version of this test claimed a `while read` loop
    lost that table; a mutation test proved otherwise, because sed had already
    fixed it. The test stands as a guard against the pipeline being reordered or
    sed being dropped, either of which really would lose the last table.
    """
    # Written without a trailing newline on purpose.
    pathlib.Path('/tmp/opencode/_nonl.txt').write_text('alpha\nbravo\ncharlie')
    r = subprocess.run(
        ['bash', '-c',
         'source scripts/portal_tables.sh; portal_table_count; portal_table_names'],
        cwd=str(REPO), capture_output=True, text=True,
        env={**os.environ, 'PORTAL_TABLES_FILE': '/tmp/opencode/_nonl.txt'},
        timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split('\n')[0].strip() == '3', r.stdout
    assert [x for x in r.stdout.split('\n')[1:] if x] == ['alpha', 'bravo', 'charlie'], r.stdout


def test_flag_expansion_actually_produces_flags():
    """The first attempt used eval with nested quotes and produced ZERO flags -
    silently, because a pg_dump with no -t flags dumps the WHOLE database."""
    r = subprocess.run(
        ['bash', '-c',
         'source scripts/portal_tables.sh; '
         'F="$(portal_dump_flags | tr \'\\n\' \' \')"; '
         'printf "%s\\n" $F | grep -c "^-t$"'],
        cwd=str(REPO), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    count = int(r.stdout.strip())
    assert count == len(listed_tables()), (
        'flag expansion produced %d flags for %d tables' % (count, len(listed_tables())))
    assert count >= 15, 'a near-empty flag list means the dump silently widened'

def test_the_stage3_probe_covers_every_relation():
    """The probe is the read-only proof that the copy is usable, so a relation
    missing from it is a relation nobody checked before switching anything over.

    The manifest is exempt: it belongs to the split tooling, not to the portal.
    """
    probe = (REPO / 'scripts' / 'probe_portal_db.py').read_text()
    relations = [t for t in listed_tables() if t != 'portal_split_manifest']
    missing = [t for t in relations if t not in probe]
    assert not missing, (
        'these relations are copied but the probe never reads them, so nothing '
        'has exercised them against the new database:\n  %s'
        % '\n  '.join(missing))


def test_the_stage3_probe_is_read_only():
    """A probe that writes would perturb the copy and make the next --verify
    report a MISMATCH against the manifest, with no obvious culprit. It also
    has to prove the read-only claim rather than assume it."""
    probe = (REPO / 'scripts' / 'probe_portal_db.py').read_text()
    assert 'set_session(readonly=True' in probe, 'the connection must be read-only'
    assert 'transaction is read-only' in probe, (
        'the probe must attempt a write and confirm it is rejected')
    # It must not be able to fall back to the old database unnoticed.
    assert 'current_database()' in probe, (
        'the probe must report which database it actually reached')
    assert 'DB_NAME' in probe


def test_the_probe_refuses_to_call_a_vacuous_check_a_pass():
    """With the demo window closed, atm_transactions_visible returns 0 rows -
    and so does any join built on it. A broken view returns 0 too, so "ok 0
    rows" for those two checks is indistinguishable from a working one. That is
    precisely the false pass this probe exists to prevent, and on 2026-10-18
    the probe did print a bare ok for both."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'probe_portal_db', str(REPO / 'scripts' / 'probe_portal_db.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The live UAT case: 181265 raw rows, demo window closed, nothing visible.
    note = mod.view_evidence(181265, 0, 0)
    assert note, 'a view exposing nothing must not count as evidence'
    assert 'demo window' in note, 'the likely cause should be named'
    assert 'prove nothing' in note

    # A real fault, not a vacuous check: rows exist but never join.
    note = mod.view_evidence(100, 50, 0)
    assert note and 'real fault' in note, (
        'visible rows that do not join is a genuine mismatch, and must not be '
        'dismissed as vacuous')

    # Healthy: rows visible and joining.
    assert mod.view_evidence(100, 50, 50) == '', (
        'a working view with matching joins must not raise a note')

    # Nothing at all is legitimately inconclusive, but not alarming either.
    assert mod.view_evidence(0, 0, 0) == '', (
        'an empty database is not evidence of anything either way')


def test_backup_covers_both_databases_once_split():
    """The most dangerous thing about this split is not a broken page, it is a
    backup that looks complete and is missing everything.

    After the switch the portal's rows live in "portal" while Zabbix's own
    configuration stays in "zabbix". Dumping only zabbix would produce nightly
    files of a normal size, no errors in the log, and no portal data in any of
    them - discovered only when a restore is attempted.
    """
    src = (REPO / 'scripts' / 'backup_db.sh').read_text()
    assert 'zabbix_full_' in src, 'the Zabbix configuration dump must remain'
    assert 'portal_full_' in src, \
        'the portal database must get its own full dump once split'
    assert 'portal_full_*.sql.gz' in src, \
        'portal dumps need retention too, or they fill the disk'


def test_backup_finds_the_portal_database_instead_of_hardcoding_it():
    """backup_db.sh had nine hardcoded references to the zabbix database. Any
    of them left pointing at the old database quietly backs up the wrong
    tables - including a CREATE TABLE that would materialise an empty
    atm_delete_archive there."""
    src = (REPO / 'scripts' / 'backup_db.sh').read_text()
    assert 'PORTAL_DB=' in src, 'the portal database must be detected'
    assert 'portal_schema_version' in src, \
        'detection needs a marker that only exists with the portal tables'
    # Every COPY, scoped dump and verification query must use the detected name.
    assert '-d zabbix \\\n  -c "COPY' not in src, 'CSV exports must not be pinned'
    assert 'pg_dump -U zabbix zabbix --no-owner --no-acl $PORTAL_T_FLAGS' not in src, \
        'the scoped dump must not be pinned to zabbix'
    # Zabbix's own full dump must stay pinned - it must never follow the marker.
    assert 'pg_dump -U zabbix zabbix --no-owner --no-acl \\\n  | gzip > "$BACKUP_DIR/zabbix_full_' in src, \
        "Zabbix's own configuration dump must always come from zabbix"


def test_backup_refuses_to_guess_the_database():
    """A backup that covers the wrong database is worse than no backup, because
    nobody finds out until a restore. Detection failure must stop the run."""
    shared = (REPO / 'scripts' / 'portal_tables.sh').read_text()
    assert 'detect_portal_db' in shared, 'detection is shared, not duplicated'
    fn = shared[shared.index('detect_portal_db()'):]
    assert 'return 1' in fn, 'a failed detection must be reported as failure'

    src = (REPO / 'scripts' / 'backup_db.sh').read_text()
    assert 'detect_portal_db' in src, 'backup_db must use the shared detection'
    assert 'Refusing to guess' in src
    assert 'exit 1' in src, 'a failed detection must abort, not carry on'

    # restore_db.sh needs the same answer, and must not come to a different one.
    rst = (REPO / 'scripts' / 'restore_db.sh').read_text()
    assert 'detect_portal_db' in rst, (
        'restore_db.sh must detect the portal database too, or it restores into '
        'the wrong one')
    assert 'exit 1' in rst


def test_the_kpi_tile_labels_match_the_windows_they_query():
    """A label that disagrees with its query is a defect nobody can see.

    The Overview tile read "Transactions (7d)" while the query it renders used a
    seven-day rolling window. That was not a bug - the count was correct - but
    because rows entered and left the window at the same rate the number never
    changed, so it looked frozen and would have been reported as one. The tile
    and the interval are now tied together by this test, so changing one
    without the other fails the suite.
    """
    import re
    routes = (REPO / 'report-portal' / 'routes.py').read_text()
    template = (REPO / 'report-portal' / 'templates' / 'portal.html').read_text()

    m = re.search(r'Transactions \((\d+)([hd])\)', template)
    assert m, 'could not find the Transactions KPI tile label'
    amount, unit = int(m.group(1)), m.group(2)

    # The window the tile is fed by: /api/stats is the endpoint the tile polls.
    api = routes[routes.index("def api_stats()"):]
    api = api[:api.index("\n@") if "\n@" in api else len(api)]
    window = re.search(r"recorded_at >= NOW\(\) - INTERVAL '(\d+) (hour|day)s?'", api)
    assert window, 'could not find the time window in the api_stats query'
    hours = int(window.group(1)) * (24 if window.group(2).startswith('day') else 1)
    label_hours = amount * (24 if unit == 'd' else 1)

    assert label_hours == hours, (
        'the tile says Transactions (%d%s) but the query counts %d hours - the '
        'label would be lying about the number underneath it'
        % (amount, unit, hours))


def test_no_two_tracked_jobs_share_a_log_file():
    """A shared log lets one job's success hide another's failure.

    backup_db and backup_configs both appended to $HOME/atm-backup.log, running
    at 02:00 and 02:30. A failed database backup followed by a successful config
    backup leaves the log ending in success, so the failure is invisible until
    the data is needed. Since the split, backup_db.sh is the only thing covering
    the portal database - it is precisely the job whose failure must not be
    masked by an unrelated one succeeding afterwards.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'check_jobs', str(REPO / 'scripts' / 'check_jobs.py'))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pytest.skip('check_jobs.py runs on import')
    jobs = getattr(mod, 'JOBS', None)
    if not jobs:
        pytest.skip('check_jobs.py exposes no JOBS table to inspect')
    seen = {}
    for entry in jobs:
        name, log = entry[0], entry[1]
        assert log not in seen, (
            '%s and %s both write to %s - a failure in the first is hidden by '
            'the second succeeding afterwards' % (seen[log], name, log))
        seen[log] = name


def test_restore_uses_the_shared_list_not_a_hand_written_one():
    """The restore kept its own table list and it had drifted: five relations
    were missing from both its -t list and its DROP list. Those five would have
    been left holding stale data while the other fifteen were replaced - the
    view every page queries, the table deciding whether simulated transactions
    are shown, the cassette levels, the fleet cache and the schema version."""
    src = (REPO / 'scripts' / 'restore_db.sh').read_text()
    assert 'portal_table_count' in src and 'portal_table_names' in src, \
        'the restore must read config/portal_tables.txt'
    hand = re.findall(r'-t ([a-z_]+)', src)
    assert not hand, 'the restore still hardcodes table names: %s' % hand
    drops = re.findall(r'DROP TABLE IF EXISTS ([a-z_]+)', src)
    assert not drops, ('dropping tables one by one is how five relations were '
                       'left behind holding stale data: %s' % drops)
    assert 'DROP DATABASE IF EXISTS' in src, \
        'the database is replaced wholesale, which cannot be half-done'


def test_restore_cannot_load_portal_data_into_the_zabbix_database():
    """The fault that would have quietly undone the split.

    atm_custom_tables.sql is written from whichever database holds the portal
    tables - after the split, that is portal. The old restore loaded it into
    zabbix. So on a switched host it would have put every portal table back
    into the Zabbix database and reported success throughout."""
    src = (REPO / 'scripts' / 'restore_db.sh').read_text()
    assert 'detect_portal_db' in src
    # the restore target must be the detected database, never a literal
    assert re.search(r'psql[^\n]*-d\s+"?zabbix"?[^\n]*(CREATE|DROP)', src) is None, \
        'a DROP/CREATE of the zabbix database must be behind the explicit ' \
        '--also-zabbix flag, not the default path'
    assert '--also-zabbix' in src, 'restoring zabbix wholesale needs its own flag'


def test_restore_checks_the_backup_before_destroying_anything():
    """Found by rehearsing the restore against a fixture: it dropped the
    database, loaded an empty file, and only noticed afterwards. The
    verification caught it and refused to claim success - but by then the data
    was gone. A truncated gzip fails halfway through loading, which is the worst
    possible moment to find out."""
    src = (REPO / 'scripts' / 'restore_db.sh').read_text()
    check = src.index('Checking the backup is usable')
    destroy = src.index('DROP DATABASE IF EXISTS')
    assert check < destroy, 'the backup must be validated before the DROP'
    assert 'gzip -t' in src, 'a corrupt archive must be caught, not discovered mid-load'
    assert '-s "$PORTAL_DUMP"' in src, 'a missing or empty backup must be caught'


def test_restore_fails_when_a_relation_is_missing():
    """The previous version printed row counts afterwards and exited 0 having
    restored almost nothing. Counting and printing proves nothing; what matters
    is that every relation the list names is actually present."""
    src = (REPO / 'scripts' / 'restore_db.sh').read_text()
    assert 'missing=' in src or 'missing' in src
    assert 'Do not start the portal against it' in src, \
        'a failed restore must say so in terms an operator will act on'
    assert 'exit 1' in src


def test_the_restore_can_be_inspected_without_destroying_anything():
    """A destructive script cannot be verified on a live host without a way to
    look at it first. The real run drops and recreates the portal database, which
    on UAT-01 is the database the demo reads and everything written since the
    last backup. So there has to be a mode that resolves the target, finds the
    backup and validates it, then stops."""
    src = (REPO / 'scripts' / 'restore_db.sh').read_text()
    assert '--dry-run' in src, 'a dry-run mode is required to check this safely'
    # Assert ordering rather than slicing out the branch: an earlier version of
    # this test cut the block at the first "fi" and so stopped at the nested
    # --also-zabbix conditional, then failed for the wrong reason - exactly the
    # kind of test that lies about what it checked.
    start = src.index('if [ "$DRY_RUN" -eq 1 ]; then')
    stop = src.index('exit 0', start)
    drop = src.index('DROP DATABASE IF EXISTS', start)
    assert stop < drop, 'the dry-run must exit before anything is dropped'
    assert 'would restore database' in src[start:stop], \
        'the dry-run must report which database it would touch - that is the ' \
        'question it exists to answer'


def test_no_database_artefact_is_tracked_by_git():
    """A dump in a public repository is a data breach, not a large file.

    .gitignore listed two specific filenames - zabbix_full_*.sql.gz and
    atm_custom_tables.sql - while backup_db.sh writes seven things to the same
    directory. The portal database dump, the GLPI ticket dump, all three CSV
    exports and restore_db.sh's pre-restore dump were all untracked *and*
    unignored, so the next `git add -A` on a live host would have published the
    ATM registry, the transaction history and every ticket.

    One already was committed: a pre_restore dump picked up by `git add -A`
    during a restore rehearsal. It contained only synthetic fixture rows, which
    was luck rather than design.
    """
    tracked = subprocess.run(['git', 'ls-files', 'config/postgres/'],
                             capture_output=True, text=True, cwd=str(REPO),
                             timeout=60).stdout.split()
    offenders = [f for f in tracked
                 if f.endswith(('.sql', '.sql.gz', '.gz', '.csv', '.dump'))]
    offenders = [f for f in offenders
                 if f not in ('config/postgres/seed_uat_atms.sql',
                              'config/postgres/uat_tables.sql')]
    assert not offenders, (
        'these database artefacts are tracked in git and would be published:\n  %s\n'
        'Untrack them and extend .gitignore.' % '\n  '.join(offenders))


def test_every_artefact_the_backup_and_restore_write_is_ignored():
    """Derived from the scripts rather than from a list someone remembered to
    update. Adding a new dump output without a matching ignore rule fails here
    instead of on a live host at the worst possible moment."""
    ignore = subprocess.run(['git', 'check-ignore', '-q', '--stdin'],
                            input='', capture_output=True, text=True,
                            cwd=str(REPO), timeout=60)
    del ignore   # only used to confirm git is usable in this environment

    written = set()
    for name in ('backup_db.sh', 'restore_db.sh'):
        src = (REPO / 'scripts' / name).read_text()
        # Resolve the simple NAME="literal" assignments each script makes, so a
        # path built as "$BACKUP_DIR/$PRE.gz" is checked under the name it
        # actually produces rather than skipped for containing a dollar sign.
        # Leaving them unexpanded meant the pre_restore dumps - which is what
        # got committed by accident - were never actually tested.
        fixed = src.replace('$(date +%Y%m%d_%H%M%S)', '20260101_000000')
        consts = dict(re.findall(r'^\s*([A-Z_]+)="([^"]*)"', fixed, re.M))
        for m in re.finditer(r'BACKUP_DIR/([A-Za-z0-9_.$]+)', fixed):
            token = m.group(1)
            for var, value in consts.items():
                token = token.replace('$' + var, value)
            written.add(token)

    expanded = set(written)
    # .sql.gz must be in this list explicitly. The first version checked only
    # .sql/.csv/.dump, so every gzipped dump - the entire portal database, every
    # GLPI ticket - was skipped, and removing the .sql.gz ignore rule entirely
    # left the suite green. A guard that skips the case it exists for is not a
    # guard; a mutation test is what caught it.
    unignored = []
    for w in sorted(expanded):
        if w.endswith(('.sql', '.sql.gz', '.gz', '.csv', '.dump')):
            r = subprocess.run(
                ['git', 'check-ignore', '-q', 'config/postgres/' + w],
                cwd=str(REPO), timeout=60)
            if r.returncode != 0:
                unignored.append(w)
    assert not unignored, (
        'these outputs of backup_db.sh / restore_db.sh are not gitignored and '
        'would be committed by `git add -A`:\n  %s' % '\n  '.join(unignored))


def test_no_portal_script_defaults_to_the_zabbix_database():
    """`os.environ.get('DB_NAME', 'zabbix')` was in four scripts, and after the
    split "zabbix" is the one database the portal no longer reads.

    The worst was ej_pull_targets.py, which runs EVERY MINUTE from cron. The
    cron line exports ZBX_URL/ZBX_USER/ZBX_PASS but not DB_NAME, so the EJ
    pipeline resolved its target ATMs from the pre-split database. Nothing about
    that fails - the read succeeds and returns the old rows - so an ATM
    registered through the portal would simply never have its journals pulled,
    and the only symptom would be an ATM quietly having no data. It worked on
    UAT only because atm_locations happened not to have changed since the copy.
    """
    offenders = []
    for f in sorted((REPO / 'scripts').glob('*.py')):
        src = f.read_text()
        if re.search(r"environ\.get\(\s*['\"]DB_NAME['\"]\s*,\s*['\"]zabbix['\"]", src):
            offenders.append(f.name)
    assert not offenders, (
        'these scripts still default DB_NAME to zabbix, so run standalone they '
        'operate on the pre-split database and report success:\n  %s'
        % '\n  '.join(offenders))


def test_the_ej_resolver_detects_the_database_instead_of_guessing():
    """The one script on the per-minute path has to find the database itself,
    because nothing on its cron line tells it. Detection uses the same marker
    the backup uses, and an explicit DB_NAME still wins for an operator who
    knows better."""
    src = (REPO / 'scripts' / 'ej_pull_targets.py').read_text()
    assert 'def detect_portal_db' in src, 'the EJ resolver must detect the database'
    assert 'portal_schema_version' in src, \
        'detection needs the same marker backup_db.sh uses'
    assert "_MARKER" in src
    # an explicit setting must still win, and an undetectable one must not
    # silently become zabbix
    assert "os.environ.get('DB_NAME')" in src, \
        'an explicit DB_NAME must override detection'
    assert 'using the CSV snapshot' in src, \
        'when detection fails it must say so and fall back, not guess'


def test_the_two_database_scripts_refuse_rather_than_guess():
    for name in ('add_perf_indexes.py', 'sync_atms_to_zabbix.py'):
        src = (REPO / 'scripts' / name).read_text()
        assert '_require_db_name' in src, \
            '%s must require DB_NAME rather than defaulting it' % name
        assert 'sys.exit(2)' in src, 'and must stop when it is unset'


def test_the_preflight_sends_portal_and_zabbix_checks_to_different_databases():
    """One file, two kinds of table. Sending a portal check to the Zabbix
    database reports OK for a fleet the portal is not serving, which is false
    confidence from the one tool meant to catch a bad cutover."""
    src = (REPO / 'scripts' / 'uat_preflight.sh').read_text()
    assert 'detect_portal_db' in src
    # the portal table must follow the detection
    assert re.search(r'psql[^\n]*-d "\$PORTAL_DB"[^\n]*atm_locations', src), \
        'atm_locations is a portal table and must be read from $PORTAL_DB'
    # Zabbix's own table must NOT
    assert re.search(r'psql[^\n]*-d zabbix[^\n]*FROM hosts', src), \
        "hosts is Zabbix's own table and must stay on the zabbix database"


def test_no_tracked_env_file_carries_a_real_secret():
    """.env.uat was TRACKED and held real UAT credentials - a GLPI app token and
    the Zabbix password, identified by shape alone: a 36-character
    UUID-pattern token and an 11-character random string. The remote is GitHub,
    and the migration guide instructs copying that file to .env, so it was doing
    a real job and could not simply be deleted.

    It is now untracked, and its twelve keys were added to .env.example as
    placeholders so the template stays complete. The values remain in git
    HISTORY: this test is not a fix on its own, rotating them is. What it does
    is stop it happening again.

    Scoped to secret-shaped KEYS rather than judging every value. An earlier
    version tried to classify values and flagged ISO8583_MODE=simulation,
    PGADMIN_EMAIL and GATEWAY_IP - three false alarms in a row. A check that
    cries wolf gets switched off. Key names are unambiguous; values are not.
    """
    import re
    placeholder = re.compile(
        r'(CHANGEME|REPLACE|your[-_ ]|xxx+|<[^>]*>|\.\.\.|TODO|PLACEHOLDER|'
        r'EXAMPLE|DUMMY|generate|random[-_ ]?secret|-here$|_here$|insert[-_ ]|'
        r'fill[-_ ]?in|\b(change|insert|generate|provide|supply|edit|enter|set)[-_ ]'
        r'|^(change|insert|generate)[-_ ]?me)', re.I)
    secret_key = re.compile(r'(PASS|PASSWORD|TOKEN|SECRET|KEY|CREDENTIAL)', re.I)
    # The vendor default, publicly documented, and not a credential anyone
    # gained by reading the repository. Whether a deployment should still be
    # running on it is a separate hardening question - not what this test is for.
    public_defaults = {'zabbix'}

    tracked = subprocess.run(['git', 'ls-files'], capture_output=True, text=True,
                             cwd=str(REPO), timeout=60).stdout.split()
    env_files = [f for f in tracked if pathlib.Path(f).name.startswith('.env')]
    assert env_files, 'expected .env.example to stay tracked as the template'
    assert '.env.uat' not in env_files, (
        '.env.uat holds real UAT credentials. A tracked file cannot be rescued by '
        'an ignore rule, so untrack it - and record its keys in .env.example as '
        'placeholders.')

    for f in env_files:
        for line in (REPO / f).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            value = value.strip().strip('"\'')
            if not value or not secret_key.search(key):
                continue
            if value.lower() in public_defaults:
                continue
            assert placeholder.search(value), (
                '%s: %s is not a placeholder. This file is TRACKED, so .env* will '
                'not save it - untrack the file instead.' % (f, key))


def test_every_sidebar_icon_exists_in_the_bundled_lucide():
    """A wrong data-lucide name renders as nothing at all.

    No error, no warning, no gap in the markup - just a silently missing glyph,
    which in an icon-only collapsed rail means an unlabelled button. The bundle
    is lucide v1.27.0 and stores icons in PascalCase, so the check has to
    translate: my first attempt substring-matched the raw file and reported 25
    of 27 icons missing, including ones that had been in use for months.
    """
    import re
    lucide = (REPO / 'report-portal' / 'static' / 'lucide.js').read_text()
    tpl = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    used = sorted(set(re.findall(r'data-lucide="([a-z0-9-]+)"', tpl)))
    assert used, 'expected the sidebar to use lucide icons'
    missing = []
    for name in used:
        pascal = ''.join(w[:1].upper() + w[1:] for w in name.split('-'))
        if not re.search(r'\b%s\b' % re.escape(pascal), lucide):
            missing.append('%s (looked for %s)' % (name, pascal))
    assert not missing, (
        'these data-lucide names are not in the bundled lucide and will render '
        'as blank space:\n  %s' % '\n  '.join(missing))


def test_the_sidebar_keeps_every_route_and_every_admin_gate():
    """The sidebar was regrouped by task rather than by codebase location. The
    grouping is a judgement call; losing a destination or an access control to
    it is not. Snapshot both and compare."""
    import json
    tpl = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    nav = tpl[tpl.index('<nav class="sidebar-nav">'):
              tpl.index('</nav>', tpl.index('<nav class="sidebar-nav">'))]
    depth, routes, gated = 0, [], []
    for line in nav.split('\n'):
        stripped = line.strip()
        # Only BLOCK-level tags: inline ones belong to active-class expressions
        # and decrementing on those lines loses the gate entirely.
        if re.fullmatch(r"\{%\s*if\s+session\.get\('role'\)\s*==\s*'admin'\s*%\}", stripped):
            depth += 1
            continue
        if re.fullmatch(r'\{%\s*endif\s*%\}', stripped):
            depth = max(0, depth - 1)
            continue
        for h in re.findall(r'href="([^"]+)"', line):
            routes.append(h)
            if depth > 0:
                gated.append(h)
    assert len(routes) == 16, 'expected 16 destinations, found %d' % len(routes)
    assert sorted(gated) == sorted([
        '/admin/audit', '/admin/onboard', '/admin/onboard/fleet',
        '/admin/schedules', '/admin/users', '{{ grafana_url }}',
    ]), 'the admin-gated destinations changed: %s' % sorted(gated)


def test_every_sidebar_item_has_a_tooltip():
    """The collapsed rail is icons only, so a missing title attribute is an
    unlabelled button. Before the regrouping only 6 of 16 items had one."""
    tpl = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    nav = tpl[tpl.index('<nav class="sidebar-nav">'):
              tpl.index('</nav>', tpl.index('<nav class="sidebar-nav">'))]
    items = re.findall(r'<a[^>]*class="nav-item[^"]*"', nav)
    titled = re.findall(r'<a[^>]*class="nav-item[^"]*"[^>]*title="', nav)
    # the multiline anchors put title after class, so count both orders
    titled = re.findall(r'class="nav-item[^"]*"[^>]*?title="[^"]+"', nav, re.S) + \
             re.findall(r'title="[^"]+"[^>]*?class="nav-item[^"]*"', nav, re.S)
    assert items, 'no nav items found'
    assert len(titled) == len(items), (
        'every sidebar item needs a title for the collapsed rail: %d of %d have one'
        % (len(titled), len(items)))


def test_collapsing_the_sidebar_hides_no_destination():
    """A rule from an earlier design - when the Dashboard group was the only
    one - hid every item inside a group while collapsed, on the theory that
    clicking the group icon would expand the sidebar and reveal them.

    Once every destination moved into a group, that rule hid the entire menu.
    The collapsed rail rendered with a toggle button and nothing else, and
    nothing in the test suite or the Jinja render noticed: the HTML was perfect,
    the CSS simply made it invisible.

    Found by looking at a screenshot, which is the argument for looking."""
    tpl = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    hiding = re.findall(
        r'\.sidebar\.collapsed[^{]*\.nav-(?:group-)?items[^{]*\{[^}]*display\s*:\s*none',
        tpl)
    assert not hiding, (
        'these rules hide navigation items while the sidebar is collapsed, which '
        'leaves the rail empty:\n  %s' % '\n  '.join(hiding))

    # And the same for the items themselves. The selector must END at .nav-item:
    # a looser pattern also matched ".nav-item .ext{display:none}", which hides
    # the external-link glyph in the 60px rail and is exactly right.
    hiding_items = re.findall(
        r'\.sidebar\.collapsed\s+\.nav-item\s*(?:,[^{]*?)?\{[^}]*display\s*:\s*none',
        tpl)
    assert not hiding_items, (
        'nav items must remain visible when collapsed: %s' % hiding_items)


def test_the_collapsed_rail_keeps_a_mark_and_a_divider_per_group():
    """Two defects visible only in the collapsed state: the brand rendered as an
    empty box (logo display:none plus text collapsed to width:0), and the
    section headers had nowhere to go."""
    tpl = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    assert '.sidebar.collapsed .sidebar-logo{display:none}' not in tpl, \
        'the brand mark must survive the collapse - an empty box reads as a fault'
    assert '.sidebar.collapsed .nav-section-head{' in tpl, \
        'the section header needs a collapsed presentation'
    # A 1px divider must not be a 1px click target.
    collapsed_head = tpl[tpl.index('.sidebar.collapsed .nav-section-head{'):]
    collapsed_head = collapsed_head[:collapsed_head.index('}')]
    assert 'pointer-events:none' in collapsed_head, \
        'a hairline divider must not be interactive'
