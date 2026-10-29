"""Tests for the host-side ATM image bridge.

The bridge is the only component that holds the HMAC key and the ATM
allow-list.  These tests pin the two properties that matter most:

* a browser/portal request never contains a raw path or filename;
* a missing file and a transport failure are different states.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import atm_image_bridge as bridge  # noqa: E402
from atm_image_link import name_hmac  # noqa: E402

KEY = b'k' * 64
GRG = 'ATM-TEST-GRG'
DAY = '2026-10-22'


def _config(tmp_path, *, atms=None):
    path = tmp_path / 'bridge.json'
    path.write_text(json.dumps({
        'max_days_back': 90,
        'atms': atms or {
            GRG: {
                'host': '10.41.30.115',
                'vendor': 'GRG',
                'roots': ['E:\\pic'],
                'image_root': 'E:\\pic',
            },
            'ATM-TEST-NCR': {
                'host': '10.41.30.114',
                'vendor': 'NCR',
                'roots': [
                    'C:\\ISUITE\\ICAM\\Permit\\icamimages',
                    'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip',
                ],
                'zip_root': 'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip',
            },
        },
    }))
    return bridge.load_config(path)


def _grg_name(stamp='261022153444', event='CardInserted',
              card='1234567890123456'):
    return (f'CAM[1]_[DateTime-{stamp}]_'
            f'[CarNum-{card}]{event}.jpg')


def _row(name, length=114843, mtime='2026-10-22T15:34:44.8883446+03:00'):
    return {
        'index': 0,
        'name': name,
        'name_masked': 'masked',
        'path': 'E:\\pic\\2026\\10\\22\\' + name,
        'length': length,
        'last_write_time': mtime,
        'kind': 'image',
        'fetchable': True,
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, '_today', lambda: dt.date(2026, 10, 27))
    return bridge.BridgeApp(_config(tmp_path), KEY, preflight_timeout=0)


def test_config_defaults_to_a_three_second_preflight(tmp_path):
    assert _config(tmp_path).preflight_timeout == 3


def test_candidates_never_return_a_raw_name_or_path(app, monkeypatch):
    name = _grg_name()
    end_name = _grg_name('261022153521', 'CardReturned')
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [
        _row(name),
        _row(end_name, length=108133,
             mtime='2026-10-22T15:35:21.1043934+03:00'),
    ])

    result = app.candidates(
        GRG, DAY, '2026-10-22 15:34:44', '2026-10-22 15:35:21')

    assert result['available'] is True
    assert result['confidence'] == 'high'
    assert len(result['candidates']) == 2
    candidate = result['candidates'][0]
    assert 'name' not in candidate
    assert 'path' not in candidate
    assert candidate['name_hmac'] == name_hmac(name, KEY)
    assert candidate['name_masked'] != name
    assert len(candidate['name_hmac']) == 64
    assert candidate['size_label'] == '112.2 KB'


def test_candidates_drop_the_unparsed_and_unknown_rows(app, monkeypatch):
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [
        _row('not-an-evidence-name.txt'),
        _row(_grg_name()),
    ])

    result = app.candidates(
        GRG, DAY, '2026-10-22 15:34:44', '2026-10-22 15:35:21')

    assert result['candidate_count'] == 1
    assert result['unparsed_count'] == 1


def test_missing_day_is_not_available(app, monkeypatch):
    def missing(*_a, **_k):
        raise bridge.ImageFetchError('E_MISSING')
    monkeypatch.setattr(bridge, 'list_dir', missing)

    with pytest.raises(bridge.NotAvailable) as exc:
        app.candidates(GRG, DAY, '2026-10-22 15:34:44',
                       '2026-10-22 15:35:21')
    assert str(exc.value) == 'Not available on ATM'


def test_transport_failure_is_not_reported_as_missing(app, monkeypatch):
    def timeout(*_a, **_k):
        raise bridge.ImageFetchError('agent timeout')
    monkeypatch.setattr(bridge, 'list_dir', timeout)

    with pytest.raises(bridge.UpstreamError):
        app.candidates(GRG, DAY, '2026-10-22 15:34:44',
                       '2026-10-22 15:35:21')


def test_image_re_lists_and_matches_the_hmac(app, monkeypatch):
    name = _grg_name()
    row = _row(name)
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [row])
    called = {}

    def fake_fetch(host, path, **kwargs):
        called['host'] = host
        called['path'] = path
        return b'\xff\xd8\xffpayload', {
            'size': 11, 'sha256': 'a' * 64, 'md5': 'b' * 32,
        }
    monkeypatch.setattr(bridge, 'fetch_bytes', fake_fetch)

    data, meta, content_type, ext = app.image(GRG, DAY, name_hmac(name, KEY))

    assert data.startswith(b'\xff\xd8\xff')
    assert content_type == 'image/jpeg'
    assert ext == '.jpg'
    assert called['host'] == '10.41.30.115'
    assert called['path'].endswith(name)


def test_an_unknown_hmac_is_not_available_without_fetching(app, monkeypatch):
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [_row(_grg_name())])
    monkeypatch.setattr(bridge, 'fetch_bytes', lambda *a, **k: pytest.fail(
        'must not fetch an unmatched locator'))

    with pytest.raises(bridge.NotAvailable):
        app.image(GRG, DAY, '0' * 64)


def test_unknown_atm_and_future_day_are_refused(app):
    with pytest.raises(bridge.BridgeError):
        app.candidates('ATM-OTHER', DAY, '2026-10-22 00:00:00',
                       '2026-10-22 23:59:59')
    with pytest.raises(bridge.BridgeError):
        app.candidates(GRG, '2026-10-28', '2026-10-28 00:00:00',
                       '2026-10-28 23:59:59')


def test_config_refuses_a_drive_root(tmp_path):
    with pytest.raises(RuntimeError):
        _config(tmp_path, atms={
            GRG: {
                'host': '10.41.30.115', 'vendor': 'GRG',
                'roots': ['E:\\'], 'image_root': 'E:\\pic',
            },
        })


def test_config_requires_ncr_zip_root(tmp_path):
    with pytest.raises(RuntimeError):
        _config(tmp_path, atms={
            'ATM-TEST-NCR': {
                'host': '10.41.30.114', 'vendor': 'NCR',
                'roots': ['C:\\ISUITE\\ICAM\\Permit\\icamimages'],
            },
        })


def test_ncr_zip_candidates_are_projected_without_the_zip_name(app,
                                                               monkeypatch):
    zip_name = '172.23.248.4_102226_163377_16_1.zip'
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [{
        'index': 0, 'name': zip_name,
        'name_masked': '172.23.248.4_102226_***_16_1.zip',
        'path': 'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip\\' + zip_name,
        'length': 163377,
        'last_write_time': '2026-10-22T16:33:00+03:00',
        'kind': 'zip', 'fetchable': True,
    }])

    result = app.candidates(
        'ATM-TEST-NCR', DAY, '2026-10-22 00:00:00', '2026-10-22 23:59:59')

    assert result['available'] is True
    candidate = result['candidates'][0]
    assert 'name' not in candidate
    assert 'path' not in candidate
    assert candidate['name_hmac'] == name_hmac(zip_name, KEY)
    assert candidate['kind'] == 'zip'
    assert candidate['size'] == 163377


def test_zip_entry_projection_uses_an_index_not_a_name(app, monkeypatch):
    zip_name = '172.23.248.4_102226_163377_16_1.zip'
    zip_row = {
        'index': 0, 'name': zip_name, 'name_masked': 'masked.zip',
        'path': 'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip\\' + zip_name,
        'length': 163377, 'last_write_time': '', 'kind': 'zip',
        'fetchable': True,
    }
    entries = [{
        'index': 0, 'name': 'PAN-1234567890123456.jpg',
        'name_masked': 'PAN-************3456.jpg',
        'length': 1000, 'compressed_length': 800, 'crc32': 42,
        'last_write_time': '', 'fetchable': True,
    }]
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [zip_row])
    monkeypatch.setattr(bridge, 'list_zip', lambda *a, **k: entries)

    result = app.zip_entries('ATM-TEST-NCR', DAY, name_hmac(zip_name, KEY))

    assert result['count'] == 1
    assert result['entries'][0]['index'] == 0
    assert 'name' not in result['entries'][0]
    assert result['entries'][0]['name_masked'] == 'PAN-************3456.jpg'
    assert result['entries'][0]['size_label'] == '1000 B'

    with pytest.raises(bridge.NotAvailable):
        app.zip_entries('ATM-TEST-NCR', '2026-10-21',
                        name_hmac(zip_name, KEY))


def test_preflight_fails_fast_when_tcp_10050_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, '_today', lambda: dt.date(2026, 10, 27))
    listed = []
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: listed.append(1))
    monkeypatch.setattr(bridge.socket, 'create_connection',
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConnectionRefusedError()))

    app = bridge.BridgeApp(_config(tmp_path), KEY, preflight_timeout=3)
    with pytest.raises(bridge.AgentUnreachable) as caught:
        app.candidates(GRG, DAY, '2026-10-22 15:34:44',
                       '2026-10-22 15:35:21')

    assert caught.value.status == 503
    assert caught.value.code == 'agent_unreachable'
    assert 'TCP/10050' in caught.value.public
    assert listed == [], 'the slow zabbix_get path must not run'


def test_preflight_continues_when_tcp_10050_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, '_today', lambda: dt.date(2026, 10, 27))
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [])

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(bridge.socket, 'create_connection',
                        lambda *a, **k: _Connection())

    app = bridge.BridgeApp(_config(tmp_path), KEY, preflight_timeout=3)
    result = app.candidates(GRG, DAY, '2026-10-22 15:34:44',
                            '2026-10-22 15:35:21')
    assert result['candidates'] == []


class _UnixClient(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=10)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(self._path)
        self.sock = sock


def test_http_server_refuses_a_missing_token_and_serves_the_allowlist(
        app, tmp_path, monkeypatch):
    token = 't' * 64
    monkeypatch.setattr(bridge.BridgeApp, 'candidates',
                        lambda self, atm_id, *_a: {'atm_id': atm_id})
    socket_path = str(tmp_path / 'bridge.sock')
    server = bridge.UnixHTTPServer(
        socket_path, bridge.BridgeHandler, app, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = _UnixClient(socket_path)
        conn.request('POST', '/v1/candidates', body=b'{}')
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        conn.close()

        body = json.dumps({
            'atm_id': GRG, 'day': DAY,
            'start': '2026-10-22 15:34:44',
            'end': '2026-10-22 15:35:21',
        }).encode()
        conn = _UnixClient(socket_path)
        conn.request('POST', '/v1/candidates', body=body, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        })
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {'atm_id': GRG}
        conn.close()

        other = json.dumps({
            'atm_id': 'ATM-NOT-ALLOWED', 'day': DAY,
            'start': '2026-10-22 15:34:44',
            'end': '2026-10-22 15:35:21',
        }).encode()
        conn = _UnixClient(socket_path)
        conn.request('POST', '/v1/candidates', body=other, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        })
        response = conn.getresponse()
        assert response.status == 404
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_zip_entries_rank_ncr_sessions_without_exposing_the_name(app,
                                                                 monkeypatch):
    zip_name = '172.23.248.4_102226_163377_16_1.zip'
    zip_row = {
        'index': 0, 'name': zip_name, 'name_masked': 'masked.zip',
        'path': 'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip\\' + zip_name,
        'length': 163377, 'last_write_time': '', 'kind': 'zip',
        'fetchable': True,
    }
    card = '123456789012345'
    entries = [{
        'index': 0,
        'name': f'1.102226.103353.Default-PicOnly.1.102226.103353.1.{card}.jpg',
        'name_masked': f'1.102226.103353.Default-PicOnly.1.102226.103353.1.1234*******2345.jpg',
        'length': 1000, 'compressed_length': 800, 'crc32': 42,
        'last_write_time': '2026-10-22T07:33:20.0000000',
        'fetchable': True,
    }, {
        'index': 1,
        'name': f'10.102226.103526.Default-PicOnly.1.102226.103353.1.{card}.jpg',
        'name_masked': f'10.102226.103526.Default-PicOnly.1.102226.103353.1.1234*******2345.jpg',
        'length': 1000, 'compressed_length': 800, 'crc32': 43,
        'last_write_time': '2026-10-22T07:35:40.0000000',
        'fetchable': True,
    }, {
        'index': 2,
        'name': f'2.102226.104430.Default-PicOnly.1.102226.104155.1.{card}.jpg',
        'name_masked': f'2.102226.104430.Default-PicOnly.1.102226.104155.1.1234*******2345.jpg',
        'length': 1000, 'compressed_length': 800, 'crc32': 44,
        'last_write_time': '2026-10-22T07:44:56.0000000',
        'fetchable': True,
    }]
    monkeypatch.setattr(bridge, 'list_dir', lambda *a, **k: [zip_row])
    monkeypatch.setattr(bridge, 'list_zip', lambda *a, **k: entries)

    result = app.zip_entries(
        'ATM-TEST-NCR', DAY, name_hmac(zip_name, KEY),
        '2026-10-22 10:33:53', '2026-10-22 10:35:30')

    assert result['confidence'] == 'high'
    assert result['candidate_count'] == 2
    assert result['start_anchor_count'] == 2
    assert result['card_field_used_for_link'] is False
    assert 'card field' in result['caveat']
    assert [e['index'] for e in result['entries']] == [0, 1, 2]
    assert result['entries'][0]['role'] == 'start'
    assert result['entries'][0]['captured_at'] == '2026-10-22 10:33:53'
    assert result['entries'][0]['session_at'] == '2026-10-22 10:33:53'
    assert 'name' not in result['entries'][0]
    assert '123456789012345' not in str(result)
