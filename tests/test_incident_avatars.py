"""Incident avatars + sidebar unread badge.

Two user-facing gaps pinned here:
- GLPI profile pictures were never rendered; the portal showed initials.
  Pictures now come from GLPI's API (`user/<id>/picture`) through the
  portal proxy, so local and remote GLPI both work without a shared volume.
- The "New" tag existed only inside the ticket list. The sidebar badge
  must use the same definition and must not block page rendering.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.incidents as inc  # noqa: E402


def test_avatar_fetch_uses_the_glpi_picture_endpoint(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        content = b'\x89PNG\r\n\x1a\n'
        headers = {'Content-Type': 'image/png'}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Resp()

    monkeypatch.setattr(inc.requests, 'get', fake_get)
    inc._avatar_cache.clear()
    assert inc._fetch_avatar('session-token', 5) == (Resp.content, 'image/png')
    assert calls[0][0] == f'{inc.GLPI_URL}/apirest.php/user/5/picture'
    assert calls[0][1]['headers']['Session-Token'] == 'session-token'
    # Second read is served from the process cache, not a second API call.
    assert inc._fetch_avatar('session-token', 5) == (Resp.content, 'image/png')
    assert len(calls) == 1


def test_avatar_fetch_caches_the_no_picture_answer(monkeypatch):
    calls = []

    class Resp:
        status_code = 204
        content = b''
        headers = {}

    def fake_get(url, **kwargs):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(inc.requests, 'get', fake_get)
    inc._avatar_cache.clear()
    assert inc._fetch_avatar('session-token', 6) == (b'', '')
    assert inc._fetch_avatar('session-token', 6) == (b'', '')
    assert len(calls) == 1


def test_unread_ids_is_the_single_definition(monkeypatch):
    from datetime import datetime, timezone

    seen = {
        2: datetime(2026, 1, 3, tzinfo=timezone.utc),
        3: datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(inc, '_seen_map', lambda user, ids: seen)
    raw = [
        {'id': 1, 'date_mod': '2026-01-02 00:00:00'},  # never opened
        {'id': 2, 'date_mod': '2026-01-02 00:00:00'},  # opened after change
        {'id': 3, 'date_mod': '2026-01-02 00:00:00'},  # changed after opening
        {'id': 'bad', 'date_mod': '2026-01-02 00:00:00'},
    ]
    assert inc._unread_ids('operator', raw) == {1, 3}


def test_sidebar_badge_polls_the_incidents_endpoint():
    base = (ROOT / 'report-portal' / 'templates' / 'base.html').read_text()
    assert 'id="incidentNavBadge"' in base
    assert "/incidents/unread-count" in base
    assert 'setInterval(' in base, 'badge must update while the user is elsewhere'
    assert 'document.hidden' in base, 'hidden tabs should not keep polling'


def test_ticket_list_uses_the_unread_endpoint_definition():
    src = (ROOT / 'report-portal' / 'blueprints' / 'incidents.py').read_text()
    assert 'def unread_count()' in src
    assert 'unread_ids = _unread_ids(me, raw)' in src, (
        'the list and the sidebar must share one unread definition')
    assert 'len(_unread_ids(me, raw))' in src


def test_detail_avatar_uses_the_proxy_and_keeps_initials():
    tpl = (ROOT / 'report-portal' / 'templates' / 'incidents_detail.html').read_text()
    assert 'macro ticket_avatar' in tpl
    assert '/incidents/avatar/{{ m.author_id }}' in tpl
    assert 'onerror="this.remove()"' in tpl, (
        'a missing picture must reveal the initials underneath')
    assert 'position:relative;overflow:hidden' in tpl


def test_list_assignee_can_show_a_picture():
    tpl = (ROOT / 'report-portal' / 'templates' / 'incidents.html').read_text()
    src = (ROOT / 'report-portal' / 'blueprints' / 'incidents.py').read_text()
    assert 'assignee_pic' in tpl and 'assignee_pic' in src
    assert '/incidents/avatar/{{ t.assignee_id }}' in tpl


def test_avatar_uses_the_glpi_api_not_a_shared_volume():
    src = (ROOT / 'report-portal' / 'blueprints' / 'incidents.py').read_text()
    compose = (ROOT / 'docker-compose.yml').read_text()
    assert '/apirest.php/user/{uid}/picture' in src
    assert 'glpi-root:/var/www/html/glpi:ro' not in compose, (
        'avatars must work when GLPI runs on another host')


def _client():
    from flask import Flask
    from blueprints.auth import bp as auth_bp

    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(auth_bp)
    app.register_blueprint(inc.bp)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['username'] = 'operator'
            sess['role'] = 'operator'
        yield client


def test_avatar_route_serves_images_only_to_logged_in_users(monkeypatch):
    monkeypatch.setattr(inc, '_avatar_for',
                        lambda uid: (b'\x89PNG\r\n\x1a\n', 'image/png'))
    for client in _client():
        r = client.get('/incidents/avatar/5')
        assert r.status_code == 200
        assert r.headers['Content-Type'] == 'image/png'
        assert r.headers['Cache-Control'].startswith('private')
        with client.session_transaction() as sess:
            sess.clear()
        assert client.get('/incidents/avatar/5').status_code == 302

    monkeypatch.setattr(inc, '_avatar_for', lambda uid: None)
    for client in _client():
        assert client.get('/incidents/avatar/5').status_code == 404


def test_unread_endpoint_matches_the_list_definition(monkeypatch):
    raw = [{'id': 1, 'date_mod': '2026-01-02 00:00:00'}]
    monkeypatch.setattr(inc, '_cached_tickets', lambda: raw)
    monkeypatch.setattr(inc, '_ensure_seen_table', lambda: None)
    monkeypatch.setattr(inc, '_seen_map', lambda user, ids: {})
    inc._unread_cache.clear()
    for client in _client():
        assert client.get('/incidents/unread-count').get_json() == {'count': 1}

