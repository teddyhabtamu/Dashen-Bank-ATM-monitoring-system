"""GRG EJ puller: names, UTF-16 decoding and append-only working copies.

The puller is the missing half of the existing GRG parser.  These tests keep
the local contract identical to the NCR puller so the shadow ingest and
filebeat do not grow a second filename dialect.
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import pull_grg_ej_logs as p  # noqa: E402


def test_both_grg_year_forms_are_tried():
    day = dt.date(2026, 10, 24)
    assert p.candidate_names(day) == ['EJ261024.log', 'EJ20261024.log']


def test_local_name_matches_the_ncr_ingest_contract():
    assert p.local_name(dt.date(2026, 10, 24), 'TESTGRG') == \
        '2026-10-24_TESTGRG_EJDATA.log'


def test_utf16le_bom_is_decoded_and_crlf_normalised():
    raw = '\ufeff2026-10-24 10:00:00 | GRG | TID1 | VENDOR=GRG\r\n'.encode('utf-16')
    assert p.decode_text(raw).startswith('2026-10-24 10:00:00 | GRG')


def test_utf16le_without_bom_is_detected_from_nuls():
    raw = '2026-10-24 10:00:00 | GRG\n'.encode('utf-16-le')
    assert p.decode_text(raw) == '2026-10-24 10:00:00 | GRG\n'


def test_two_digit_year_fetch_rejects_not_supported(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, b'ZBX_NOTSUPPORTED: Cannot open file', b'')
    monkeypatch.setattr(p.subprocess, 'run', fake_run)
    raw, reason = p.zabbix_fetch('10.0.0.1', r'D:\x\EJ261024.log')
    assert raw is None
    assert 'ZBX_NOTSUPPORTED' in reason


def test_missing_two_digit_name_falls_back_to_four_digit(monkeypatch, tmp_path):
    day = dt.date.today()
    calls = []

    def fake_fetch(ip, remote):
        calls.append(remote)
        if remote.endswith('EJ261024.log'):
            return None, 'not found'
        return ('\ufeff2026-10-24 10:00:00 | GRG | TID1 | VENDOR=GRG\n'
                ).encode('utf-16'), None

    monkeypatch.setattr(p, 'OUTDIR', tmp_path / 'ej-logs')
    monkeypatch.setattr(p, 'RAWDIR', tmp_path / 'ej-logs' / 'raw')
    monkeypatch.setattr(p, 'zabbix_fetch', fake_fetch)

    status, name, detail = p.pull_one(
        'ATM-1', '10.0.0.1', 'TESTGRG', day, r'D:\Log')
    assert status == 'ok'
    assert name == f'{day:%Y-%m-%d}_TESTGRG_EJDATA.log'
    assert (tmp_path / 'ej-logs' / name).read_text().startswith('2026-10-24 10:00:00')
    assert (tmp_path / 'ej-logs' / 'raw' / name).exists(), 'raw evidence missing'
    assert calls and calls[0].endswith('.log')


def test_force_refetches_a_closed_day_after_a_late_transaction(monkeypatch, tmp_path):
    day = dt.date.today() - dt.timedelta(days=1)
    out = tmp_path / 'ej-logs'
    raw = tmp_path / 'ej-logs' / 'raw'
    raw.mkdir(parents=True)
    name = p.local_name(day, 'TESTGRG')
    (out / name).write_text('old\n')
    (raw / name).write_bytes(b'old\n')
    calls = []

    def fake_fetch(ip, remote):
        calls.append(remote)
        return ('2026-10-24 10:00:00 | GRG | TID1 | VENDOR=GRG\n').encode('utf-8'), None

    monkeypatch.setattr(p, 'OUTDIR', out)
    monkeypatch.setattr(p, 'RAWDIR', raw)
    monkeypatch.setattr(p, 'zabbix_fetch', fake_fetch)

    status, _, _ = p.pull_one('ATM-1', '10.0.0.1', 'TESTGRG', day,
                              r'D:\Log', force=False)
    assert status == 'skipped'
    assert calls == []
    status, _, _ = p.pull_one('ATM-1', '10.0.0.1', 'TESTGRG', day,
                              r'D:\Log', force=True)
    assert status == 'ok'
    assert calls


def test_append_only_avoids_the_filebeat_truncation_duplicate(tmp_path):
    path = tmp_path / 'j.log'
    assert p.write_append_only(path, b'one\n') == 'replaced'
    assert p.write_append_only(path, b'one\ntwo\n') == 'appended'
    assert path.read_bytes() == b'one\ntwo\n'
    assert p.write_append_only(path, b'one\ntwo\n') == 'unchanged'
    assert p.write_append_only(path, b'three\n') == 'replaced'
    assert path.read_bytes() == b'three\n'
