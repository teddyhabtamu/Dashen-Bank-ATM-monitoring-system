"""KPI page audit: every panel must be a different measurement of something real.

Asked after the Error Distribution panel appeared beside a Fault Distribution
panel showing the same four categories at the same counts. They were the same
query, rendered twice under different names - which reads as two independent
measurements agreeing, and is the most misleading thing a dashboard can do.

These tests keep that honest: no panel may be fed from another panel's data, no
label may leak a raw vendor code when a human name exists, and no ranking may
split one machine into two rows because a transaction carried a stale branch
string.
"""
import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
KPI = REPO / 'report-portal' / 'blueprints' / 'kpi.py'
KPI_HTML = REPO / 'report-portal' / 'templates' / 'kpi.html'


def _src():
    return KPI.read_text()


def _query(name):
    """The SQL assigned to a named variable inside kpi._collect()."""
    src = _src()
    start = src.index('%s = _all(' % name)
    opener = src.index('"""', start) + 3
    closer = src.index('"""', opener)
    return src[opener:closer]


# ── no panel is a copy of another ───────────────────────────────────────────

def test_fault_chart_is_not_a_copy_of_the_error_chart():
    """chart['fault_*'] was assigned from the `errors` rows verbatim."""
    src = _src()
    body = src[src.index('chart = {'):src.index('return {', src.index('chart = {'))]
    assert 'fault_labels' not in body, (
        "the fault chart is being fed the error chart data - that is the duplicate")
    assert 'fault_data' not in body
    assert body.count('for r in errors') == 2, (
        'exactly two payloads may come from `errors`: labels and counts')


def test_removed_panel_is_gone_from_the_template():
    """Orphaned JS or a blank card is worse than the duplicate."""
    html = KPI_HTML.read_text()
    assert 'faultChart' not in html
    assert 'Fault Distribution' not in html
    assert 'Error Distribution' in html, 'the real panel must survive'


def test_every_panel_has_a_distinct_title():
    """Two panels with the same title are a copy until proven otherwise."""
    html = KPI_HTML.read_text()
    titles = re.findall(r'<h2>([^<]+)</h2>', html)
    assert len(titles) == len(set(titles)), 'duplicate panel titles: %s' % titles


# ── labels ──────────────────────────────────────────────────────────────────

def test_fault_labels_prefer_a_human_name():
    """B2C1 and 3A7F are mapped in the seed data, so a raw code on screen means
    the join failed - here because the gateway stamps NCR codes onto GRG ATMs
    and fault_type_map is keyed on (code, vendor)."""
    sql = _query('errors')
    assert 'f.display_name' in sql, 'must prefer the mapped display name'
    assert 'COALESCE(f.display_name, t.error_code' in sql, (
        'falling back to the raw code is correct, but it must not be the norm')


def test_raw_codes_on_screen_have_a_known_cause():
    """Documents why B2C1 / 3A7F render as raw codes on the KPI page.

    fault_type_map is keyed on (raw_fault_code, vendor), and the gateway stamps
    NCR codes - '96' -> 3A7F, 'B2' -> B2C1 - onto every ATM regardless of
    vendor. A GRG machine therefore carries a code no GRG row can match, the join
    misses, and the COALESCE falls through to the raw code. Two GRG ATMs and two
    NCR ATMs produced four slices, two of them labelled 'Dispenser' and 'Card
    Reader' and two raw.

    The portal's behaviour is correct - a raw code beats inventing a label. The
    fix belongs in the generator, so this test pins the cause rather than
    pretending the portal can resolve it.
    """
    gw = (REPO / 'camel' / 'iso8583_gateway.py').read_text()
    assert "'96': ('ERROR', '3A7F')" in gw and "'B2': ('ERROR', 'B2C1')" in gw, (
        'the NCR codes are stamped unconditionally - this is the cause')
    assert 'error_code' in gw, 'sanity: the gateway still records a fault code'
    sql = _query('errors')
    assert 'f.vendor = l.vendor' in sql, (
        'the lookup is vendor-scoped, which is why a cross-vendor code cannot resolve')
    assert sql.count('LEFT JOIN fault_type_map') == 1, 'exactly one lookup, no duplication'


# ── ranking ─────────────────────────────────────────────────────────────────

def test_ranking_does_not_split_an_atm_by_a_stale_branch_string():
    """One ATM appeared twice in the ranking: '4 Kilo Building BRANCH' and
    '4 Kilo Building'. t.branch is denormalised onto the transaction and drifts;
    the registered branch on atm_locations is the one to report."""
    sql = _query('ranking')
    assert 'l.branch' in sql, 'branch must come from the registered ATM'
    assert 'GROUP BY t.atm_id, l.branch' in sql, (
        'grouping by the transaction branch splits one machine into several rows')


# ── availability ────────────────────────────────────────────────────────────

def test_availability_is_computed_from_state_history():
    """It is - but the table is created at runtime by state_manager, so a host
    where that service has never run returns nothing and the chart draws a flat
    0% line, which reads as 'every ATM was down' rather than 'no data'."""
    sql = _query('availability')
    assert 'atm_state_history' in sql
    assert "s.state = 'IN_SERVICE'" in sql, (
        'availability is uptime in IN_SERVICE only - note in the docs that one '
        'faulted peripheral therefore reads as a fully unavailable machine')


def test_availability_denominator_counts_the_fleet():
    sql = _query('availability')
    assert "FROM atm_locations WHERE status = 'active'" in sql, (
        'the denominator must be the active fleet, or availability is measured '
        'against a moving target')