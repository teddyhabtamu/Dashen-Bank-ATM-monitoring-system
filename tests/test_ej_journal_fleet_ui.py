"""Fleet EJ journal page: the monitoring surface, not a raw table dump.

The page used to render every active ATM and leave 1,199 "no data" rows in the
main table. These tests pin the contracts that keep the redesign honest:
health tabs carry the fleet counts, the table footer reports the visible range,
and the template keeps the current-health / activity-period distinction.
"""
import sys
from datetime import date
from pathlib import Path

from flask import Flask, render_template, session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.ej_journal as fleet  # noqa: E402


def _app():
    app = Flask(__name__,
                template_folder=str(ROOT / 'report-portal' / 'templates'),
                static_folder=str(ROOT / 'report-portal' / 'static'))
    app.secret_key = 'test'
    app.jinja_env.globals['csrf_token'] = lambda: 'test-token'
    app.register_blueprint(fleet.bp)

    @app.context_processor
    def _ctx():
        return {'grafana_url': '#'}

    return app


def _row(atm_id, branch, vendor, journal_date, status, sessions=0,
         completed=0, declined=0):
    return {
        'atm_id': atm_id, 'branch': branch, 'vendor': vendor,
        'journal_date': journal_date,
        'parsed_at': '2026-11-02 14:10:01' if journal_date else '—',
        'sessions': sessions, 'completed': completed, 'declined': declined,
        'unknown': 0, 'no_receipt': 0, 'parse_errors': 0, 'status': status,
    }


def _render(days=30, health=None):
    app = _app()
    summary = {
        'files': 17, 'parse_errors': 0, 'unmapped_files': 0,
        'reporting_atms': 2, 'staged': 17, 'approved': 13,
        'approved_amount': 800.0, 'dispensed': 404.0,
        'no_cash_evidence': 0, 'unmapped_txns': 0, 'active_atms': 1201,
        'health': health or {'total': 1201, 'ok': 1, 'stale': 1, 'error': 0,
                             'no_data': 1199},
    }
    rows = [
        _row('ATM-TEST-NCR', '4 Kilo Building', 'NCR', date(2026, 11, 2),
             'ok', 13, 13),
        _row('TST-0316', 'Megenagna', 'GRG', date(2026, 10, 30), 'stale', 4, 3, 1),
        _row('ATM-TEST-GRG', 'Test lab', 'GRG', None, 'no data'),
    ]
    with app.test_request_context('/ej-journal'):
        session.update(role='admin', logged_in=True, username='tester')
        return render_template(
            'ej_journal.html', setup_error=None, summary=summary, rows=rows,
            attention_files=[], days=days, vendor='', q='', view='all', page=1,
            per_page=50, page_sizes=(25, 50, 100), total=1201, total_pages=25,
            start_index=1, end_index=50, today=date(2026, 11, 2),
            last_updated='14:12')


def test_health_view_and_page_size_are_clamped():
    assert fleet._view('no-data') == 'no_data'
    assert fleet._view('STALE') == 'stale'
    assert fleet._view('bogus') == 'all'
    assert fleet._per_page('100') == 100
    assert fleet._per_page('13') == fleet.DEFAULT_PAGE_SIZE
    assert fleet._per_page(None) == fleet.DEFAULT_PAGE_SIZE


def test_fleet_page_renders_health_tabs_and_a_bounded_range():
    html = _render()
    assert 'class="ej-health-tabs"' in html
    assert 'aria-label="Filter ATMs by health"' in html
    assert 'No data <span class="ev-tab-count">1,199</span>' in html
    assert 'Showing <b>1–50</b> of <b>1,201</b>' in html
    assert 'data-native' in html
    assert 'ETB 800.00' in html
    assert 'Current state, independent of the activity period' in html
    assert 'Latest journal file per ATM' in html


def test_health_summary_names_every_excluded_state():
    html = _render(health={'total': 1201, 'ok': 1148, 'stale': 39,
                           'error': 5, 'no_data': 9})
    assert '98.8%' in html
    assert 'have a parseable journal in staging' in html
    assert '<b>5</b> have parse errors.' in html
    assert '<b>9</b> have no EJ data yet.' in html
    assert 'Journal coverage</b> — the share of active ATMs' in html


def test_health_tabs_link_to_the_same_period_and_page_size():
    html = _render()
    assert 'view=healthy&amp;page=1&amp;per_page=50' in html
    assert 'view=no_data&amp;page=1&amp;per_page=50' in html


def test_detail_link_expands_to_cover_an_old_latest_file():
    html = _render(days=1)
    # TST-0316 is three days old, so a one-day table must not open an empty
    # one-day drill-down for it.
    assert '/ej-journal/TST-0316?days=7' in html
    assert '/ej-journal/ATM-TEST-NCR?days=1' in html
