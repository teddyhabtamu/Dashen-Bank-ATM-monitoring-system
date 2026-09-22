"""Directory cutover checker: mock LDAP lab today, bank AD on cutover day.

Runs the acceptance matrix from docs/AD_Cutover_Runbook.md §6 against the
REAL portal auth code (report-portal/blueprints/auth.py::_ldap_auth) plus a
live consistency check of config/grafana/ldap.toml against the directory.

Target is 100% environment-driven — no values committed::

    # Lab (defaults below match config/ldap/seed.ldif):
    venv/bin/python -m pytest tests/test_directory.py -q
    # AD day (values from IAM, never in git):
    DIR_HOST=<ad-host> DIR_PORT=636 DIR_USE_SSL=true \\
    DIR_BASE_DN='DC=bank,DC=local' DIR_BIND_DN='<svc DN>' \\
    DIR_BIND_PASSWORD='<from IAM>' DIR_LOGIN_ATTR=sAMAccountName \\
    DIR_ADMINS_GROUP='<admins CN>' DIR_OPERATORS_GROUP='<operators CN>' \\
    DIR_OPS_USER=<t-user> DIR_OPS_PASS=<...> \\
    DIR_ADMIN_USER=<a-user> DIR_ADMIN_PASS=<...> \\
    venv/bin/python -m pytest tests/test_directory.py -q
    # Leaver test (after disabling the test account in the directory):
    DIR_MODE=locked DIR_OPS_USER=<disabled-user> DIR_OPS_PASS=<old-pass> ...

Skips (never fails) when the directory is unreachable — the suite must stay
green on machines without the lab running. Needs `ldap3` in the venv.
"""
import importlib.util
import os
import re
import socket
import sys
import types
from pathlib import Path

import pytest

try:
    from ldap3 import Server, Connection, ALL
    _LDAP3 = True
except ImportError:
    _LDAP3 = False

REPO = Path(__file__).resolve().parent.parent

# ─── Target (lab defaults; override per run) ────────────────────────────────
HOST = os.environ.get('DIR_HOST', '127.0.0.1')
PORT = int(os.environ.get('DIR_PORT', '3890'))
USE_SSL = os.environ.get('DIR_USE_SSL', 'false').lower() == 'true'
BASE_DN = os.environ.get('DIR_BASE_DN', 'dc=atmmon,dc=local')
BIND_DN = os.environ.get('DIR_BIND_DN', 'cn=admin,dc=atmmon,dc=local')
BIND_PASSWORD = os.environ.get('DIR_BIND_PASSWORD', 'LabAdminPass123!')
LOGIN_ATTR = os.environ.get('DIR_LOGIN_ATTR', 'uid')
ADMINS_GROUP = os.environ.get('DIR_ADMINS_GROUP', 'ATM-Admins')
OPERATORS_GROUP = os.environ.get('DIR_OPERATORS_GROUP', 'ATM-Operators')
OPS_USER = os.environ.get('DIR_OPS_USER', 'ops1')
OPS_PASS = os.environ.get('DIR_OPS_PASS', 'OpsPass123!')
ADMIN_USER = os.environ.get('DIR_ADMIN_USER', 'admin1')
ADMIN_PASS = os.environ.get('DIR_ADMIN_PASS', 'AdminPass123!')
MODE = os.environ.get('DIR_MODE', 'matrix')  # matrix | locked

TOML = REPO / 'config' / 'grafana' / 'ldap.toml'


def _reachable():
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _server():
    return Server(HOST, port=PORT, use_ssl=USE_SSL, get_info=ALL,
                  connect_timeout=5)


@pytest.fixture(scope='module', autouse=True)
def _isolate_stubs():
    """The portal-auth import below stubs framework modules; keep the stubs
    inside this file so sibling test files import the real world."""
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    saved_env = dict(os.environ)
    yield
    sys.modules.clear()
    sys.modules.update(saved_modules)
    sys.path[:] = saved_path
    os.environ.clear()
    os.environ.update(saved_env)


@pytest.fixture(scope='module')
def directory():
    if not _LDAP3:
        pytest.skip('ldap3 not installed in venv')
    if not _reachable():
        pytest.skip(f'directory {HOST}:{PORT} unreachable '
                    f'(start it: docker compose --profile ldap up -d mock-ldap)')
    return _server()


def _load_portal_auth():
    """Import the REAL portal auth module (framework imports stubbed)."""
    sys.path.insert(0, str(REPO / 'report-portal'))
    flask = types.ModuleType('flask')

    class _BP:
        def __init__(self, *a, **k):
            pass

        def route(self, *a, **k):
            def deco(f):
                return f
            return deco

    for n in ('Blueprint', 'render_template', 'request', 'redirect',
              'session', 'url_for', 'current_app', 'jsonify', 'flash',
              'Response', 'abort'):
        setattr(flask, n, _BP if n == 'Blueprint'
                else (lambda *a, **k: None))
    sys.modules['flask'] = flask
    wz = types.ModuleType('werkzeug')
    sec = types.ModuleType('werkzeug.security')
    sec.generate_password_hash = lambda *a, **k: ''
    sec.check_password_hash = lambda *a, **k: False
    wz.security = sec
    sys.modules['werkzeug'] = wz
    sys.modules['werkzeug.security'] = sec
    audit = types.ModuleType('audit')
    audit.log_action = lambda *a, **k: None
    sys.modules['audit'] = audit
    db = types.ModuleType('db')
    db.get_db = lambda *a, **k: None
    sys.modules['db'] = db
    pkg = types.ModuleType('blueprints')
    pkg.__path__ = []
    sys.modules['blueprints'] = pkg
    authm = types.ModuleType('blueprints.auth')
    authm.login_required = lambda f: f
    authm.role_required = lambda *a, **k: (lambda f: f)
    authm.ROLE_ADMIN = 'admin'
    authm.ROLE_OPERATOR = 'operator'
    authm.ROLE_VIEWER = 'viewer'
    sys.modules['blueprints.auth'] = authm
    for var, val in {
            'LDAP_HOST': HOST, 'LDAP_PORT': str(PORT),
            'LDAP_BASE_DN': BASE_DN, 'LDAP_BIND_DN': BIND_DN,
            'LDAP_BIND_PASSWORD': BIND_PASSWORD,
            'LDAP_LOGIN_ATTR': LOGIN_ATTR,
            'LDAP_ADMINS_GROUP': ADMINS_GROUP,
            'LDAP_OPERATORS_GROUP': OPERATORS_GROUP}.items():
        os.environ[var] = val
    spec = importlib.util.spec_from_file_location(
        'portal_auth', REPO / 'report-portal' / 'blueprints' / 'auth.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def portal_auth():
    if not _LDAP3:
        pytest.skip('ldap3 not installed in venv')
    if not _reachable():
        pytest.skip(f'directory {HOST}:{PORT} unreachable')
    return _load_portal_auth()


# ─── Matrix: normal cutover ─────────────────────────────────────────────────
class TestMatrix:
    def test_operator_login(self, portal_auth):
        if MODE != 'matrix':
            pytest.skip('locked mode')
        ok, role, reachable = portal_auth._ldap_auth(OPS_USER, OPS_PASS)
        assert reachable is True
        assert ok is True and role == 'operator'

    def test_admin_login(self, portal_auth):
        if MODE != 'matrix':
            pytest.skip('locked mode')
        ok, role, reachable = portal_auth._ldap_auth(ADMIN_USER, ADMIN_PASS)
        assert reachable is True
        assert ok is True and role == 'admin'

    def test_wrong_password_rejected_without_fallback(self, portal_auth):
        if MODE != 'matrix':
            pytest.skip('locked mode')
        ok, role, reachable = portal_auth._ldap_auth(OPS_USER, 'wrong!')
        assert (ok, reachable) == (False, True)

    def test_unknown_user_rejected(self, portal_auth):
        if MODE != 'matrix':
            pytest.skip('locked mode')
        ok, role, reachable = portal_auth._ldap_auth(
            'no-such-user-xyz', 'whatever')
        assert (ok, reachable) == (False, True)

    def test_role_groups_exist(self, directory):
        if MODE != 'matrix':
            pytest.skip('locked mode')
        conn = Connection(directory, user=BIND_DN, password=BIND_PASSWORD,
                          auto_bind=True)
        for group in (ADMINS_GROUP, OPERATORS_GROUP):
            found = conn.search(
                BASE_DN, f'(&(objectClass=groupOfNames)(cn={group}))',
                attributes=['cn'])
            if not found:
                # AD names groups objectClass=group; accept either schema.
                found = conn.search(
                    BASE_DN, f'(&(objectClass=group)(cn={group}))',
                    attributes=['cn'])
            assert found, f'role group missing: {group}'


# ─── Locked: leaver test (account disabled in the directory) ────────────────
class TestLocked:
    def test_disabled_account_cannot_bind(self, portal_auth):
        if MODE != 'locked':
            pytest.skip('matrix mode (set DIR_MODE=locked after '
                        'disabling the test account)')
        ok, role, reachable = portal_auth._ldap_auth(OPS_USER, OPS_PASS)
        assert ok is False, 'disabled account still authenticates!'


# ─── Grafana ldap.toml ↔ directory consistency ──────────────────────────────
class TestGrafanaConfig:
    def _group_dns(self):
        text = TOML.read_text()
        return re.findall(r'group_dn\s*=\s*"([^"]+)"', text)

    def test_every_mapped_group_dn_exists(self, directory):
        dns = [d for d in self._group_dns() if d != '*']
        assert dns, 'no group mappings in ldap.toml'
        conn = Connection(directory, user=BIND_DN, password=BIND_PASSWORD,
                          auto_bind=True)
        for dn in dns:
            m = re.search(r'cn=([^,]+)', dn, re.IGNORECASE)
            assert m, f'unparseable group_dn: {dn}'
            found = conn.search(
                BASE_DN,
                f'(|(cn={m.group(1)})(distinguishedName={dn}))',
                attributes=['cn'])
            assert found, f'ldap.toml maps {dn} but it is not in the directory'
