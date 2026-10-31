"""Route-level tests for the admin agent surface.

The pure modules have their own tests. These exercise the HTTP edge: the page
must render for an admin, refuse everyone else, download a signed profile, and
reject bad identity/root input without writing it.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest
from flask import Blueprint, Flask

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import blueprints.agent as agent_mod  # noqa: E402
from agent_profile import build_profile, load_manifest  # noqa: E402
from atm_identity import parse  # noqa: E402
from helpers import (state_colour, state_ink, state_label, state_legend,  # noqa: E402
                     state_tone)

ATM = {
    'atm_id': 'ATM-TEST-NCR',
    'atm_name': 'Test ATM',
    'vendor': 'NCR',
    'model': 'NCR SelfServ 82',
    'district': 'NAD',
    'city': 'Addis Ababa',
    'ip_address': '10.41.30.10',
    'terminal_id': 'TID006',
    'ej_hostname': 'ATM-TEST-NCR',
    'ej_log_format': 'ncr',
    'image_roots': ['C:\\ATM\\images'],
}

PROFILE = build_profile(
    ATM, load_manifest(), secret='test-secret',
    now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
PROBE = next(p for p in PROFILE['probes'] if p['name'] == 'ncr-xfs-probe.ps1')

IDENTITY = parse({
    'schema': 1,
    'collected_at': '2026-10-30T09:00:00Z',
    'computer': 'ATM-PC-01',
    'vendor': 'NCR',
    'vendor_source': 'program-files',
    'model': 'NCR SelfServ 82',
    'serial': 'SN-123',
    'terminal_id': 'TID006',
    'terminal_source': 'ej-header',
    'ej': {
        'hostname': 'ATM-TEST-NCR',
        'hostname_source': 'ej-filename',
        'roots': ['E:\\EJLogs'],
        'newest_file': '2026-10-12_TESTNCR_EJDATA.log',
        'format': 'ncr',
    },
    'images': {'roots': ['C:\\ATM\\images'], 'candidates': []},
    'ip_addresses': ['10.41.30.10'],
    'probes': {
        'ncr-xfs-probe.ps1': {'sha256': PROBE['sha256'], 'version': PROBE['version']},
    },
    'agent': {'version': '6.4.0', 'config_hash': 'b' * 64},
})

STORED = {
    'reported_at': '2026-10-30 09:00',
    'schema': 1,
    'payload': IDENTITY,
    'terminal_id': IDENTITY['terminal_id'],
    'terminal_source': IDENTITY['terminal_source'],
    'ej_hostname': IDENTITY['ej']['hostname'],
    'vendor': IDENTITY['vendor'],
    'probes': IDENTITY['probes'],
    'conflicts': [],
    'received_by': 'admin',
}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('ATM_PROFILE_SECRET', 'test-secret')
    monkeypatch.setattr(agent_mod, '_profile_for', lambda atm_id: (ATM, PROFILE, ''))
    monkeypatch.setattr(agent_mod, '_atm', lambda atm_id: ATM)
    monkeypatch.setattr(agent_mod, 'load_identity', lambda atm_id: STORED)
    monkeypatch.setattr(agent_mod, 'log_action', lambda *args, **kwargs: None)

    app = Flask(
        __name__,
        template_folder=str(REPO / 'report-portal' / 'templates'),
        static_folder=str(REPO / 'report-portal' / 'static'),
    )
    app.secret_key = 'test'
    app.register_blueprint(agent_mod.bp)

    admin_bp = Blueprint('admin', __name__)

    @admin_bp.route('/atms/<atm_id>')
    def atm_detail(atm_id):
        return 'atm'

    app.register_blueprint(admin_bp)

    auth_bp = Blueprint('auth', __name__)

    @auth_bp.route('/login')
    def login():
        return 'login'

    @auth_bp.route('/profile')
    def profile():
        return 'profile'

    app.register_blueprint(auth_bp)

    app.jinja_env.filters.update(
        state_tone=state_tone, state_colour=state_colour,
        state_ink=state_ink, state_label=state_label)
    app.jinja_env.globals.update(
        state_legend=state_legend, csrf_token=lambda: 'token')

    @app.context_processor
    def base_context():
        return {
            'grafana_url': '#', 'zabbix_ui_url': '#', 'glpi_ui_url': '#',
            'txn_note': '', 'txn_level': 'info',
        }

    return app


def login(client, role='admin'):
    with client.session_transaction() as session:
        session['logged_in'] = True
        session['role'] = role
        session['username'] = 'admin'


def test_an_admin_can_open_the_agent_page(app):
    with app.test_client() as client:
        login(client)
        response = client.get('/atms/ATM-TEST-NCR/agent')
    assert response.status_code == 200
    assert b'Agent profile' in response.data
    assert b'Desired state' in response.data
    assert b'Reported identity' in response.data
    assert b'atm-identity.ps1' in response.data


def test_a_missing_profile_and_identity_still_render(app, monkeypatch):
    monkeypatch.setattr(agent_mod, '_profile_for',
                        lambda atm_id: (ATM, None, 'manifest missing'))
    monkeypatch.setattr(agent_mod, 'load_identity', lambda atm_id: None)
    with app.test_client() as client:
        login(client)
        response = client.get('/atms/ATM-TEST-NCR/agent')
    assert response.status_code == 200
    assert b'manifest missing' in response.data
    assert b'No identity has been reported' in response.data


def test_anonymous_and_viewer_requests_are_refused(app):
    with app.test_client() as client:
        anonymous = client.get('/atms/ATM-TEST-NCR/agent')
        login(client, role='viewer')
        viewer = client.get('/atms/ATM-TEST-NCR/agent')
    assert anonymous.status_code == 302
    assert '/login' in anonymous.headers['Location']
    assert viewer.status_code == 403


def test_profile_download_is_signed_and_logged(app, monkeypatch):
    logged = []
    monkeypatch.setattr(agent_mod, 'log_action',
                        lambda action, detail=None: logged.append((action, detail)))
    with app.test_client() as client:
        login(client)
        response = client.get('/atms/ATM-TEST-NCR/agent/profile.json')
    assert response.status_code == 200
    assert response.headers['X-Profile-Sha256'] == PROFILE['profile_hash']
    assert json.loads(response.data)['profile_hash'] == PROFILE['profile_hash']
    assert response.headers['Content-Disposition'].endswith(
        'filename="ATM-TEST-NCR.profile.json"')
    assert logged and logged[0][0] == 'AGENT_PROFILE_DOWNLOAD'


def test_a_valid_identity_is_stored_with_its_conflicts(app, monkeypatch):
    saved = []
    monkeypatch.setattr(agent_mod, 'save_identity',
                        lambda atm_id, identity, conflicts, received_by='':
                        saved.append((atm_id, identity, conflicts, received_by)))
    with app.test_client() as client:
        login(client)
        response = client.post(
            '/atms/ATM-TEST-NCR/agent/identity',
            data={'payload': json.dumps(IDENTITY)})
    assert response.status_code == 302
    assert saved
    atm_id, identity, conflicts, received_by = saved[0]
    assert atm_id == 'ATM-TEST-NCR'
    assert identity['terminal_id'] == 'TID006'
    assert conflicts == []
    assert received_by == 'admin'


def test_a_bad_identity_is_rejected_without_writing(app, monkeypatch):
    saved = []
    monkeypatch.setattr(agent_mod, 'save_identity',
                        lambda *args, **kwargs: saved.append(args))
    with app.test_client() as client:
        login(client)
        response = client.post(
            '/atms/ATM-TEST-NCR/agent/identity',
            data={'payload': '{"schema": 2}'},
            follow_redirects=True)
    assert response.status_code == 200
    assert b'Identity rejected' in response.data
    assert saved == []


def test_identity_json_returns_400_for_invalid_and_503_for_storage_failure(
        app, monkeypatch):
    with app.test_client() as client:
        login(client)
        bad = client.post('/atms/ATM-TEST-NCR/agent/identity',
                          json={'schema': 2})
        monkeypatch.setattr(agent_mod, 'save_identity',
                            lambda *args, **kwargs: (_ for _ in ()).throw(
                                RuntimeError('db down')))
        down = client.post('/atms/ATM-TEST-NCR/agent/identity', json=IDENTITY)
    assert bad.status_code == 400
    assert bad.get_json()['ok'] is False
    assert down.status_code == 503
    assert down.get_json()['ok'] is False


def test_image_roots_are_validated_before_storage(app, monkeypatch):
    saved = []
    monkeypatch.setattr(agent_mod, 'set_image_roots',
                        lambda atm_id, roots: saved.append((atm_id, roots)))
    with app.test_client() as client:
        login(client)
        good = client.post(
            '/atms/ATM-TEST-NCR/agent/roots',
            data={'roots': 'C:\\ATM\\images\n# comment\nC:\\ATM\\other'})
        bad = client.post(
            '/atms/ATM-TEST-NCR/agent/roots',
            data={'roots': '\\\\server\\share'},
            follow_redirects=True)
    assert good.status_code == 302
    assert saved == [('ATM-TEST-NCR', ['C:\\ATM\\images', 'C:\\ATM\\other'])]
    assert b'Image roots not saved' in bad.data
