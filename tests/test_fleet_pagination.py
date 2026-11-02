"""Fleet table pagination and wide-table affordances.

The fleet page used to render every matching ATM (1,201 rows in UAT) in one
table. The horizontal scrollbar therefore sat below the whole page, and there
was no way to move between groups of rows. These tests pin the page contract:
bounded page sizes, clamped page numbers, and the template hooks that keep
the header/first column/scrollbar usable.
"""
import sys
from pathlib import Path

from flask import Flask, render_template, session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.onboarding as ob  # noqa: E402


def _app():
    from blueprints.onboarding import bp as onboarding_bp

    app = Flask(__name__,
                template_folder=str(ROOT / 'report-portal' / 'templates'),
                static_folder=str(ROOT / 'report-portal' / 'static'))
    app.secret_key = 'test'

    @app.context_processor
    def _csrf():
        return {'csrf_token': lambda: 'test-token',
                'state_legend': lambda: []}

    app.register_blueprint(onboarding_bp)
    return app


def _row():
    return {
        'atm_id': 'ATM-1', 'terminal_id': 'TID-1', 'vendor': 'NCR',
        'ip': '10.0.0.1', 'state': '',
        'onboarding_status': 'complete', 'onboarding_label': 'Onboarded',
        'status': 'ok', 'status_label': 'Reporting',
        'stages': {
            'provisioning': {'status': 'ok', 'label': 'Ready', 'detail': ''},
            'identity': {'status': 'pending', 'label': 'Pending',
                         'detail': 'No identity received'},
            'ej': {'status': 'pending', 'label': 'Pending',
                   'detail': 'Waiting for identity'},
            'images': {'status': 'unknown', 'label': 'Unknown', 'detail': ''},
        },
        'problem': '', 'blocker': '', 'next_action': 'Install identity profile',
        'needs_action': True,
    }


def _render(page=1, total_pages=3, total=1201, start_index=0, end_index=1):
    app = _app()
    with app.test_request_context('/admin/onboard/fleet?filter=all&per_page=50&page=2'):
        session['logged_in'] = True
        session['username'] = 'admin'
        session['role'] = 'admin'
        return render_template(
            'fleet.html', cached={
                'fresh': True, 'running': False, 'failed': False,
                'generated_label': '2026-11-02 10:02 EAT',
                'counts': {'total': 1201, 'needs_action': 1201},
            }, computing=False,
            counts={'total': 1201, 'onboarded': 0, 'needs_action': 1201,
                    'reporting': 0, 'identity_ready': 0, 'ej_ready': 1,
                    'images_ready': 0, 'not_provisioned': 14, 'failed': 0,
                    'review': 0},
            rows=[_row()], ttl=15, failed='', filter_mode='all', q='',
            page=page, total_pages=total_pages, per_page=50, total=total,
            start_index=start_index, end_index=end_index,
            page_sizes=(25, 50, 100))


def test_page_context_clamps_and_slices():
    app = _app()
    with app.test_request_context('/admin/onboard/fleet?per_page=50&page=2'):
        ctx = ob._fleet_page(1201)
    assert ctx['page'] == 2
    assert ctx['total_pages'] == 25
    assert ctx['start_index'] == 50
    assert ctx['end_index'] == 100


def test_a_page_past_the_end_is_clamped():
    app = _app()
    with app.test_request_context('/admin/onboard/fleet?per_page=50&page=999'):
        ctx = ob._fleet_page(1201)
    assert ctx['page'] == 25
    assert ctx['start_index'] == 1200
    assert ctx['end_index'] == 1201


def test_unknown_page_sizes_fall_back_to_the_default():
    app = _app()
    with app.test_request_context('/admin/onboard/fleet?per_page=50000&page=0'):
        ctx = ob._fleet_page(0)
    assert ctx['per_page'] == ob.FLEET_DEFAULT_PAGE_SIZE
    assert ctx['page'] == 1
    assert ctx['total_pages'] == 1
    assert ctx['start_index'] == 0
    assert ctx['end_index'] == 0


def test_the_template_has_the_wide_table_contract():
    html = _render()
    assert 'id="fleetScrollTop"' in html
    assert 'id="fleetTableWrap"' in html
    assert 'id="fleetTable"' in html
    assert 'fleet-table' in html
    assert 'Rows per page' in html
    assert 'Showing' in html


def test_pager_links_preserve_filter_search_and_page_size():
    html = _render(page=2, total_pages=3, total=1201, start_index=50, end_index=100)
    assert 'Page 2 of 3' in html
    assert 'page=1' in html
    assert 'page=3' in html
    assert 'filter=all' in html
    assert 'per_page=50' in html
    assert 'Prev' in html
    assert 'Next' in html


def test_the_route_slices_after_filtering_and_searching():
    src = (ROOT / 'report-portal' / 'blueprints' / 'onboarding.py').read_text()
    fn = src[src.index('def onboard_fleet():'):]
    assert "_fleet_page(len(rows))" in fn
    assert "rows[page_ctx['start_index']:page_ctx['end_index']]" in fn
