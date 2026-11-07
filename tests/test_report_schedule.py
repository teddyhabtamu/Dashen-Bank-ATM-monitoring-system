"""Recurring delivery for saved builder reports.

The scheduler historically generated only the eight fixed management reports
through internal HTTP endpoints.  A saved definition now uses the same route
family (``/report/custom/<id>/<fmt>``) so one email pipeline and one set of
content-type checks covers both.
"""

from __future__ import annotations

import io
import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'report-portal'))

import scheduler  # noqa: E402


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_validate_cron_accepts_presets_and_rejects_bad_fields():
    assert scheduler.validate_cron('0 7 * * 1') is None
    assert scheduler.validate_cron('*/30 * * * *') is None
    assert scheduler.validate_cron('0 6-9 * * 1-5') is None
    assert '5 fields' in scheduler.validate_cron('0 7 * *')
    assert 'Bad cron value' in scheduler.validate_cron('0 25 * * *')
    assert 'Bad cron range' in scheduler.validate_cron('0 9-6 * * *')


def test_custom_schedule_loads_the_saved_definition(monkeypatch):
    calls = {}

    monkeypatch.setattr(scheduler, 'get_db', lambda: _Conn())
    monkeypatch.setattr(scheduler.report_store, 'get_definition',
                        lambda conn, definition_id: {
                            'id': definition_id,
                            'name': 'Weekly district performance'})

    def fake_generate(report_type, fmt, days=7, atm='all', **kwargs):
        calls['generate'] = {
            'report_type': report_type, 'fmt': fmt, 'kwargs': kwargs}
        return io.BytesIO(b'file'), 'report.pdf', 'application/pdf'

    def fake_email(recipients, subject, body, filename, filedata, mimetype):
        calls['email'] = {
            'recipients': recipients, 'subject': subject, 'body': body,
            'filename': filename, 'mimetype': mimetype}
        return True

    monkeypatch.setattr(scheduler, '_generate_report', fake_generate)
    monkeypatch.setattr(scheduler, '_send_email', fake_email)
    monkeypatch.setattr(scheduler, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_record_run_status',
                        lambda *a, **k: calls.setdefault('status', a))

    scheduler._run_scheduled_report(
        7, 'custom', 'pdf', '0 7 * * 1', 'ops@dashenbank.com',
        'definition_id=42')

    assert calls['generate']['report_type'] == 'custom'
    assert calls['generate']['kwargs']['definition_id'] == 42
    assert calls['generate']['kwargs']['report_label'] \
        == 'Weekly district performance'
    assert calls['email']['recipients'] == ['ops@dashenbank.com']
    assert 'Weekly district performance' in calls['email']['subject']
    assert 'Weekly district performance' in calls['email']['body']
    # _record_run_status(report_id, ok, error, schedule)
    assert calls['status'][1] is True


def test_custom_schedule_fails_cleanly_when_the_definition_is_gone(monkeypatch):
    calls = {'generated': False}

    monkeypatch.setattr(scheduler, 'get_db', lambda: _Conn())
    monkeypatch.setattr(scheduler.report_store, 'get_definition',
                        lambda conn, definition_id: None)
    monkeypatch.setattr(scheduler, '_generate_report',
                        lambda *a, **k: calls.update(generated=True))
    monkeypatch.setattr(scheduler, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_record_run_status',
                        lambda *a, **k: calls.setdefault('status', a))

    scheduler._run_scheduled_report(
        7, 'custom', 'pdf', '0 7 * * 1', 'ops@dashenbank.com',
        'definition_id=42')

    assert calls['generated'] is False
    assert calls['status'][1] is False
    assert 'no longer exists' in calls['status'][2]


def test_custom_schedule_without_a_definition_id_is_rejected(monkeypatch):
    calls = {}
    monkeypatch.setattr(scheduler, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_record_run_status',
                        lambda *a, **k: calls.setdefault('status', a))
    scheduler._run_scheduled_report(
        7, 'custom', 'pdf', '0 7 * * 1', 'ops@dashenbank.com', 'days=7')
    assert calls['status'][1] is False
    assert 'definition_id' in calls['status'][2]


def test_custom_generation_uses_the_internal_custom_endpoint(monkeypatch):
    seen = {}

    class _Response:
        status_code = 200
        headers = {'Content-Type': 'application/pdf'}
        content = b'%PDF-1.4 test'

    def fake_get(url, timeout=None, headers=None):
        seen.update(url=url, timeout=timeout, headers=headers)
        return _Response()

    monkeypatch.setattr(requests, 'get', fake_get)
    filedata, filename, mimetype = scheduler._generate_report(
        'custom', 'pdf', definition_id=42,
        report_label='Weekly district performance')

    assert filedata is not None
    assert filename.startswith('Dashen_Bank_Weekly_district_performance_')
    assert filename.endswith('.pdf')
    assert mimetype == 'application/pdf'
    assert seen['url'].endswith('/report/custom/42/pdf')
    assert '/report/custom/' in seen['url']
    assert '?' not in seen['url']
