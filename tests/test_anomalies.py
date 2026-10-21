"""Transaction anomaly inbox: EAT day filters, tab persistence, undo.

The route itself needs Flask and a database, so the pure date arithmetic is
tested directly and the template/route invariants are read from source. Those
are the pieces that were wrong: UTC date boundaries, a filter form that lost
the Acknowledged tab, and an Undo button hidden on the only tab that has
acknowledged rows.
"""
import pathlib
import re
import sys
from datetime import datetime

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORTAL = ROOT / 'report-portal'
sys.path.insert(0, str(PORTAL))

from anomaly_dates import eat_day_end_utc, eat_day_start_utc  # noqa: E402


def _read(*parts):
    return PORTAL.joinpath(*parts).read_text()


ROUTE = _read('blueprints', 'anomalies.py')
PAGE = _read('templates', 'admin_anomalies.html')


# ── EAT day boundaries ────────────────────────────────────────────────────

def test_from_date_starts_at_eat_midnight_in_utc():
    assert eat_day_start_utc('2026-10-21') == datetime(2026, 10, 20, 21, 0)


def test_to_date_ends_at_next_eat_midnight_in_utc():
    assert eat_day_end_utc('2026-10-21') == datetime(2026, 10, 21, 21, 0)


def test_boundaries_roll_over_month_and_leap_day():
    assert eat_day_end_utc('2026-10-31') == datetime(2026, 10, 31, 21, 0)
    assert eat_day_end_utc('2028-02-28') == datetime(2028, 2, 28, 21, 0)
    assert eat_day_end_utc('2028-02-29') == datetime(2028, 2, 29, 21, 0)


def test_invalid_dates_are_rejected():
    for value in ('', '21/10/2026', '2026-13-01', '2026-02-30'):
        with pytest.raises(ValueError):
            eat_day_start_utc(value)


def test_the_route_uses_eat_boundaries_not_utc_dates():
    assert 'eat_day_start_utc' in ROUTE and 'eat_day_end_utc' in ROUTE
    assert 'detected_at >= %s' in ROUTE
    assert 'detected_at < %s' in ROUTE
    assert "%s::timestamp + interval '1 day'" not in ROUTE


# ── tab persistence ───────────────────────────────────────────────────────

def test_filter_form_submits_the_current_tab():
    assert 'name="acknowledged"' in PAGE
    assert 'value="{{ filters.acknowledged }}"' in PAGE


def test_clear_keeps_the_current_tab():
    assert '/anomalies?acknowledged={{ filters.acknowledged }}' in PAGE


# ── acknowledged tab actions ──────────────────────────────────────────────

def test_acknowledged_rows_render_the_undo_action():
    assert re.search(r'{% if can_ack %}\s*<td>\s*{% if a\.acknowledged %}', PAGE)
    assert 'onclick="unacknowledge({{ a.id }})"' in PAGE


def test_the_action_header_matches_the_action_cell():
    assert '<th style="width:70px">Action</th>' in PAGE
    assert '{% if can_ack %}{% if is_ack_tab %}7{% else %}8{% endif %}{% else %}6{% endif %}' in PAGE


# ── empty and error states ────────────────────────────────────────────────

def test_empty_open_tab_points_at_acknowledged_history():
    assert 'No open anomalies.' in PAGE
    assert '{{ ack_count }}' in PAGE
    assert '?acknowledged=yes' in PAGE


def test_load_failures_are_logged_and_rendered():
    assert 'log.exception' in ROUTE
    assert 'load_error =' in ROUTE
    assert 'load-error' in PAGE
    assert 'Anomaly data is unavailable' in PAGE


def test_bad_dates_show_a_filter_error():
    assert 'Invalid date filter' in ROUTE


# ── write routes unchanged ────────────────────────────────────────────────

def test_acknowledge_and_undo_remain_post_operator_routes():
    assert "@bp.route('/anomalies/acknowledge', methods=['POST'])" in ROUTE
    assert "@bp.route('/anomalies/unacknowledge', methods=['POST'])" in ROUTE
    assert 'role_required(ROLE_OPERATOR, ROLE_ADMIN)' in ROUTE
