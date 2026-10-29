"""Incident avatars + sidebar unread badge.

Two user-facing gaps pinned here:
- GLPI profile pictures were never rendered; the portal showed initials.
  Pictures are served through a path-checked proxy from the read-only
  GLPI picture volume, not by linking to GLPI's session-protected URL.
- The "New" tag existed only inside the ticket list. The sidebar badge
  must use the same definition and must not block page rendering.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.incidents as inc  # noqa: E402


def test_avatar_path_accepts_nested_glpi_picture(tmp_path, monkeypatch):
    (tmp_path / 'ab').mkdir()
    pic = tmp_path / 'ab' / 'picture_abc.jpg'
    pic.write_bytes(b'jpeg')
    monkeypatch.setattr(inc, 'GLPI_PICTURE_DIR', str(tmp_path))
    assert inc._avatar_path('ab/picture_abc.jpg') == str(pic)
    assert inc._avatar_path('ab\\picture_abc.jpg') == str(pic)


def test_avatar_path_rejects_traversal_and_non_images(tmp_path, monkeypatch):
    (tmp_path / 'ab').mkdir()
    (tmp_path / 'ab' / 'picture.png').write_bytes(b'png')
    (tmp_path / 'secret.txt').write_text('nope')
    monkeypatch.setattr(inc, 'GLPI_PICTURE_DIR', str(tmp_path))
    for bad in ('../secret.txt', '/etc/passwd', 'ab/../../secret.txt',
                'secret.txt', 'ab/picture.png/..'):
        assert inc._avatar_path(bad) is None, bad


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


def test_glpi_picture_volume_is_mounted_read_only():
    compose = (ROOT / 'docker-compose.yml').read_text()
    assert 'glpi-root:/var/www/html/glpi:ro' in compose
    assert 'GLPI_PICTURE_DIR' in compose


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


def test_avatar_route_serves_images_only_to_logged_in_users(tmp_path, monkeypatch):
    (tmp_path / 'ab').mkdir()
    (tmp_path / 'ab' / 'p.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    monkeypatch.setattr(inc, 'GLPI_PICTURE_DIR', str(tmp_path))
    monkeypatch.setattr(inc, '_user_picture', lambda uid: 'ab/p.png')
    for client in _client():
        r = client.get('/incidents/avatar/5')
        assert r.status_code == 200
        assert r.headers['Content-Type'] == 'image/png'
        assert r.headers['Cache-Control'].startswith('private')
        with client.session_transaction() as sess:
            sess.clear()
        assert client.get('/incidents/avatar/5').status_code == 302

    monkeypatch.setattr(inc, '_user_picture', lambda uid: '../../etc/passwd')
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

