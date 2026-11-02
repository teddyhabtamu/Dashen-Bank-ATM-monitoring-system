"""GLPI range responses for the ticket list.

The portal asks for tickets 0-99. Once an instance holds more than 100
tickets, GLPI answers 206 Partial Content with that first page. Only 200 was
treated as success, so every refresh logged a warning and fell back to stale
data: new tickets stopped appearing in the portal.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.incidents as inc  # noqa: E402


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_a_partial_ticket_list_is_a_success(monkeypatch, caplog):
    page = [{'id': 100}, {'id': 99}]
    monkeypatch.setattr(inc.requests, 'get', lambda *a, **k: _Resp(206, page))

    assert inc._tickets('session-token') == page
    assert 'Ticket.list' not in caplog.text


def test_a_failed_ticket_list_is_still_reported(monkeypatch, caplog):
    monkeypatch.setattr(inc.requests, 'get', lambda *a, **k: _Resp(500))

    assert inc._tickets('session-token') is None
    assert 'GLPI Ticket.list status=500' in caplog.text


def _search_response(monkeypatch, ids, total):
    calls = []

    class Resp:
        status_code = 206

        def json(self):
            return {'totalcount': total,
                    'data': [{'2': str(tid)} for tid in ids]}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Resp()

    monkeypatch.setattr(inc.requests, 'get', fake_get)
    return calls


def test_search_fetches_one_page_but_reports_the_full_total(monkeypatch):
    calls = _search_response(monkeypatch, [100, 99], 1234)

    assert inc._search_ticket_ids('token', '', 100, 50) == ([100, 99], 1234)
    params = calls[0][1]['params']
    assert calls[0][0].endswith('/search/Ticket')
    assert params['range'] == '100-149'
    assert params['sort'] == '19'       # date_mod, not title
    assert params['order'] == 'DESC'
    assert 'criteria[0][field]' not in params


def test_the_open_tab_is_statuses_one_to_three(monkeypatch):
    calls = _search_response(monkeypatch, [], 0)

    inc._search_ticket_ids('token', 'open', 0, 50)
    params = calls[0][1]['params']
    assert params['criteria[0][field]'] == '12'
    assert params['criteria[0][value]'] == '1'
    assert params['criteria[1][link]'] == 'OR'
    assert params['criteria[1][value]'] == '2'
    assert params['criteria[2][link]'] == 'OR'
    assert params['criteria[2][value]'] == '3'


def test_multiple_items_are_reordered_like_the_search_page(monkeypatch):
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            # GLPI is not required to answer in the requested order.
            return [{'id': 2, 'name': 'second'}, {'id': 1, 'name': 'first'}]

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Resp()

    monkeypatch.setattr(inc.requests, 'get', fake_get)
    rows = inc._tickets_by_ids('token', [1, 2])
    assert [r['id'] for r in rows] == [1, 2]
    assert calls[0][0].endswith('/getMultipleItems')
    assert calls[0][1]['params']['items[0][items_id]'] == '1'


def test_counts_ask_glpi_for_a_total_without_downloading_rows(monkeypatch):
    inc._counts_cache.update(ts=0.0, data=None)
    wanted = {'open': 11, 'pending': 4, 'solved': 7}
    monkeypatch.setattr(inc, '_session', lambda: 'token')
    monkeypatch.setattr(inc, '_close', lambda token: None)
    monkeypatch.setattr(inc, '_search_ticket_ids',
                        lambda token, status, start, limit: ([1], wanted[status]))

    assert inc._ticket_counts() == wanted


def _board_client():
    from flask import Flask
    from blueprints.auth import bp as auth_bp

    app = Flask(__name__,
                template_folder=str(ROOT / 'report-portal' / 'templates'),
                static_folder=str(ROOT / 'report-portal' / 'static'))
    app.secret_key = 'test'

    @app.context_processor
    def _csrf():
        return {'csrf_token': lambda: 'test-token'}

    app.register_blueprint(auth_bp)
    app.register_blueprint(inc.bp)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['username'] = 'operator'
            sess['role'] = 'operator'
        yield client


def _ticket(tid):
    return {
        'id': tid, 'name': f'ticket {tid}', 'status': 1, 'priority': 2,
        'itilcategories_id': 'ATM', 'users_id_recipient': 'requester',
        'date_creation': '2026-01-01 00:00:00',
        'date_mod': '2026-01-02 00:00:00', 'users_id_lastupdater': 1,
    }


def test_board_paginates_with_the_whole_fleet_total(monkeypatch):
    counts = {'open': 120, 'pending': 3, 'solved': 4}
    seen = []
    monkeypatch.setattr(inc, '_ticket_counts', lambda: counts)
    monkeypatch.setattr(inc, '_cached_ticket_page',
                        lambda status, page: (seen.append((status, page)) or
                                              ([_ticket(5), _ticket(4)], 127)))
    monkeypatch.setattr(inc, '_ensure_seen_table', lambda: None)
    monkeypatch.setattr(inc, '_unread_ids', lambda user, rows: set())
    monkeypatch.setattr(inc, '_session', lambda: None)

    for client in _board_client():
        html = client.get('/incidents').get_data(as_text=True)
        assert 'Tickets (127)' in html
        assert 'Showing 1–2 of 127' in html
        assert 'Page 1 of 3' in html
        assert '?page=2' in html
        assert seen == [('', 1)]


def test_board_clamps_a_page_past_the_end(monkeypatch):
    counts = {'open': 120, 'pending': 3, 'solved': 4}
    seen = []
    monkeypatch.setattr(inc, '_ticket_counts', lambda: counts)
    monkeypatch.setattr(inc, '_cached_ticket_page',
                        lambda status, page: (seen.append((status, page)) or
                                              ([_ticket(5)], 127)))
    monkeypatch.setattr(inc, '_ensure_seen_table', lambda: None)
    monkeypatch.setattr(inc, '_unread_ids', lambda user, rows: set())
    monkeypatch.setattr(inc, '_session', lambda: None)

    for client in _board_client():
        html = client.get('/incidents?page=99').get_data(as_text=True)
        assert 'Page 3 of 3' in html
        assert '?page=2' in html
        assert '?page=4' not in html
        assert seen == [('', 3)]


def test_board_filter_links_keep_the_status(monkeypatch):
    counts = {'open': 120, 'pending': 3, 'solved': 4}
    monkeypatch.setattr(inc, '_ticket_counts', lambda: counts)
    monkeypatch.setattr(inc, '_cached_ticket_page',
                        lambda status, page: ([_ticket(5)], 120))
    monkeypatch.setattr(inc, '_ensure_seen_table', lambda: None)
    monkeypatch.setattr(inc, '_unread_ids', lambda user, rows: set())
    monkeypatch.setattr(inc, '_session', lambda: None)

    for client in _board_client():
        html = client.get('/incidents?status=open&page=1').get_data(as_text=True)
        assert 'Page 1 of 3' in html
        assert '?page=2&status=open' in html

