"""HTTP boundary for the governed report builder.

The engine is tested separately.  These tests cover the parts that could leak
or mislead at the edge: subject validation, role rules for saved definitions,
and the fact that exports go through the same validated definition as the
preview (never a second, looser query path).
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'report-portal'))

import blueprints.report_builder as rb  # noqa: E402
import report_engine  # noqa: E402


VALID_DEFINITION = {
    'dataset': 'transactions',
    'group_by': ['district'],
    'measures': ['txn_count', 'success_rate'],
    'filters': {'date': {'preset': 'last_7_days'}},
    'limit': 500,
}

RESULT = {
    'dataset': 'transactions',
    'dataset_label': 'Transactions',
    'title': 'Transactions by District',
    'subtitle': 'Last 7 days · Active ATMs',
    'period_label': 'Last 7 days (EAT)',
    'period_days': 7,
    'columns': [
        {'id': 'district', 'label': 'District', 'type': 'text',
         'kind': 'dimension', 'description': ''},
        {'id': 'txn_count', 'label': 'Transactions', 'type': 'number',
         'kind': 'measure', 'description': ''},
    ],
    'rows': [["=Bole", 12]],
    'totals': [None, 12],
    'row_count': 1,
    'limit': 500,
    'truncated': False,
    'generated_at': '2026-11-06 12:00 EAT',
    'duration_ms': 12,
    'warnings': [],
    'definition_hash': 'abc123',
    'definition': VALID_DEFINITION,
}


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _app(monkeypatch):
    monkeypatch.setattr(rb, 'get_db', lambda: _Conn())
    monkeypatch.setattr(rb, 'log_action', lambda *a, **k: None)
    app = Flask(__name__,
                template_folder=os.path.join(ROOT, 'report-portal',
                                             'templates'))
    app.secret_key = 'test'
    app.config['WTF_CSRF_ENABLED'] = False
    app.jinja_env.globals['csrf_token'] = lambda: 'test'
    app.register_blueprint(rb.bp)
    return app


def _login(client, role='viewer', username='alice'):
    with client.session_transaction() as session:
        session['logged_in'] = True
        session['role'] = role
        session['username'] = username


# ── Page and catalog ────────────────────────────────────────────────────────

def test_builder_page_renders_the_catalog(monkeypatch):
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    response = client.get('/reports/builder')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Report Builder' in body
    assert 'Governed report builder.' in body
    assert 'id="fieldPickerModal"' in body
    assert 'id="filterModal"' in body
    assert 'id="conditionList"' in body
    assert 'Search fields and measures' in body
    assert 'BUILDER_JS' not in body


def test_builder_hidden_states_are_not_beaten_by_display_rules(monkeypatch):
    """The builder toggles empty/loading/error, custom-date and modal blocks
    with the hidden attribute. Their own display:flex rules beat the browser's
    [hidden] rule, so every state rendered at once - including "No preview yet"
    and "Building report..." below a finished table. Found in a screenshot."""
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    body = client.get('/reports/builder').get_data(as_text=True)
    assert '[hidden]{display:none !important}' in body


def test_catalog_endpoint_never_exposes_sql(monkeypatch):
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    response = client.get('/api/reports/catalog')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['datasets']
    blob = json.dumps(payload)
    assert 'SELECT ' not in blob
    assert 'atm_transactions_visible' not in blob


# ── Preview ────────────────────────────────────────────────────────────────

def test_preview_rejects_an_invalid_definition_before_querying(monkeypatch):
    app = _app(monkeypatch)
    called = []
    monkeypatch.setattr(rb.report_engine, 'run_report',
                        lambda *a, **k: called.append(a) or RESULT)
    client = app.test_client()
    _login(client)
    response = client.post('/api/reports/preview',
                           json={'dataset': 'sql', 'measures': ['x']})
    assert response.status_code == 400
    assert 'valid report subject' in response.get_json()['error']
    assert called == []


def test_preview_uses_the_validated_definition(monkeypatch):
    app = _app(monkeypatch)
    seen = {}

    def fake_run(conn, definition, **kwargs):
        seen['definition'] = definition
        seen['kwargs'] = kwargs
        return RESULT

    monkeypatch.setattr(rb.report_engine, 'run_report', fake_run)
    client = app.test_client()
    _login(client)
    response = client.post('/api/reports/preview',
                           json={'definition': VALID_DEFINITION})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['title'] == 'Transactions by District'
    assert seen['definition'].dataset.id == 'transactions'
    # Preview never asks for the export-sized row count.
    assert seen['kwargs']['limit'] <= report_engine.PREVIEW_ROW_LIMIT


# ── Exports ────────────────────────────────────────────────────────────────

def test_csv_export_is_parameterised_and_neutralises_formulas(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_engine, 'run_report',
                        lambda *a, **k: RESULT)
    client = app.test_client()
    _login(client)
    response = client.post('/api/reports/export/csv',
                           json={'definition': VALID_DEFINITION})
    assert response.status_code == 200
    assert 'text/csv' in response.headers['Content-Type']
    assert 'attachment' in response.headers['Content-Disposition']
    body = response.data.decode('utf-8-sig')
    assert "'=Bole" in body
    assert ',=Bole' not in body
    assert 'Last 7 days' in body


def test_excel_export_builds_a_workbook_with_a_total_row(monkeypatch):
    from openpyxl import load_workbook

    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_engine, 'run_report',
                        lambda *a, **k: RESULT)
    client = app.test_client()
    _login(client)
    response = client.post('/api/reports/export/excel',
                           json={'definition': VALID_DEFINITION})
    assert response.status_code == 200
    assert 'spreadsheetml' in response.headers['Content-Type']
    assert response.data[:2] == b'PK'

    workbook = load_workbook(io.BytesIO(response.data))
    sheet = workbook.active
    values = [cell.value for row in sheet.iter_rows() for cell in row]
    assert 'Total' in values
    # The leading apostrophe is intentional: Excel must not treat the branch
    # name as a formula.
    assert "'=Bole" in values


def test_pdf_export_renders_data_quality_notes(monkeypatch):
    extra = dict(RESULT)
    extra['warnings'] = ['3 active ATMs have no district; they are grouped as Unassigned.']
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_engine, 'run_report',
                        lambda *a, **k: extra)
    client = app.test_client()
    _login(client)
    response = client.post('/api/reports/export/pdf',
                           json={'definition': VALID_DEFINITION})
    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/pdf'
    assert response.data[:4] == b'%PDF'


def test_unknown_export_format_is_a_404(monkeypatch):
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    response = client.post('/api/reports/export/word',
                           json={'definition': VALID_DEFINITION})
    assert response.status_code == 404


# ── Saved definitions ──────────────────────────────────────────────────────

def test_saved_reports_are_listed_for_the_signed_in_user(monkeypatch):
    app = _app(monkeypatch)
    seen = {}

    def fake_list(conn, username, role):
        seen.update(username=username, role=role)
        return [{'id': 3, 'name': 'District volume', 'owner': 'alice'}]

    monkeypatch.setattr(rb.report_store, 'list_definitions', fake_list)
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.get('/api/reports/definitions')
    assert response.status_code == 200
    assert response.get_json()['definitions'][0]['name'] == 'District volume'
    assert seen == {'username': 'alice', 'role': 'viewer'}


def test_a_viewer_may_save_a_private_report(monkeypatch):
    app = _app(monkeypatch)
    captured = {}

    def fake_create(conn, **kwargs):
        captured.update(kwargs)
        return {'id': 7, 'name': kwargs['name'], 'visibility': 'private',
                'description': '', 'dataset': kwargs['dataset'],
                'definition': kwargs['payload'], 'owner': kwargs['owner'],
                'is_system': False, 'version': 1,
                'created_at': '', 'updated_at': ''}

    monkeypatch.setattr(rb.report_store, 'create_definition', fake_create)
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.post('/api/reports/definitions', json={
        'name': 'Weekly district volume',
        'description': 'Monday pack',
        'definition': VALID_DEFINITION,
    })
    assert response.status_code == 201
    assert response.get_json()['id'] == 7
    assert captured['owner'] == 'alice'
    assert captured['visibility'] == 'private'
    assert captured['payload']['group_by'] == ['district']


def test_a_viewer_cannot_share_a_report(monkeypatch):
    app = _app(monkeypatch)
    called = []
    monkeypatch.setattr(rb.report_store, 'create_definition',
                        lambda *a, **k: called.append(k))
    client = app.test_client()
    _login(client, role='viewer')
    response = client.post('/api/reports/definitions', json={
        'name': 'Shared by viewer',
        'visibility': 'shared',
        'definition': VALID_DEFINITION,
    })
    assert response.status_code == 403
    assert called == []


def test_an_admin_may_share_a_report(monkeypatch):
    app = _app(monkeypatch)
    captured = {}
    monkeypatch.setattr(rb.report_store, 'create_definition',
                        lambda conn, **kwargs: captured.update(kwargs) or {
                            'id': 8, 'name': kwargs['name'],
                            'visibility': kwargs['visibility'],
                            'description': '', 'dataset': kwargs['dataset'],
                            'definition': kwargs['payload'],
                            'owner': kwargs['owner'], 'is_system': False,
                            'version': 1, 'created_at': '', 'updated_at': ''})
    client = app.test_client()
    _login(client, role='admin')
    response = client.post('/api/reports/definitions', json={
        'name': 'Shared report',
        'visibility': 'shared',
        'definition': VALID_DEFINITION,
    })
    assert response.status_code == 201
    assert captured['visibility'] == 'shared'


def test_a_viewer_cannot_edit_someone_elses_report(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda conn, definition_id: {
                            'id': definition_id, 'owner': 'bob',
                            'is_system': False})
    called = []
    monkeypatch.setattr(rb.report_store, 'update_definition',
                        lambda *a, **k: called.append(k))
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.put('/api/reports/definitions/3', json={
        'name': 'Hijack',
        'definition': VALID_DEFINITION,
    })
    assert response.status_code == 403
    assert called == []


def test_a_system_report_cannot_be_deleted(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda conn, definition_id: {
                            'id': definition_id, 'owner': 'system',
                            'is_system': True})
    called = []
    monkeypatch.setattr(rb.report_store, 'delete_definition',
                        lambda *a, **k: called.append(a))
    client = app.test_client()
    _login(client, role='admin')
    response = client.delete('/api/reports/definitions/4')
    assert response.status_code == 403
    assert called == []


def test_the_owner_can_delete_a_private_report(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda conn, definition_id: {
                            'id': definition_id, 'name': 'Mine',
                            'owner': 'alice', 'is_system': False})
    monkeypatch.setattr(rb.report_store, 'delete_definition',
                        lambda conn, definition_id: True)
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.delete('/api/reports/definitions/5')
    assert response.status_code == 200
    assert response.get_json() == {'success': True}


def test_saved_definition_names_are_required_and_bounded(monkeypatch):
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    missing = client.post('/api/reports/definitions', json={
        'name': '   ', 'definition': VALID_DEFINITION})
    assert missing.status_code == 400
    too_long = client.post('/api/reports/definitions', json={
        'name': 'x' * 121, 'definition': VALID_DEFINITION})
    assert too_long.status_code == 400


# ── Custom dropdowns and scheduled delivery ───────────────────────────────

def test_builder_uses_the_shared_custom_picker(monkeypatch):
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    body = client.get('/reports/builder').get_data(as_text=True)
    for select_id in ('datasetSelect', 'periodPreset', 'scopeSelect',
                      'sortField', 'sortDirection', 'filterOperator',
                      'saveVisibility'):
        marker = f'<select id="{select_id}" class="builder-select">'
        assert marker in body
        assert f'<select id="{select_id}" class="builder-select" data-native>' \
            not in body
    assert "refreshSelect('datasetSelect')" in body
    assert "refreshSelect('filterOperator')" in body
    assert '.builder-card.select-open' in body


def test_builder_exposes_scheduling_for_saved_reports(monkeypatch):
    app = _app(monkeypatch)
    client = app.test_client()
    _login(client)
    body = client.get('/reports/builder').get_data(as_text=True)
    assert 'id="scheduleModal"' in body
    assert 'Schedule report' in body
    assert 'id="scheduleCron"' in body
    assert 'id="scheduleRecipients"' in body
    assert 'data-schedule="' in body
    assert '/api/reports/definitions/' in body
    assert "'/schedules'" in body


def test_saved_reports_expose_a_schedule_badge(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_store, 'list_definitions',
                        lambda conn, username, role: [
                            {'id': 3, 'name': 'District volume',
                             'owner': 'alice', 'dataset': 'transactions',
                             'definition': {}, 'visibility': 'private',
                             'is_system': False, 'updated_at': ''}])
    monkeypatch.setattr(rb, '_schedule_counts', lambda conn: {3: 2})
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    payload = client.get('/api/reports/definitions').get_json()
    assert payload['definitions'][0]['schedule_count'] == 2


def test_schedule_payload_validates_cron_recipients_and_format():
    valid = {
        'name': 'Weekly districts',
        'schedule': '0 7 * * 1',
        'format': 'pdf',
        'recipients': 'ops@dashenbank.com, data@dashenbank.com',
    }
    parsed = rb._schedule_payload(valid)
    assert parsed['recipients'] == 'ops@dashenbank.com, data@dashenbank.com'
    assert parsed['enabled'] is True

    for field, value, text in (
        ('schedule', '0 7 * *', '5 fields'),
        ('recipients', 'not-an-email', 'Invalid email'),
        ('format', 'word', 'PDF, Excel or CSV'),
        ('name', '', 'name'),
    ):
        data = dict(valid)
        data[field] = value
        with pytest.raises(report_engine.ReportDefinitionError) as exc:
            rb._schedule_payload(data)
        assert text.lower() in str(exc.value).lower()


class _ScheduleCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.statements.append((' '.join(sql.split()), params))
        if 'INSERT INTO scheduled_reports' in sql:
            self._row = (11, params[0], params[1], params[2], params[3],
                         params[5], None, None, None)
        elif 'SELECT params FROM scheduled_reports' in sql:
            self._rows = [('definition_id=9',)]
        else:
            self._row = None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _ScheduleConn:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _ScheduleCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_the_owner_can_schedule_a_saved_report(monkeypatch):
    app = _app(monkeypatch)
    conn = _ScheduleConn()
    monkeypatch.setattr(rb, 'get_db', lambda: conn)
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda c, definition_id: {
                            'id': definition_id, 'name': 'District volume',
                            'owner': 'alice', 'visibility': 'private',
                            'is_system': False,
                            'definition': VALID_DEFINITION})
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.post('/api/reports/definitions/9/schedules', json={
        'name': 'Monday district pack',
        'schedule': '0 7 * * 1',
        'format': 'excel',
        'recipients': 'ops@dashenbank.com',
    })
    assert response.status_code == 201
    assert response.get_json()['id'] == 11
    insert = [item for item in conn.statements
              if item[0].startswith('INSERT INTO scheduled_reports')]
    assert insert
    assert 'definition_id=9' in insert[0][1]
    assert insert[0][1][1] == 'excel'


def test_a_viewer_cannot_schedule_someone_elses_report(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda c, definition_id: {
                            'id': definition_id, 'name': 'Private',
                            'owner': 'bob', 'visibility': 'private',
                            'is_system': False})
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.get('/api/reports/definitions/9/schedules')
    assert response.status_code == 403


def test_the_scheduler_can_generate_a_saved_report_internally(monkeypatch):
    import blueprints.auth as auth
    from flask import Response

    app = _app(monkeypatch)
    monkeypatch.setattr(auth, 'INTERNAL_API_KEY', 'secret-key')
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda conn, definition_id: {
                            'id': definition_id, 'name': 'District volume',
                            'owner': 'alice', 'visibility': 'private',
                            'is_system': False,
                            'definition': VALID_DEFINITION})
    monkeypatch.setattr(rb.report_engine, 'run_report',
                        lambda *a, **k: RESULT)
    seen = {}

    def fake_render(fmt, result):
        seen.update(fmt=fmt, result=result)
        return Response(b'file-bytes', mimetype='text/csv')

    monkeypatch.setattr(rb.report_exports, 'render', fake_render)
    client = app.test_client()
    response = client.get('/report/custom/9/csv',
                          headers={'X-Internal-Key': 'secret-key'})
    assert response.status_code == 200
    assert response.data == b'file-bytes'
    assert seen['fmt'] == 'csv'
    assert seen['result']['title'] == 'Transactions by District'


def test_a_private_saved_report_is_not_downloadable_by_another_user(monkeypatch):
    app = _app(monkeypatch)
    monkeypatch.setattr(rb.report_store, 'get_definition',
                        lambda conn, definition_id: {
                            'id': definition_id, 'name': 'Private',
                            'owner': 'bob', 'visibility': 'private',
                            'is_system': False,
                            'definition': VALID_DEFINITION})
    client = app.test_client()
    _login(client, role='viewer', username='alice')
    response = client.get('/report/custom/9/csv')
    assert response.status_code == 403
