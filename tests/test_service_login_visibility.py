"""A rejected Zabbix login must be visible, not silent.

Both background services sent `user.login` and took `.get('result')`. A wrong
password is answered with an error OBJECT, not an exception, so the `except`
never fired: the service received None and carried on. For the cash watcher
that is indistinguishable from "these cassettes have no items" - a bad
credential looks exactly like healthy silence, and cash tracking would quietly
stop after a rotation. The network correlator already logged its rejection;
these two did not.
"""
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _stub_psycopg2():
    """Neither service can be imported without its driver, and the test env
    deliberately has no database driver."""
    if 'psycopg2' not in sys.modules:
        fake = types.ModuleType('psycopg2')
        fake.connect = lambda *a, **k: None
        fake.Error = Exception
        sys.modules['psycopg2'] = fake
    yield


def _load(name, path):
    sys.path.insert(0, path)
    sys.modules.pop(name, None)
    return __import__(name)


def test_cash_watcher_logs_a_rejected_login(caplog):
    cw = _load('cash_watcher', REPO)
    cw.zbx_post = lambda payload: {
        'jsonrpc': '2.0', 'id': 1,
        'error': {'code': -32602, 'message': 'Invalid params',
                  'data': 'Login name or password is incorrect.'}}

    with caplog.at_level('WARNING'):
        assert cw.zbx_login() is None

    assert any('rejected' in r.message.lower() for r in caplog.records), (
        'a rejected login must be logged, or it reads as "no data": %s'
        % [r.message for r in caplog.records])
    assert any('password is incorrect' in r.message for r in caplog.records), \
        'the server\'s reason should be in the log'


def test_cash_watcher_still_logs_an_unreachable_server(caplog):
    cw = _load('cash_watcher', REPO)

    def boom(payload):
        raise OSError('connection refused')

    cw.zbx_post = boom
    with caplog.at_level('WARNING'):
        assert cw.zbx_login() is None

    assert any('login failed' in r.message.lower() for r in caplog.records)


def test_state_manager_logs_a_rejected_login(capsys):
    sm = _load('state_manager', os.path.join(REPO, 'simulators'))
    sm.zbx_post = lambda payload: {
        'jsonrpc': '2.0', 'id': 1,
        'error': {'message': 'Invalid params',
                  'data': 'Login name or password is incorrect.'}}

    assert sm.zbx_login() is None

    out = capsys.readouterr().out
    assert 'rejected' in out.lower(), (
        'a rejected login must be visible in the service output: %r' % out)


def test_a_successful_login_is_unchanged(caplog):
    """The guard must not break the happy path."""
    cw = _load('cash_watcher', REPO)
    cw.zbx_post = lambda payload: {'jsonrpc': '2.0', 'id': 1, 'result': 'token-1'}

    with caplog.at_level('WARNING'):
        assert cw.zbx_login() == 'token-1'

    assert not [r for r in caplog.records if 'rejected' in r.message.lower()]
