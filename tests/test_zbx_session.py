"""Tests for report-portal/zbx_session.py — telling a dead session from a bad call.

Written after a live incident with no diagnosis available. The ATM detail page
intermittently showed "Live parts data unavailable (ATM not reachable)" while a
direct `zabbix_get` to the same ATM's agent answered normally, and the container
log was completely empty.

The reason the log was empty: `_zbx_call` returned `res.get('result')`, and a
Zabbix error response has no 'result' key — so every error, including an expired
session token, became a silent `None`. The page then asserted something about
the ATM that the portal had no basis for. Nothing was logged because nothing
failed loudly; the API had answered, and answered with an error.

These tests pin the classification, because getting it wrong in one direction
leaves a worker permanently broken (the token was cached and never
revalidated) and wrong in the other turns a malformed request into a pointless
re-login loop.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import zbx_session as zs  # noqa: E402


def test_the_real_zabbix_dead_session_wording_is_recognised():
    """The exact strings Zabbix 6.x returns once a session token expires.
    Verbatim, because this is the case that produced the empty log."""
    for text in (
        'Session terminated, re-login, please.',
        'Not authorised.',
        'Not authorized.',
        'No permissions to referred object or it does not exist!',
        'Login name or password is incorrect.',
    ):
        assert zs.is_auth_error(text), text


def test_an_ordinary_bad_request_is_not_treated_as_a_dead_session():
    """The other half. Re-logging in cannot fix a malformed request, and
    retrying one would loop."""
    for text in (
        'Invalid parameter "/output": unexpected parameter "foo".',
        'No such host.',
        'Application error.',
        'Invalid params.',
        '',
        None,
    ):
        assert not zs.is_auth_error(text), text


def test_error_text_prefers_the_specific_message():
    """Zabbix splits errors across 'data' (specific) and 'message' (generic).
    The classification runs on the specific one, so the split matters."""
    err = {'code': -32602, 'message': 'Invalid params.',
           'data': 'Session terminated, re-login, please.'}
    assert zs.error_text(err) == 'Session terminated, re-login, please.'
    assert zs.is_auth_error(zs.error_text(err))

    assert zs.error_text({'code': -1, 'message': 'Not authorised.'}) == 'Not authorised.'
    assert zs.error_text('a bare string') == 'a bare string'
    assert zs.error_text(42) == '42'


def test_a_dead_session_is_never_mistaken_for_data():
    """The failure mode this module exists for, stated as one assertion: the
    text of a session error must classify as an error, because the code path
    that consumes it returns None and the page renders that as 'not reachable'.
    If this ever returns False for real Zabbix wording, an ATM parks itself in
    a false alarm until someone restarts a container."""
    err = {'code': -32602, 'data': 'Session terminated, re-login, please.'}
    assert zs.is_auth_error(zs.error_text(err)) is True
