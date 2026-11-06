"""Tests for the portal-side ATM image viewer.

These tests pin the security boundary the portal owns:

* admin-only access;
* no raw filename or path in the rendered page;
* `IMAGE_VIEW` before a byte is streamed;
* missing evidence is exactly "Not available on ATM";
* a bridge failure is not rendered as missing evidence;
* the Unix-socket client actually works without a TCP port.
"""
from __future__ import annotations

import datetime as dt
import json
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import atm_images as bridge_client  # noqa: E402
import blueprints.atm_images as bpmod  # noqa: E402

HMAC = 'a' * 64
RAW_NAME = ('CAM[1]_[DateTime-261022153444]_'
            '[CarNum-1234567890123456]CardInserted.jpg')


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *_a, **_k):
        return None

    def fetchone(self):
        return self._row

    def close(self):
        return None


class _Conn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def cursor(self):
        return _Cursor(self._row)


def _app(monkeypatch):
    monkeypatch.setattr(bpmod, '_today', lambda: dt.date(2026, 10, 27))
    monkeypatch.setattr(bpmod, 'get_db', lambda: _Conn(
        ('ATM-TEST-GRG', '4 Kilo', 'GRG', 'TID', 'active')))
    calls = []
    monkeypatch.setattr(bpmod, 'log_action',
                        lambda action, detail=None: calls.append((action, detail)))
    monkeypatch.setattr(bpmod.bridge, 'configured', lambda: True)

    app = Flask(__name__, template_folder=str(ROOT / 'report-portal' / 'templates'))
    app.secret_key = 'test'
    app.jinja_env.globals['csrf_token'] = lambda: 'test'
    app.register_blueprint(bpmod.bp)

    from flask import Blueprint
    admin = Blueprint('admin', __name__)

    @admin.route('/atms/<atm_id>')
    def atm_detail(atm_id):
        return 'atm'

    ej = Blueprint('ej_journal', __name__)

    @ej.route('/ej-journal/<atm_id>')
    def ej_journal_detail(atm_id):
        return 'ej'

    app.register_blueprint(admin)
    app.register_blueprint(ej)
    return app, calls


def _admin(client):
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['role'] = 'admin'
        sess['username'] = 'admin'


def _candidate():
    return {
        'name_masked': ('CAM[1]_[DateTime-261022153444]_'
                        '[CarNum-1234********3456]CardInserted.jpg'),
        'name_hmac': HMAC,
        'day': '2026-10-22',
        'captured_at': '2026-10-22 15:34:44',
        'file_mtime': '2026-10-22T15:34:44.8883446+03:00',
        'capture_basis': 'filename_time',
        'clip_end': None,
        'clip_duration_s': None,
        'camera': 1,
        'event': 'CardInserted',
        'kind': 'image',
        'card_field': '1234********3456',
        'card_field_present': True,
        'delta_start_s': 0.0,
        'delta_end_s': -37.0,
        'in_interval': True,
        'service': False,
        'role': 'start',
        'score': 80,
        'size': 114843,
        'size_label': '112.2 KB',
        'content_type': 'image/jpeg',
        'fetchable': True,
    }


def _zip_result():
    return {
        'kind': 'atm-zip-entries',
        'atm_id': 'ATM-TEST-NCR',
        'host': '10.41.30.114',
        'day': '2026-10-22',
        'zip_masked': '172.23.248.4_102226_***_16_1.zip',
        'zip_hmac': HMAC,
        'count': 1,
        'available': True,
        'caveat': ('The entry name carries the ATM-local capture time and '
                   'the session time. The masked card field is display-only '
                   'and never used as the link.'),
        'session_start': '2026-10-22 15:34:44',
        'session_end': '2026-10-22 15:35:21',
        'confidence': 'high',
        'candidate_count': 1,
        'entry_count': 1,
        'unparsed_count': 0,
        'start_anchor_count': 1,
        'in_window_count': 1,
        'card_field_used_for_link': False,
        'entries': [{
            'index': 0,
            'name_masked': 'PAN-************3456.jpg',
            'length': 114843,
            'size_label': '112.2 KB',
            'compressed_length': 114000,
            'crc32': 42,
            'last_write_time': '2026-10-22T07:34:44.0000000',
            'zip_mtime_utc': '2026-10-22T07:34:44.0000000',
            'captured_at': '2026-10-22 15:34:44',
            'session_at': '2026-10-22 15:34:44',
            'camera': 1,
            'image_type': 'Default-PicOnly',
            'kind': 'image',
            'card_field': '1234********3456',
            'card_field_present': True,
            'role': 'start',
            'score': 150,
            'in_interval': True,
            'delta_start_s': 0.0,
            'delta_end_s': -37.0,
            'fetchable': True,
            'content_type': 'image/jpeg',
        }],
    }


def _ncr_result():
    return {
        'kind': 'atm-image-candidates',
        'vendor': 'NCR',
        'host': '10.41.30.114',
        'atm_id': 'ATM-TEST-NCR',
        'session_start': '2026-10-22 10:33:53',
        'session_end': '2026-10-22 10:35:30',
        'available': True,
        'confidence': 'low',
        'caveat': 'The zip name identifies the day container, not a transaction link.',
        'candidates': [{
            'name_masked': '172.23.248.4_102226_***_16_1.zip',
            'name_hmac': HMAC,
            'day': '2026-10-22',
            'zip_bytes': 163377,
            'image_count': 16,
            'camera': 1,
            'length': 163377,
            'size': 163377,
            'size_label': '159.5 KB',
            'last_write_time': '2026-10-22T16:33:00+03:00',
            'kind': 'zip',
            'content_type': 'application/zip',
            'fetchable': True,
        }],
    }


def _result():
    return {
        'kind': 'atm-image-candidates',
        'vendor': 'GRG',
        'host': '10.41.30.115',
        'atm_id': 'ATM-TEST-GRG',
        'session_start': '2026-10-22 15:34:44',
        'session_end': '2026-10-22 15:35:21',
        'available': True,
        'directory_masked': 'E:\\pic\\2026\\10\\22',
        'confidence': 'high',
        'ambiguous': False,
        'caveat': 'Timestamp and event sequence only.',
        'card_field_used_for_link': False,
        'candidate_count': 1,
        'service_count': 0,
        'unparsed_count': 0,
        'start_anchor_count': 1,
        'end_anchor_count': 0,
        'candidates': [_candidate()],
    }


def test_page_is_admin_only(monkeypatch):
    app, _calls = _app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['role'] = 'viewer'
        sess['username'] = 'viewer'

    response = client.get('/ej-journal/ATM-TEST-GRG/images')
    assert response.status_code == 403


def test_page_renders_masked_candidates_and_audits(monkeypatch):
    app, calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images?'
        'day=2026-10-22&start=15:34:44&end=15:35:21')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert '1234567890123456' not in body
    assert '1234********3456' in body
    assert 'card_field_used_for_link=False' in body
    assert 'flatpickr-date' in body
    assert 'flatpickr-time' in body
    assert 'type="date"' not in body
    assert 'type="time"' not in body
    assert calls and calls[0][0] == 'IMAGE_SEARCH'
    assert '1234567890123456' not in calls[0][1]


def test_missing_evidence_renders_the_exact_state(monkeypatch):
    app, calls = _app(monkeypatch)

    def missing(*_a, **_k):
        raise bpmod.bridge.NotAvailable()
    monkeypatch.setattr(bpmod.bridge, 'candidates', missing)
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images?day=2026-10-22')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<strong>Not available on ATM</strong>' in body
    assert calls and calls[0][0] == 'IMAGE_SEARCH'
    assert calls[0][1].endswith('unavailable 1 error 0')


def test_bridge_failure_is_not_missing_evidence(monkeypatch):
    app, calls = _app(monkeypatch)

    def down(*_a, **_k):
        raise bpmod.bridge.BridgeUnavailable('bridge is down')
    monkeypatch.setattr(bpmod.bridge, 'candidates', down)
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images?day=2026-10-22')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Image transport failed' in body
    assert 'bridge is down' in body
    assert 'Not available on ATM' not in body
    assert calls[0][1].endswith('unavailable 0 error 1')


def test_agent_unreachable_has_its_own_banner(monkeypatch):
    app, calls = _app(monkeypatch)

    def offline(*_a, **_k):
        raise bpmod.bridge.BridgeError(
            'The monitoring host cannot reach the ATM agent on TCP/10050.',
            code='agent_unreachable', status=503)
    monkeypatch.setattr(bpmod.bridge, 'candidates', offline)
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images?day=2026-10-22')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'ATM agent unreachable' in body
    assert 'TCP/10050' in body
    assert 'Image transport failed' not in body
    assert 'Not available on ATM' not in body


def test_candidate_page_links_to_the_wrapped_viewer(monkeypatch):
    app, _calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images?'
        'day=2026-10-22&start=15:34:44&end=15:35:21')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '/ej-journal/ATM-TEST-GRG/images/view?' in body
    assert 'name_hmac=' + HMAC in body
    assert 'event=CardInserted' in body
    assert 'camera=1' in body
    assert 'captured_at=' in body
    assert 'role=start' in body
    assert '112.2 KB' in body
    assert 'Card inserted' in body


def test_viewer_page_wraps_the_stream_and_keeps_the_chrome(monkeypatch):
    app, calls = _app(monkeypatch)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-GRG/images/view?day=2026-10-22'
        f'&name_hmac={HMAC}&kind=image&start=15:34:44&end=15:35:21')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'Image evidence' in body
    assert 'Sensitive evidence' in body
    assert 'Fullscreen' in body
    assert 'Open raw stream' in body
    assert '/ej-journal/ATM-TEST-GRG/images/file?' in body
    assert 'name_hmac=' + HMAC in body
    assert 'Not available on ATM' in body
    assert 'CardInserted' not in body
    # Opening the page itself is not a view; the stream route audits the bytes.
    assert calls == []


def test_viewer_shows_event_camera_and_capture_time(monkeypatch):
    app, _calls = _app(monkeypatch)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-GRG/images/view?day=2026-10-22'
        f'&name_hmac={HMAC}&kind=image&start=15:34:44&end=15:35:21'
        f'&event=CardInserted&camera=1&captured_at=2026-10-22%2015:34:44'
        f'&role=start')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'CardInserted' in body
    assert 'CAM 1' in body
    assert '2026-10-22 15:34:44' in body
    assert 'Card inserted' in body
    assert 'ATM file' in body


def test_session_page_separates_the_video_from_still_moments(monkeypatch):
    app, _calls = _app(monkeypatch)
    result = _result()
    video = dict(result['candidates'][0])
    video.update({
        'name_masked': 'session.mp4',
        'name_hmac': 'b' * 64,
        'kind': 'video',
        'event': '',
        'role': 'in_window',
        'captured_at': '2026-10-22 15:34:50',
        'clip_end': '2026-10-22 15:36:21',
        'clip_duration_s': 91.0,
        'size_label': '3.3 MB',
    })
    result['candidates'] = result['candidates'] + [video]
    monkeypatch.setattr(bpmod.bridge, 'candidates', lambda *a, **k: result)
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images?'
        'day=2026-10-22&start=15:34:44&end=15:35:21')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Session recording' in body
    assert 'Session video' in body
    assert '3.3 MB' in body
    assert '91.0s clip' in body


def test_zip_page_links_entries_through_the_indexed_viewer(monkeypatch):
    app, calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'zip_entries',
                        lambda *a, **k: _zip_result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-GRG/images/zip?'
        f'day=2026-10-22&zip_hmac={HMAC}')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '/ej-journal/ATM-TEST-GRG/images/view?' in body
    assert 'source=zip' in body
    assert 'index=0' in body
    assert 'zip_hmac=' + HMAC in body
    assert calls and calls[0][0] == 'IMAGE_SEARCH'


def test_ncr_candidate_link_carries_the_session_window(monkeypatch):
    app, calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _ncr_result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-NCR/images?'
        'day=2026-10-22&start=10:33:53&end=10:35:30')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Open archive' in body
    assert 'start=10:33:53' in body
    assert 'end=10:35:30' in body


def test_ncr_day_only_link_does_not_send_a_full_day_window(monkeypatch):
    app, calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _ncr_result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-NCR/images?day=2026-10-22')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Open archive' in body
    assert '&start=' not in body
    assert '&end=' not in body


def test_ncr_full_day_link_is_treated_as_no_session_window(monkeypatch):
    app, calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _ncr_result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-NCR/images?'
        'day=2026-10-22&start=00:00:00&end=23:59:59')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Open archive' in body
    assert '&start=' not in body
    assert '&end=' not in body


def test_ncr_page_uses_the_unified_day_archive_layout(monkeypatch):
    app, _calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _ncr_result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-NCR/images?day=2026-10-22')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'ev-session-summary is-three' in body
    assert 'Selected day' in body
    assert 'Full day' in body
    assert 'No session window selected' in body
    assert 'ev-archive-row' in body
    assert 'Archives for this day' not in body


def test_ncr_page_shows_the_requested_session_window(monkeypatch):
    app, _calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'candidates',
                        lambda *a, **k: _ncr_result())
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-NCR/images?'
        'day=2026-10-22&start=10:33:53&end=10:35:30')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Window 2026-10-22 10:33:53 → 2026-10-22 10:35:30' in body
    assert 'ev-confidence' in body
    assert 'Full day' not in body


def test_zip_page_ignores_a_full_day_window_for_ranking(monkeypatch):
    app, calls = _app(monkeypatch)
    seen = {}

    def fake_zip_entries(*args, **kwargs):
        seen['args'] = args
        seen['kwargs'] = kwargs
        return _zip_result()

    monkeypatch.setattr(bpmod.bridge, 'zip_entries', fake_zip_entries)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-NCR/images/zip?'
        f'day=2026-10-22&zip_hmac={HMAC}'
        f'&start=00:00:00&end=23:59:59')

    assert response.status_code == 200
    assert len(seen['args']) == 3
    assert seen['kwargs'] == {}


def test_zip_page_passes_the_window_and_shows_local_times(monkeypatch):
    app, calls = _app(monkeypatch)
    seen = {}

    def fake_zip_entries(atm_id, day, zip_hmac, start=None, end=None):
        seen.update(atm_id=atm_id, day=day, start=start, end=end)
        return _zip_result()

    monkeypatch.setattr(bpmod.bridge, 'zip_entries', fake_zip_entries)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-NCR/images/zip?'
        f'day=2026-10-22&zip_hmac={HMAC}'
        f'&start=15:34:44&end=15:35:21')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert seen == {
        'atm_id': 'ATM-TEST-NCR',
        'day': '2026-10-22',
        'start': '2026-10-22 15:34:44',
        'end': '2026-10-22 15:35:21',
    }
    assert 'Captured (ATM local)' in body
    assert 'Session (ATM local)' in body
    assert 'Session start' in body
    assert '2026-10-22 15:34:44' in body
    assert 'ZIP mtime 2026-10-22T07:34:44.0000000 UTC' in body
    assert 'never the link' in body
    assert calls and calls[0][0] == 'IMAGE_SEARCH'


def test_zip_viewer_uses_the_entry_stream_not_a_file_locator(monkeypatch):
    app, calls = _app(monkeypatch)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-GRG/images/view?day=2026-10-22'
        f'&source=zip&zip_hmac={HMAC}&index=0&kind=image')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Archive entry 0' in body
    assert '/ej-journal/ATM-TEST-GRG/images/zip/file?' in body
    assert 'zip_hmac=' + HMAC in body
    assert 'index=0' in body
    assert calls == []


class _Stream:
    headers = {
        'Content-Type': 'image/jpeg',
        'Content-Length': '10',
        'Content-Disposition': 'inline; filename="atm-evidence.jpg"',
        'X-Evidence-Size': '10',
        'X-Evidence-SHA256': 'c' * 64,
    }

    def __init__(self):
        self.closed = False

    def iter_bytes(self, size=65536):
        yield b'\xff\xd8\xffpayload'

    def close(self):
        self.closed = True


def test_image_stream_is_audited_and_not_cached(monkeypatch):
    app, calls = _app(monkeypatch)
    stream = _Stream()
    monkeypatch.setattr(bpmod.bridge, 'open_image',
                        lambda *a, **k: stream)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-GRG/images/file?'
        f'day=2026-10-22&name_hmac={HMAC}')

    assert response.status_code == 200
    assert response.data == b'\xff\xd8\xffpayload'
    assert response.headers['Content-Type'] == 'image/jpeg'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Content-Security-Policy'] == 'sandbox'
    assert calls[0][0] == 'IMAGE_VIEW'
    assert 'sha256 ' + 'c' * 64 in calls[0][1]
    assert stream.closed is True


def test_image_not_available_is_a_404_with_the_exact_text(monkeypatch):
    app, calls = _app(monkeypatch)

    def missing(*_a, **_k):
        raise bpmod.bridge.NotAvailable()
    monkeypatch.setattr(bpmod.bridge, 'open_image', missing)
    client = app.test_client()
    _admin(client)

    response = client.get(
        f'/ej-journal/ATM-TEST-GRG/images/file?'
        f'day=2026-10-22&name_hmac={HMAC}')

    assert response.status_code == 404
    assert response.get_data(as_text=True) == 'Not available on ATM'
    assert response.headers['Cache-Control'] == 'no-store'
    assert calls[0][0] == 'IMAGE_VIEW'
    assert calls[0][1].endswith('unavailable')


def test_invalid_locator_is_refused_before_the_bridge(monkeypatch):
    app, calls = _app(monkeypatch)
    monkeypatch.setattr(bpmod.bridge, 'open_image',
                        lambda *a, **k: pytest.fail('bridge must not be called'))
    client = app.test_client()
    _admin(client)

    response = client.get(
        '/ej-journal/ATM-TEST-GRG/images/file?'
        'day=2026-10-22&name_hmac=not-an-hmac')

    assert response.status_code == 400
    assert calls == []


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    token = 't' * 64

    def log_message(self, *_a):
        return

    def _authorized(self):
        return self.headers.get('Authorization') == f'Bearer {self.token}'

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            self.send_response(401)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = {
            'ok': True,
            'path': self.path,
            'candidate_count': 0,
            'candidates': [],
        }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            self.send_response(401)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        raw = b'\xff\xd8\xffunix-socket'
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def test_client_uses_the_unix_socket_and_bearer_token(tmp_path, monkeypatch):
    socket_path = tmp_path / 'bridge.sock'
    server = _UnixServer(str(socket_path), _BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv('ATM_IMAGE_BRIDGE_SOCKET', str(socket_path))
        monkeypatch.setenv('ATM_IMAGE_BRIDGE_TOKEN', 't' * 64)
        monkeypatch.delenv('ATM_IMAGE_BRIDGE_URL', raising=False)

        payload = bridge_client.candidates(
            'ATM-TEST-GRG', '2026-10-22',
            '2026-10-22 15:34:44', '2026-10-22 15:35:21')
        assert payload['ok'] is True
        assert payload['path'] == '/v1/candidates'

        stream = bridge_client.open_image(
            'ATM-TEST-GRG', '2026-10-22', 'a' * 64)
        try:
            assert b''.join(stream.iter_bytes()) == b'\xff\xd8\xffunix-socket'
        finally:
            stream.close()
    finally:
        server.shutdown()
        server.server_close()
