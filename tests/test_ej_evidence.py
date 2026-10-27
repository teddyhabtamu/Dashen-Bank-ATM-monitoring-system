import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import evidence  # noqa: E402


def _write(path: Path, data=b'evidence'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_canonical_name_is_taken_from_windows_and_posix_paths():
    assert evidence.raw_filename(r'D:\Log\2026-10-25_TESTGRG_EJDATA.log') == \
        '2026-10-25_TESTGRG_EJDATA.log'
    assert evidence.raw_filename('/data/ej-logs/2026-10-25_TESTGRG_EJDATA.log') == \
        '2026-10-25_TESTGRG_EJDATA.log'


@pytest.mark.parametrize('value', [
    None, '', 'x.log', '2026-10-25_TESTGRG.log',
    '../../etc/passwd', '2026-10-25_TESTGRG_EJDATA.log.bak',
])
def test_non_canonical_names_are_refused(value):
    assert evidence.raw_filename(value) is None


def test_raw_path_only_resolves_inside_the_allowlisted_root(tmp_path):
    raw = tmp_path / 'raw'
    _write(raw / '2026-10-25_TESTGRG_EJDATA.log')
    outside = _write(tmp_path / 'outside' / '2026-10-24_OUTSIDE_EJDATA.log')

    found = evidence.raw_path(
        '/data/ej-logs/2026-10-25_TESTGRG_EJDATA.log', base=raw)
    assert found == (raw / '2026-10-25_TESTGRG_EJDATA.log').resolve()

    assert evidence.raw_path(
        '/data/ej-logs/../../outside/2026-10-25_TESTGRG_EJDATA.log',
        base=raw) is None
    assert evidence.raw_path(outside.name, base=raw) is None


def test_symlink_is_refused(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    target = _write(tmp_path / 'target.log')
    link = raw / '2026-10-25_TESTGRG_EJDATA.log'
    os.symlink(target, link)

    assert evidence.raw_path(link.name, base=raw) is None


def test_missing_file_is_not_available(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    assert evidence.raw_info(
        '2026-10-25_TESTGRG_EJDATA.log', base=raw
    ) == {'raw_available': False, 'raw_size': None, 'raw_size_label': '—'}


def test_raw_info_reports_human_size(tmp_path):
    raw = tmp_path / 'raw'
    _write(raw / '2026-10-25_TESTGRG_EJDATA.log', b'x' * 2048)

    info = evidence.raw_info('2026-10-25_TESTGRG_EJDATA.log', base=raw)
    assert info['raw_available'] is True
    assert info['raw_size'] == 2048
    assert info['raw_size_label'] == '2.0 KB'


def test_sha256_is_of_the_raw_bytes(tmp_path):
    path = _write(tmp_path / '2026-10-25_TESTGRG_EJDATA.log', b'evidence')
    assert evidence.sha256_file(path) == \
        'ee8250fb76e094b34b471f13a73dbbe51d1ae142e9df59d7c0d31ec20f0a0a8e'


def test_raw_dir_reads_env_each_call(monkeypatch, tmp_path):
    monkeypatch.setenv('EVIDENCE_RAW_DIR', str(tmp_path / 'one'))
    assert evidence.raw_dir() == tmp_path / 'one'
    monkeypatch.setenv('EVIDENCE_RAW_DIR', str(tmp_path / 'two'))
    assert evidence.raw_dir() == tmp_path / 'two'


def test_read_text_lines_decodes_utf8_and_crlf(tmp_path):
    path = _write(
        tmp_path / '2026-10-25_TESTGRG_EJDATA.log',
        b'alpha\r\nbeta\r\n',
    )
    result = evidence.read_text_lines(path)
    assert result['lines'] == ['alpha', 'beta']
    assert result['too_large'] is False
    assert result['binary'] is False


def test_read_text_lines_makes_control_bytes_visible(tmp_path):
    path = _write(
        tmp_path / '2026-10-25_CONTROL_EJDATA.log',
        b'STX\x02field\n',
    )
    result = evidence.read_text_lines(path)
    assert result['lines'] == ['STX\\x02field']


def test_read_text_lines_refuses_binary_and_oversize(tmp_path):
    binary = _write(
        tmp_path / '2026-10-25_BINARY_EJDATA.log', b'abc\x00def')
    assert evidence.read_text_lines(binary)['binary'] is True

    larger = _write(
        tmp_path / '2026-10-25_LARGE_EJDATA.log', b'0123456789')
    result = evidence.read_text_lines(larger, max_bytes=3)
    assert result['too_large'] is True
    assert result['lines'] == []


def test_paginate_lines_is_case_insensitive_and_keeps_line_numbers():
    lines = ['Card OK', 'transaction APPROVED', 'card declined', 'tail']
    result = evidence.paginate_lines(lines, query='CARD', page=1, per_page=1)
    assert result['total'] == 2
    assert result['pages'] == 2
    assert result['lines'] == [{'number': 1, 'text': 'Card OK'}]

    result = evidence.paginate_lines(lines, query='CARD', page=2, per_page=1)
    assert result['lines'] == [{'number': 3, 'text': 'card declined'}]


def test_paginate_lines_clamps_page_and_handles_empty_search():
    result = evidence.paginate_lines(['one', 'two'], query='missing', page=9)
    assert result['page'] == 1
    assert result['total'] == 0
    assert result['lines'] == []


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *_args, **_kwargs):
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

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor(self._row)


def _app(monkeypatch, row, tmp_path):
    from flask import Flask
    import blueprints.ej_journal as ejb

    monkeypatch.setattr(ejb, 'get_db', lambda: _Conn(row))
    calls = []
    monkeypatch.setattr(ejb, 'log_action', lambda action, detail=None: calls.append((action, detail)))
    monkeypatch.setenv('EVIDENCE_RAW_DIR', str(tmp_path / 'raw'))

    app = Flask(__name__, template_folder=str(ROOT / 'report-portal' / 'templates'))
    app.secret_key = 'test'
    app.jinja_env.globals['csrf_token'] = lambda: 'test'
    app.register_blueprint(ejb.bp)

    # The raw viewer links back to the ATM detail page. A stub keeps this
    # route test focused on evidence access instead of booting the admin
    # blueprint and all of its Zabbix/database dependencies.
    from flask import Blueprint
    admin = Blueprint('admin', __name__)

    @admin.route('/atms/<atm_id>')
    def atm_detail(atm_id):
        return 'atm'

    app.register_blueprint(admin)
    return app, calls


def _admin_session(client):
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['role'] = 'admin'
        sess['username'] = 'admin'


def test_raw_download_streams_bytes_and_audits(monkeypatch, tmp_path):
    raw = tmp_path / 'raw'
    expected = _write(raw / '2026-10-25_TESTGRG_EJDATA.log', b'raw evidence')
    app, calls = _app(
        monkeypatch, ('/data/ej-logs/2026-10-25_TESTGRG_EJDATA.log',), tmp_path)
    client = app.test_client()
    _admin_session(client)

    response = client.get('/ej-journal/ATM-GRG-1/raw/7')

    assert response.status_code == 200
    assert response.data == expected.read_bytes()
    assert response.headers['Content-Disposition'].startswith('attachment;')
    assert response.headers['Cache-Control'] == 'no-store'
    assert calls and calls[0][0] == 'EJ_RAW_DOWNLOAD'
    assert '2026-10-25_TESTGRG_EJDATA.log' in calls[0][1]
    assert 'sha256 ' in calls[0][1]


def test_raw_download_rejects_traversal(monkeypatch, tmp_path):
    app, calls = _app(
        monkeypatch, ('../../etc/passwd',), tmp_path)
    client = app.test_client()
    _admin_session(client)

    response = client.get('/ej-journal/ATM-GRG-1/raw/7')

    assert response.status_code == 404
    assert calls == []


def test_raw_download_rejects_wrong_atm(monkeypatch, tmp_path):
    app, calls = _app(monkeypatch, None, tmp_path)
    client = app.test_client()
    _admin_session(client)

    assert client.get('/ej-journal/ATM-GRG-1/raw/7').status_code == 404
    assert calls == []


def test_raw_download_rejects_missing_archive(monkeypatch, tmp_path):
    app, calls = _app(
        monkeypatch, ('/data/ej-logs/2026-10-25_TESTGRG_EJDATA.log',), tmp_path)
    client = app.test_client()
    _admin_session(client)

    assert client.get('/ej-journal/ATM-GRG-1/raw/7').status_code == 404
    assert calls == []


def _view_row(file_path='/data/ej-logs/2026-10-25_TESTGRG_EJDATA.log'):
    return (
        file_path,
        datetime(2026, 10, 25).date(),
        datetime(2026, 10, 26, 16, 20, tzinfo=timezone.utc),
        'ok',
        None,
        2,
        'TESTGRG',
        '4 Kilo Branch',
    )


def test_raw_view_renders_and_searches_archived_file(monkeypatch, tmp_path):
    raw = tmp_path / 'raw'
    _write(
        raw / '2026-10-25_TESTGRG_EJDATA.log',
        b'line one\r\nCard APPROVED\r\nline three\r\n',
    )
    app, calls = _app(monkeypatch, _view_row(), tmp_path)
    client = app.test_client()
    _admin_session(client)

    response = client.post(
        '/ej-journal/ATM-GRG-1/raw/7/view',
        data={'q': 'approved'},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'Card APPROVED' in body
    assert 'line one' not in body
    assert 'matching line' in body
    assert 'q_len 8' in calls[0][1]
    assert 'approved' not in calls[0][1].lower()
    assert calls[0][0] == 'EJ_RAW_VIEW'


def test_raw_view_is_forbidden_for_non_admin(monkeypatch, tmp_path):
    raw = tmp_path / 'raw'
    _write(raw / '2026-10-25_TESTGRG_EJDATA.log', b'evidence')
    app, calls = _app(monkeypatch, _view_row(), tmp_path)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['role'] = 'viewer'
        sess['username'] = 'viewer'

    assert client.get('/ej-journal/ATM-GRG-1/raw/7/view').status_code == 403
    assert calls == []


def test_raw_view_paginates_and_clamps_page(monkeypatch, tmp_path):
    raw = tmp_path / 'raw'
    _write(
        raw / '2026-10-25_TESTGRG_EJDATA.log',
        b'\n'.join(('line %d' % n).encode() for n in range(1, 252)),
    )
    app, _calls = _app(monkeypatch, _view_row(), tmp_path)
    client = app.test_client()
    _admin_session(client)

    response = client.post(
        '/ej-journal/ATM-GRG-1/raw/7/view',
        data={'page': '2'},
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Page 2 of 2' in body
    assert 'line 201' in body
    assert 'line 1<' not in body

    response = client.post(
        '/ej-journal/ATM-GRG-1/raw/7/view',
        data={'page': '99'},
    )
    body = response.get_data(as_text=True)
    assert 'Page 2 of 2' in body


def test_raw_view_refuses_binary_archive(monkeypatch, tmp_path):
    raw = tmp_path / 'raw'
    _write(raw / '2026-10-25_TESTGRG_EJDATA.log', b'abc\x00def')
    app, calls = _app(monkeypatch, _view_row(), tmp_path)
    client = app.test_client()
    _admin_session(client)

    response = client.get('/ej-journal/ATM-GRG-1/raw/7/view')
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'not plain text' in body
    assert 'Download raw file' in body
    assert calls[0][0] == 'EJ_RAW_VIEW'


def test_raw_view_refuses_traversal_and_missing_file(monkeypatch, tmp_path):
    app, calls = _app(monkeypatch, _view_row('../../etc/passwd'), tmp_path)
    client = app.test_client()
    _admin_session(client)
    assert client.get('/ej-journal/ATM-GRG-1/raw/7/view').status_code == 404
    assert calls == []

    app, calls = _app(monkeypatch, _view_row(), tmp_path)
    client = app.test_client()
    _admin_session(client)
    assert client.get('/ej-journal/ATM-GRG-1/raw/7/view').status_code == 404
    assert calls == []


def test_raw_routes_are_admin_only():
    source = (ROOT / 'report-portal' / 'blueprints' / 'ej_journal.py').read_text()
    for function in ('def ej_raw_download', 'def ej_raw_view'):
        start = source.index(function)
        decorators = source[max(0, start - 500):start]
        assert '@role_required(ROLE_ADMIN)' in decorators
    assert "/raw/<int:run_id>/view" in source


def test_atm_detail_has_admin_only_evidence_card():
    template = (ROOT / 'report-portal' / 'templates' / 'admin_atm_detail.html').read_text()
    assert 'EJ Evidence' in template
    assert 'can_view_ej' in template
    assert "url_for('ej_journal.ej_raw_view'" in template
    assert "url_for('ej_journal.ej_raw_download'" in template
    assert 'Open full EJ Journal' in template
    assert 'sessions in latest file' in template


def test_ej_journal_detail_has_raw_view_and_download_links():
    template = (ROOT / 'report-portal' / 'templates' / 'ej_journal_detail.html').read_text()
    assert '<th style="width:64px">Actions</th>' in template
    assert "url_for('ej_journal.ej_raw_view'" in template
    assert "url_for('ej_journal.ej_raw_download'" in template
    assert 'toggleKebab' in template
    assert 'View raw' in template
    assert 'Download raw' in template
    assert 'ATM images' in template
    assert 'View</a> · ' not in template


def test_fleet_table_says_counts_are_latest_file_only():
    template = (ROOT / 'report-portal' / 'templates' / 'ej_journal.html').read_text()
    assert 'Latest journal file per ATM' in template
    assert 'period totals above' in template


def test_raw_view_uses_the_light_portal_surface():
    template = (ROOT / 'report-portal' / 'templates' / 'ej_raw_view.html').read_text()
    assert '.raw-shell{background:#fff' in template
    assert '.raw-lines td.code{color:#0F172A' in template
