import os
import sys
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
    app.register_blueprint(ejb.bp)
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


def test_raw_download_route_is_admin_only():
    source = (ROOT / 'report-portal' / 'blueprints' / 'ej_journal.py').read_text()
    start = source.index("@bp.route('/ej-journal/<atm_id>/raw/<int:run_id>')")
    assert '@role_required(ROLE_ADMIN)' in source[start:source.index('def ej_raw_download')]


def test_atm_detail_has_admin_only_evidence_card():
    template = (ROOT / 'report-portal' / 'templates' / 'admin_atm_detail.html').read_text()
    assert 'EJ Evidence' in template
    assert 'can_view_ej' in template
    assert "url_for('ej_journal.ej_raw_download'" in template
    assert 'Open full EJ Journal' in template


def test_ej_journal_detail_has_raw_download_column():
    template = (ROOT / 'report-portal' / 'templates' / 'ej_journal_detail.html').read_text()
    assert '<th>Raw</th>' in template
    assert "url_for('ej_journal.ej_raw_download'" in template
