"""report-portal/zbx_session.py — Zabbix session errors, classified purely.

Extracted from blueprints/admin.py for the same reason as hardware_view.py and
onboard_rules.py: this decides whether the portal re-authenticates or gives up,
and a wrong answer here is invisible by construction. A dead session token made
every worker return None for every ATM, which the detail page rendered as "ATM
not reachable" — a statement about the machine, in a case where the portal only
knew something about itself. The container log stayed empty throughout.

Stdlib-only: no Flask, no database, no network. Tested in tests/test_zbx_session.py.
"""

# Matched against the message text rather than the numeric JSON-RPC code:
# Zabbix reuses codes across unrelated faults (-32602 covers both a bad
# parameter and a dead session), so the code alone cannot distinguish "your
# request is malformed" from "log in again".
AUTH_ERROR_MARKERS = (
    'not authorised',
    'not authorized',
    'session terminated',
    're-login',
    'permission denied',
    'no permissions',
    'login name or password is incorrect',
)


def error_text(err):
    """The most specific message available in a Zabbix JSON-RPC error object.

    Zabbix puts the useful sentence in 'data' and a generic label in 'message',
    so 'data' wins when both are present.
    """
    if not isinstance(err, dict):
        return str(err)
    return str(err.get('data') or err.get('message') or err)


def is_auth_error(text):
    """True when the error means the session is gone, not that the call was bad.

    A true result is what triggers a single re-login and retry. Getting this
    wrong in the other direction - treating a dead session as a permanent
    failure - is what left a worker broken until its container was restarted.
    """
    t = (text or '').lower()
    return any(marker in t for marker in AUTH_ERROR_MARKERS)
