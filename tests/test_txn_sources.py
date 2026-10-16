"""The simulator must not be able back into a transaction view.

Two layers are tested here:

1. Policy behaviour of report-portal/txn_sources.py, against a fake connection -
   no live database needed, so this runs anywhere.
2. A structural guarantee that every transaction read in the portal goes through
   atm_transactions_visible. The ~45 call sites are too many to review by hand on
   every change, and a single missed one is a silent leak of invented money data
   into a KPI, a CSV or a cash forecast. This test fails when a new query reaches
   for the base table.

The allowlist below is deliberate and small: DELETEs, DDL, retention pruning and
the health check must keep using the real table, or the portal could not clean up
after itself.
"""
import ast
import datetime
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import txn_sources  # noqa: E402


class FakeCursor:
    """Minimal psycopg2 cursor stand-in.

    fail=True simulates a host where the migration has not been applied, which
    must hide the simulator rather than fail open.
    """

    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.closed = False

    def execute(self, sql, args=None):
        if self.conn.fail:
            raise Exception('relation "txn_source_policy" does not exist')
        if 'min(demo_expires_at)' in sql:
            self._rows = [(self.conn.demo_until,)]
        elif 'FROM txn_source_policy' in sql and 'NOT EXISTS' not in sql:
            self._rows = list(self.conn.policy)
        elif 'NOT EXISTS' in sql:
            self._rows = [(self.conn.hidden_rows,)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, policy=None, fail=False, hidden_rows=0, demo_until=None):
        self.policy = policy if policy is not None else [
            ('EJ', 'EJ journal (real ATM)', True, True, None),
            ('ISO8583', 'ATM switch (ISO 8583)', True, True, None),
            ('SIMULATOR', 'Transaction simulator', False, False, None),
            ('ISO8583_SIM', 'Switch simulator', False, False, None),
            ('ISO8583_REAL', 'Switch test generator', False, False, None),
        ]
        self.demo_until = demo_until
        self.fail = fail
        self.hidden_rows = hidden_rows
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True


def test_default_policy_shows_real_sources_only():
    visible, hidden, fallback = txn_sources.load(FakeConn())
    assert [s for s, _ in visible] == ['EJ', 'ISO8583']
    assert 'SIMULATOR' in [s for s, _ in hidden]
    assert fallback is False


def test_status_reports_hidden_rows():
    note, level = txn_sources.status(FakeConn(hidden_rows=4289))
    assert level == 'info'
    assert 'EJ' in note and '4,289' in note
    assert 'DEMONSTRATION' not in note


def test_demo_source_escalates_to_loud_banner():
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    conn = FakeConn(policy=[
        ('EJ', 'EJ journal', True, True, None),
        ('SIMULATOR', 'Transaction simulator', False, True, future),
    ], demo_until=future)
    note, level = txn_sources.status(conn)
    assert level == 'demo'
    assert 'Demo data' in note
    assert 'SIMULATOR' in note


def test_no_enabled_source_is_stated_not_hidden():
    note, level = txn_sources.status(FakeConn(policy=[('EJ', 'EJ', True, False, None)]))
    assert level == 'info'
    assert 'empty by policy' in note


def test_unmigrated_host_fails_closed():
    """No policy table must mean the simulator is hidden, not visible."""
    visible, hidden, fallback = txn_sources.load(FakeConn(fail=True))
    assert fallback is True
    assert all(s in txn_sources.TRUSTED_SOURCES for s, _ in visible)
    assert 'SIMULATOR' in [s for s, _ in hidden]


def test_failed_query_rolls_back():
    """get_db() pools connections: an aborted transaction must not go back in
    the pool, or every later query on it fails."""
    conn = FakeConn(fail=True)
    txn_sources.load(conn)
    assert conn.rolled_back is True


def test_iso8583_real_is_not_trusted():
    """The name says REAL but scripts/fake_iso8583_switch.py generates it, so it
    must never be treated as bank data by default."""
    assert 'ISO8583_REAL' not in txn_sources.TRUSTED_SOURCES
    assert 'ISO8583_REAL' in txn_sources.DEMO_SOURCES


# ── structural guarantee ────────────────────────────────────────────────────

# Direct uses of the base table that are correct, each pinned to the exact text
# that justifies it. Adding a query here is a deliberate act; anything else that
# reaches for atm_transactions fails the test below.
JUSTIFIED = [
    ('report-portal/audit.py', 'ALTER TABLE atm_transactions',
     'constraint DDL - must target the real table'),
    ('report-portal/audit.py', 'ON atm_transactions(',
     'index DDL - indexes the real table, which the view selects from'),
    ('report-portal/txn_sources.py', 'SELECT t.* FROM atm_transactions t',
     'the view definition itself - the single place allowed to read the base '
     'table, in order to build the filtered projection everything else uses'),
    ('report-portal/txn_sources.py', 'WHERE NOT EXISTS',
     'counts the hidden rows so the banner can say how many are suppressed'),
    ('report-portal/scheduler.py', "('atm_transactions', 'recorded_at'",
     'retention pruning - must also delete expired simulated rows'),
]

SQL_MARKERS = ('FROM atm_transactions', 'JOIN atm_transactions')


def _ddl_from(path, name):
    """Read a module-level list-of-strings constant via AST.

    Importing the module would need psycopg2, which the test venv does not have,
    and a test that cannot run locally is a test that stops being run.
    """
    tree = ast.parse((REPO / path).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == name:
            return [el.value for el in node.value.elts]
    raise AssertionError('%s not found in %s' % (name, path))


def _startup_ddl():
    return _ddl_from('report-portal/txn_sources.py', 'POLICY_DDL')


def _is_delete(line):
    return 'DELETE' in line.upper()


def _is_justified(rel, line):
    return any(rel == jpath and frag in line for jpath, frag, _ in JUSTIFIED)


def test_every_direct_base_table_read_is_justified():
    """The guarantee: no portal read reaches atm_transactions without a reason.

    Scans every module rather than a hand-listed set, so a new file cannot opt
    out of the check by simply not being mentioned.
    """
    offenders = []
    for py in sorted((REPO / 'report-portal').rglob('*.py')):
        if '__pycache__' in py.parts:
            continue
        rel = str(py.relative_to(REPO))
        for i, line in enumerate(py.read_text().split('\n'), 1):
            if 'atm_transactions_visible' in line:
                continue
            if not any(m in line for m in SQL_MARKERS):
                continue
            if _is_delete(line) or _is_justified(rel, line):
                continue
            offenders.append('%s:%d  %s' % (rel, i, line.strip()))
    assert not offenders, (
        'These reads bypass the simulator quarantine. Route them through '
        'atm_transactions_visible, or add a JUSTIFIED entry explaining why the '
        'base table is correct:\n  ' + '\n  '.join(offenders))


def test_deletes_still_reach_the_real_table():
    """Quarantining reads must not stop the portal cleaning up after itself:
    removing an ATM has to delete its simulated rows too."""
    admin = (REPO / 'report-portal' / 'blueprints' / 'admin.py').read_text()
    assert 'DELETE FROM atm_transactions WHERE' in admin
    assert 'DELETE FROM atm_transactions_visible' not in admin


def test_read_paths_use_the_view():
    """Positive check on the paths that render money figures."""
    routes = (REPO / 'report-portal' / 'routes.py').read_text()
    assert routes.count('atm_transactions_visible') >= 25, 'read sites went missing'
    admin = (REPO / 'report-portal' / 'blueprints' / 'admin.py').read_text()
    assert 'atm_transactions_visible' in admin, 'ATM detail page still reads the base table'
    kpi = (REPO / 'report-portal' / 'blueprints' / 'kpi.py').read_text()
    assert 'atm_transactions_visible' in kpi
    cash = (REPO / 'report-portal' / 'cash_service.py').read_text()
    assert 'atm_transactions_visible' in cash, 'cash forecast still reads simulated withdrawals'


def test_app_boot_applies_the_schema():
    """The outage this prevents: the code deploy landed first, the schema did
    not, and every page that reads transactions 500'd. The app must therefore
    apply the schema itself before serving."""
    app_src = (REPO / 'report-portal' / 'app.py').read_text()
    assert 'ensure_schema_best_effort' in app_src, (
        'create_app must apply the policy schema at boot')
    # ...and it must not be fatal, or a DB hiccup would take down login too.
    assert 'except Exception' in app_src
    boot = app_src.index('ensure_schema_best_effort')
    assert app_src.rindex('return app') > boot, 'schema must be ensured before returning the app'


def test_schema_is_only_created_when_missing():
    """Recreating the view on every start would make three gunicorn workers each
    take an ACCESS EXCLUSIVE lock on it for nothing."""
    src = (REPO / 'report-portal' / 'txn_sources.py').read_text()
    assert 'to_regclass' in src, 'must check for the view before running DDL'
    fn = src[src.index('def ensure_schema_best_effort'):]
    check = fn.index('to_regclass')
    ddl = fn.index('for stmt in POLICY_DDL')
    assert check < ddl, 'the existence check must come before the DDL'


def test_advisory_lock_is_transaction_scoped():
    """get_db() pools connections. A session-scoped advisory lock would return to
    the pool still held and block the next worker forever."""
    src = (REPO / 'report-portal' / 'txn_sources.py').read_text()
    assert 'pg_advisory_xact_lock' in src
    fn = src[src.index('def ensure_schema_best_effort'):]
    assert 'pg_advisory_lock(' not in fn.replace('pg_advisory_xact_lock(', '')


def test_boot_failure_is_logged_with_a_fix():
    """If it cannot self-heal, the operator needs the exact command."""
    src = (REPO / 'report-portal' / 'txn_sources.py').read_text()
    assert 'migrate_txn_sources.sql' in src, 'the failure path must name the fix'
    assert 'logger.error' in src


def test_startup_schema_never_resets_operator_choices():
    """init_schema() runs on every container start.

    If it re-asserted the seed defaults, a demo toggle enabled through
    scripts/set_txn_sources.py would silently revert on the next restart or
    deploy, and the audit log would claim a change that did not stick.
    """
    ddl = '\n'.join(_startup_ddl())
    assert 'UPDATE txn_source_policy' not in ddl, (
        'startup DDL must not rewrite policy rows')
    assert 'ON CONFLICT (source) DO NOTHING' in ddl, (
        'seed must only insert sources it has not seen before')
    assert any('CREATE OR REPLACE VIEW atm_transactions_visible' in q for q in _startup_ddl())


def test_sql_migration_matches_startup_ddl():
    """The .sql migration and the startup DDL are two copies on purpose (one for
    operators, one so a restart self-heals). They must not drift."""
    sql = (REPO / 'scripts' / 'migrate_txn_sources.sql').read_text()
    assert 'CREATE TABLE IF NOT EXISTS txn_source_policy' in sql
    assert 'CREATE OR REPLACE VIEW atm_transactions_visible' in sql
    assert 'UPDATE txn_source_policy' not in sql
    for src in ('EJ', 'ISO8583', 'SIMULATOR', 'ISO8583_SIM', 'ISO8583_REAL'):
        assert f"'{src}'" in sql, f'{src} missing from the migration'
        assert any(f"'{src}'" in q for q in _startup_ddl()), f'{src} missing from startup DDL'


def test_justified_entries_still_exist():
    """A JUSTIFIED entry whose line has been edited away is a stale exemption."""
    for rel, frag, _ in JUSTIFIED:
        assert frag in (REPO / rel).read_text(), (
            '%s no longer contains %r - drop the stale JUSTIFIED entry' % (rel, frag))


def test_cli_bootstrap_works_without_a_file_path():
    """scripts/ is not in the container and the host has no venv, so the only way
    to run a DB-dependent script is `docker exec -i report-portal python3 - < file`.

    Under `python3 -` there is no __file__, so a bootstrap that derives the path
    from __file__ breaks exactly where it is needed.
    """
    src = (REPO / 'scripts' / 'set_txn_sources.py').read_text()
    fn = src[src.index('def _find_portal_dir'):]
    assert "'__file__' in globals()" in fn, (
        'must tolerate being run with no __file__ (python3 -)')
    assert '/app' in fn, 'must know the in-container path'
    assert 'PORTAL_DIR' in fn, 'must allow an explicit override'
    assert 'db.py' in fn, 'must verify it found the portal, not just any directory'


def test_cli_explains_itself_when_psycopg2_is_unavailable(tmp_path):
    """Run where the DB driver cannot be imported, the operator must be told to
    use the container instead of being shown a psycopg2 traceback.

    This is the real UAT situation: the host has no venv, and the container that
    has psycopg2 has no scripts/.
    """
    import subprocess
    stub = tmp_path / 'stub'
    stub.mkdir()
    (stub / 'psycopg2.py').write_text('raise ImportError("psycopg2 blocked for test")\n')
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(stub),
           'PORTAL_DIR': str(REPO / 'report-portal')}
    out = subprocess.run([sys.executable, str(REPO / 'scripts' / 'set_txn_sources.py')],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode != 0
    text = out.stdout + out.stderr
    assert 'docker exec -i report-portal python3 -' in text, (
        'must give the working invocation, not just an import error')
    assert 'Traceback' not in text


# ── expiring demo mode ──────────────────────────────────────────────────────
#
# A demo that cannot switch itself off is the one thing guaranteed to end up
# showing invented cash to someone who believes it. So simulated data carries an
# expiry, and simulated data with NO expiry is invisible.

def _utc(hours=0):
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)


def test_simulated_data_without_an_expiry_is_hidden():
    """The safe default: no window means no demo data, even if enabled."""
    conn = FakeConn(policy=[
        ('EJ', 'EJ journal', True, True, None),
        ('SIMULATOR', 'Simulator', False, True, None),
    ])
    visible, hidden, _ = txn_sources.load(conn)
    assert [s for s, _ in visible] == ['EJ']
    assert 'SIMULATOR' in [s for s, _ in hidden]


def test_simulated_data_inside_its_window_is_visible():
    conn = FakeConn(policy=[
        ('EJ', 'EJ journal', True, True, None),
        ('ISO8583_SIM', 'Simulator', False, True, _utc(3)),
    ], demo_until=_utc(3))
    visible, _, _ = txn_sources.load(conn)
    assert [s for s, _ in visible] == ['EJ', 'ISO8583_SIM']


def test_expired_demo_window_disappears_on_its_own():
    """Nobody has to remember to switch it off."""
    conn = FakeConn(policy=[
        ('EJ', 'EJ journal', True, True, None),
        ('ISO8583_SIM', 'Simulator', False, True, _utc(-1)),
    ], demo_until=_utc(-1))
    visible, hidden, _ = txn_sources.load(conn)
    assert [s for s, _ in visible] == ['EJ']
    note, level = txn_sources.status(conn)
    assert level != 'demo', 'an expired demo must not keep the red banner up'


def test_banner_states_when_the_demo_ends():
    """The audience can see the safety net, not just the operator."""
    until = _utc(4)
    conn = FakeConn(policy=[
        ('EJ', 'EJ journal', True, True, None),
        ('ISO8583_SIM', 'Simulator', False, True, until),
    ], demo_until=until)
    note, level = txn_sources.status(conn)
    assert level == 'demo'
    assert 'EAT' in note and 'Demo data' in note
    assert 'not use it for cash or fraud' in note.lower()


def test_view_sql_and_python_agree_on_the_demo_predicate():
    """The banner and the data must not disagree.

    The view filters in SQL; status() filters in Python. If they drift, the page
    says "real data only" while invented rows are on screen - the exact failure
    the quarantine exists to prevent.
    """
    view = [q for q in _startup_ddl() if 'CREATE OR REPLACE VIEW' in q][0]
    assert 'p.trusted' in view
    assert 'p.demo_expires_at IS NOT NULL AND p.demo_expires_at > NOW()' in view
    now = datetime.datetime.now(datetime.timezone.utc)
    assert txn_sources._demo_live(now + datetime.timedelta(hours=1), now) is True
    assert txn_sources._demo_live(now - datetime.timedelta(hours=1), now) is False
    assert txn_sources._demo_live(None, now) is False


def test_schema_version_exists_because_skipping_is_not_free():
    """ensure_schema_best_effort skips DDL when things look present.

    Without a version marker a corrected view definition would never be applied
    to an already-deployed host - which is how the missing view, and then the
    stale three-hour timezone view, both persisted.
    """
    ddl = '\n'.join(_startup_ddl())
    assert 'portal_schema_version' in ddl
    src = (REPO / 'report-portal' / 'txn_sources.py').read_text()
    assert 'SCHEMA_VERSION = 2' in src
    fn = src[src.index('def ensure_schema_best_effort'):]
    assert 'portal_schema_version' in fn, 'the version check must gate the DDL'


def test_demo_sources_cannot_be_enabled_without_an_expiry():
    """The CLI must make the duration an explicit decision."""
    src = (REPO / 'scripts' / 'set_txn_sources.py').read_text()
    assert '--until' in src
    assert 'cannot outlive the demo' in src
    assert 'demo_expires_at' in src


def test_demo_banner_does_not_repeat_the_demo_source():
    """A real bug: the demo source was named once as demo data and again inside
    the list of everything on screen, so it read "ISO8583_SIM ... alongside
    EJ, ISO8583, ISO8583_SIM"."""
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    conn = FakeConn(policy=[
        ('EJ', 'EJ journal', True, True, None),
        ('ISO8583', 'ATM switch', True, True, None),
        ('ISO8583_SIM', 'Simulator', False, True, until),
    ], demo_until=until)
    note, _ = txn_sources.status(conn)
    assert note.count('ISO8583_SIM') == 1, note
    assert 'Real data on screen: EJ, ISO8583' in note


def test_banner_does_not_alarm():
    """The audience reads this. It must be unmistakable without looking like a
    failure, or a demo spends its time explaining an error that is not there."""
    note, level = txn_sources.status(FakeConn())
    assert level != 'demo'
    assert 'DEMONSTRATION DATA IS SHOWN' not in note


def test_active_nav_has_no_gold_stripe():
    """The 3px gold bar on the active sidebar item read as generic template
    styling; the tinted background and weight already mark the row."""
    css = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    assert '.nav-item.active::before' not in css
    assert '.nav-item.active{' in css, 'the active state must still be visible'


def test_sidebar_scrollbar_is_slim_and_the_menu_is_tighter():
    """A 17-row menu overflowed, and the default scrollbar with its arrows is
    what made the layout look broken beside the page scrollbar."""
    css = (REPO / 'report-portal' / 'templates' / 'base.html').read_text()
    assert 'scrollbar-width:thin' in css
    assert '.sidebar-nav::-webkit-scrollbar{width:6px}' in css
    nav = css[css.index('.nav-item{'):css.index('.nav-item.active{')]
    assert 'min-height:34px' in nav, 'menu rows must be tighter to fit'
