"""Monitoring Center pagination.

The page used to render every monitored host and every fetched problem. UAT
has 1,188 hosts, so the browser got a very long DOM and the live poll replaced
all of it every 20 seconds. These tests pin server-side paging and the pager
contract used by both the initial render and the live refresh.
"""
import sys
from pathlib import Path

from flask import Flask, render_template, session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.monitoring as mon  # noqa: E402


def _app():
    from blueprints.monitoring import bp as monitoring_bp

    app = Flask(__name__,
                template_folder=str(ROOT / 'report-portal' / 'templates'),
                static_folder=str(ROOT / 'report-portal' / 'static'))
    app.secret_key = 'test'

    @app.context_processor
    def _ctx():
        return {'csrf_token': lambda: 'test-token',
                'zabbix_ui_url': 'http://zabbix.example'}

    app.register_blueprint(monitoring_bp)
    return app


def _problem():
    return {
        'eventid': '101', 'clock': '2026-11-02 08:31:35', 'severity': '2',
        'severity_name': 'Warning', 'acknowledged': False,
        'host': 'ATM-1', 'name': 'ATM Network — Agent port TCP unreachable',
        'ack_note': '', 'suppressed': False, 'maintenance': '',
        'suppress_until_label': '', 'glpi_id': '', 'ticket_url': '',
    }


def _host():
    return {
        'host': 'ATM-1', 'name': '4 Kilo Test NCR', 'ip': '10.41.30.114',
        'groups': 'ATMs, ATM-NCR', 'maintenance_until': '',
        'maintenance_name': '', 'state': 'UP',
    }


def _render():
    app = _app()
    with app.test_request_context('/monitoring?ppage=2&hpage=1&per_page=50'):
        session['logged_in'] = True
        session['username'] = 'admin'
        session['role'] = 'admin'
        return render_template(
            'monitoring_center.html',
            problems=[_problem()], hosts=[_host()],
            summary={'hosts': 1188, 'up': 0, 'down': 0,
                     'problems': 120, 'unacked': 120},
            problem_total=120, host_total=1188,
            ppage=2, ppages=3, prob_start=50, prob_end=51,
            hpage=1, hpages=24, host_start=0, host_end=1,
            per_page=50, page_sizes=(25, 50, 100),
            severities=mon.SEVERITIES, sev_colors=mon.SEV_COLORS,
            f_severity='', f_ack='', error='', at_cap=False,
            suppress_options=[('30', '30 minutes')],
            can_act=True)


def test_page_slices_and_clamps():
    app = _app()
    with app.test_request_context('/monitoring?ppage=2&per_page=50'):
        rows, page, pages, start, end = mon._mc_page(list(range(120)),
                                                     'ppage', 50)
    assert (page, pages, start, end) == (2, 3, 50, 100)
    assert rows == list(range(50, 100))


def test_a_page_past_the_end_is_clamped():
    app = _app()
    with app.test_request_context('/monitoring?ppage=999&per_page=50'):
        rows, page, pages, start, end = mon._mc_page(list(range(120)),
                                                     'ppage', 50)
    assert (page, pages, start, end) == (3, 3, 100, 120)
    assert rows == list(range(100, 120))


def test_unknown_page_size_falls_back_to_fifty():
    app = _app()
    with app.test_request_context('/monitoring?per_page=100000'):
        assert mon._mc_per_page() == mon.MC_DEFAULT_PAGE_SIZE


def test_both_tables_have_their_own_pager():
    html = _render()
    assert 'mcProbCount">120' in html
    assert 'mcHostCount">1188' in html
    assert 'Showing 51–51 of 120' in html
    assert 'Showing 1–1 of 1188' in html
    assert 'data-pager="problems"' in html
    assert 'data-pager="hosts"' in html
    assert 'Page 2 of 3' in html
    assert 'Page 1 of 24' in html
    assert 'Rows' in html


def test_the_live_endpoint_returns_page_metadata(monkeypatch):
    app = _app()
    problems = [dict(_problem(), eventid=str(1000 + i)) for i in range(120)]
    hosts = [dict(_host(), host='ATM-%d' % i) for i in range(1188)]

    def fake_collect(severity, ack):
        assert (severity, ack) == ('2', 'no')
        return ({'hosts': 1188, 'up': 0, 'down': 0,
                 'problems': 120, 'unacked': 120},
                problems, hosts, None, False)

    monkeypatch.setattr(mon, '_collect', fake_collect)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['username'] = 'admin'
            sess['role'] = 'admin'
        res = client.get(
            '/monitoring/api/live?severity=2&ack=no&per_page=50&ppage=2&hpage=3')

    assert res.status_code == 200
    d = res.get_json()
    assert d['problem_total'] == 120
    assert len(d['problems']) == 50
    assert d['problems'][0]['eventid'] == '1050'
    assert d['ppage'] == 2 and d['ppages'] == 3
    assert d['host_total'] == 1188
    assert len(d['hosts']) == 50
    assert d['hosts'][0]['host'] == 'ATM-100'
    assert d['hpage'] == 3 and d['hpages'] == 24


def test_the_live_endpoint_clamps_an_out_of_range_page(monkeypatch):
    app = _app()
    problems = [dict(_problem(), eventid=str(1000 + i)) for i in range(120)]

    monkeypatch.setattr(mon, '_collect', lambda s, a: (
        {'hosts': 0, 'up': 0, 'down': 0, 'problems': 120, 'unacked': 120},
        problems, [], None, False))
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['username'] = 'admin'
            sess['role'] = 'admin'
        d = client.get('/monitoring/api/live?ppage=999').get_json()

    assert d['ppage'] == 3 and d['ppages'] == 3
    assert len(d['problems']) == 20


def test_the_live_refresh_keeps_the_operator_on_the_same_page():
    tpl = (ROOT / 'report-portal' / 'templates'
           / 'monitoring_center.html').read_text()
    assert 'updatePagers(d)' in tpl
    assert "window.location.search" in tpl


def test_the_problem_fetch_limit_is_not_the_old_hundred():
    src = (ROOT / 'report-portal' / 'blueprints' / 'monitoring.py').read_text()
    assert "'limit': PROBLEM_FETCH_LIMIT" in src
    assert 'PROBLEM_FETCH_LIMIT = 5000' in src
