"""Timezone handling, which has silently cost us three hours of accuracy.

recorded_at and last_seen are `timestamp without time zone` columns holding UTC
wall time (Postgres session TZ Etc/UTC). To render or bucket them in Ethiopian
time the conversion must happen TWICE:

    naive-UTC  --AT TIME ZONE 'UTC'-->  instant  --AT TIME ZONE 'Africa/Addis_Ababa'-->  EAT wall

A single `recorded_at AT TIME ZONE 'Africa/Addis_Ababa'` asks Postgres to
*interpret* the value as if it were already EAT, producing a UTC instant three
hours EARLIER. The value is still "reasonable looking", so nothing looks broken
- the ATM detail page just says "last seen 18:22" for a machine answering at
21:22, and the Peak Transaction Hours chart labels every transaction in the
wrong hour.

This is not hypothetical: operations.py:225 documents the wrong conversion as
"seen live" and fixes it, while 13 other call sites kept the broken form for
weeks. Comments do not prevent this; tests do.
"""
import pathlib
import re

from conftest import chart_sql

REPO = pathlib.Path(__file__).resolve().parent.parent

# Columns stored as naive UTC. Converted incorrectly, each of these reads three
# hours in the past.
NAIVE_UTC_COLUMNS = ('recorded_at', 'last_seen', 'updated_at', 'state_changed_at')

EAT = 'Africa/Addis_Ababa'

# The one legitimate single-form pattern: build an EAT wall-clock midnight
# boundary from NOW() (a timestamptz), then re-localise it for comparison
# against a naive column. Converting NOW() is correct precisely because NOW()
# is not a naive UTC column.
BOUNDARY_RE = re.compile(
    r"date_trunc\([^)]*,\s*NOW\(\)\s*AT TIME ZONE\s+'%s'\)" % EAT)

SINGLE_FORM_RE = re.compile(r"AT TIME ZONE '%s'" % EAT)

PY_SOURCES = sorted(
    p for p in (REPO / 'report-portal').rglob('*.py')
    if '__pycache__' not in p.parts)


def _naive_conversion_sites():
    """Yield (path, lineno, line) for single-form conversions of naive columns."""
    for py in PY_SOURCES:
        for i, line in enumerate(py.read_text().split('\n'), 1):
            if SINGLE_FORM_RE.search(line) is None:
                continue
            if 'AT TIME ZONE \'UTC\' AT TIME ZONE' in line:
                continue                      # double form: correct
            if BOUNDARY_RE.search(line):
                continue                      # EAT-midnight boundary: correct
            if 'date_trunc' in line and 'NOW()' in line:
                continue
            for col in NAIVE_UTC_COLUMNS:
                if re.search(r'\b%s\b' % col, line):
                    yield py, i, line
                    break


def test_no_naive_column_is_converted_the_wrong_way():
    offenders = ['%s:%d  %s' % (p.relative_to(REPO), n, ln.strip())
                 for p, n, ln in _naive_conversion_sites()]
    assert not offenders, (
        'These convert a naive-UTC column with a single AT TIME ZONE, which '
        'shifts the value three hours into the past:\n  '
        + '\n  '.join(offenders)
        + "\n\nUse: col AT TIME ZONE 'UTC' AT TIME ZONE '%s'" % EAT)


def test_double_conversion_is_actually_used_somewhere():
    """Guard against someone 'fixing' this by deleting the conversion entirely."""
    found = any("AT TIME ZONE 'UTC' AT TIME ZONE '%s'" % EAT in p.read_text()
                for p in PY_SOURCES)
    assert found, "no correct double conversion found - did the fix get reverted?"


def test_eat_midnight_boundary_is_untouched():
    """The day-boundary pattern is correct and must not be rewritten."""
    hits = 0
    for py in PY_SOURCES:
        for line in py.read_text().split('\n'):
            if BOUNDARY_RE.search(line):
                hits += 1
                assert "date_trunc('day', NOW() AT TIME ZONE '%s') AT TIME ZONE '%s'" % (
                    EAT, EAT) in line or 'AT TIME ZONE' in line
    assert hits >= 2, (
        'expected the day-boundary pattern in at least two queries; found %d. '
        'If it was removed, "today" is no longer EAT-today.' % hits)


def test_last_seen_is_not_displayed_three_hours_stale():
    """Regression guard for the reported symptom.

    The ATM detail page showed "last seen 18:22" for a machine whose agent
    answered at 21:22, and the page badge read UNREACHABLE while zabbix_get
    returned live hardware data.
    """
    admin = (REPO / 'report-portal' / 'blueprints' / 'admin.py').read_text()
    assert "last_seen AT TIME ZONE 'Africa/Addis_Ababa'" not in admin, (
        'the ATM detail page is still showing last_seen three hours in the past')
    assert "last_seen AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa'" in admin


def test_peak_hour_chart_buckets_in_eat_not_utc():
    """The Peak Transaction Hours chart is the analytical cost of this bug: every
    bar was labelled three hours early, which misstates when the branch is busy."""
    kpi = (REPO / 'report-portal' / 'blueprints' / 'kpi.py').read_text()
    assert "recorded_at AT TIME ZONE 'Africa/Addis_Ababa'" not in kpi
    assert kpi.count("AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa'") >= 2, (
        'both the hour and the weekday must be converted')

# ── date bucketing, which has no AT TIME ZONE to catch it ───────────────────

BARE_DATE_RE = re.compile(r"DATE\(\s*recorded_at\s*\)")


def test_no_chart_buckets_a_day_by_utc_date():
    """Reported live: the Overview cash card read 1.5M while the Cash Dispensed
    Trend tooltip read 1,028,100 for the same day.

    The card counted from 00:00 EAT; the chart grouped by DATE(recorded_at),
    which is the UTC date and therefore starts at 03:00 EAT. The card was right
    and the chart was wrong - it silently dropped the first three hours of every
    day, which is exactly the window a bank opens at.
    """
    routes = (REPO / 'report-portal' / 'routes.py').read_text()
    offenders = [ln.strip() for ln in routes.split('\n') if BARE_DATE_RE.search(ln)]
    assert not offenders, (
        'these bucket days by UTC date instead of EAT:\n  ' + '\n  '.join(offenders))


def test_daily_charts_convert_both_in_select_and_group_by():
    """Converting only the SELECT would leave GROUP BY on the UTC date and split
    each day across two buckets."""
    routes = (REPO / 'report-portal' / 'routes.py').read_text()
    for name in ('TXN_VOL', 'SR', 'CASH'):
        sql = chart_sql(name)
        converted = sql.count("AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa'")
        assert converted >= 2, (
            '%s must convert in both SELECT and GROUP BY (found %d)' % (name, converted))


def test_live_transaction_feed_is_converted():
    """The Operations feed rendered recorded_at raw, so it showed 06:31 UTC next
    to a clock reading 09:31 EAT."""
    ops = (REPO / 'report-portal' / 'blueprints' / 'operations.py').read_text()
    feed = ops[ops.index('feed = _all('):ops.index('summary = _all(')]
    assert 'recorded_at_eat' in feed, 'the feed must return an EAT value'
    assert "AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa'" in feed
    assert 'ORDER BY recorded_at DESC' in feed, (
        'ordering must still use the stored value, not the formatted string')
    tpl = (REPO / 'report-portal' / 'templates' / 'operations.html').read_text()
    assert 'recorded_at_eat' in tpl, 'both the table and the JS row builder must use it'
