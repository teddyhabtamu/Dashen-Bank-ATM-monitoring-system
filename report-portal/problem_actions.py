"""report-portal/problem_actions.py — Zabbix problem actions, pure logic.

Kept out of the blueprint so the payload can be tested without Flask: the
Monitoring Center is read-only elsewhere, and the first write path on that page
is exactly where a wrong bitmask would be hard to notice (Zabbix answers with a
generic error, and the operator only sees "it did not work").
"""

# Zabbix `event.acknowledge` uses a bitmask for `action`. Close (1) and
# severity (8) are deliberately absent: close is refused on triggers without
# manual_close (none of ours allow it) and changing a single event's severity
# corrupts the report history.
ACKNOWLEDGE = 2
UNACKNOWLEDGE = 16
SUPPRESS = 32
UNSUPPRESS = 64
ADD_MESSAGE = 4

ACTIONS = {
    'acknowledge': ACKNOWLEDGE,
    'unacknowledge': UNACKNOWLEDGE,
    'suppress': SUPPRESS,
    'unsuppress': UNSUPPRESS,
}

# Bit 4 without a field would silently mean "add a message" on an action whose
# UI never offers one.
NOTE_ACTIONS = {'acknowledge', 'suppress', 'unsuppress'}

LABELS = {
    'acknowledge': 'Acknowledged',
    'unacknowledge': 'Unacknowledged',
    'suppress': 'Suppressed',
    'unsuppress': 'Unsuppressed',
}

# A suppression is a promise ("ignore this until X"). A free-form number of
# minutes is one typo away from a multi-year blind spot, so the UI offers a
# closed set and the route validates against the same table.
SUPPRESS_MINUTES = {
    '30': 30,
    '120': 120,
    '480': 480,
    '1440': 1440,
    '0': 0,
}
SUPPRESS_LABELS = {
    '30': '30 minutes',
    '120': '2 hours',
    '480': '8 hours',
    '1440': '24 hours',
    '0': 'until manually removed',
}
OPERATOR_MAX_MINUTES = 24 * 60
MAX_BULK = 100


def _event_ids(eventids):
    """Validate one id or a list, dedupe it, and cap the batch.

    eventids accepts a comma/space separated string as well as a list because
    the single-row form posts one id while the bulk form posts many.
    """
    if isinstance(eventids, (list, tuple, set)):
        raw = list(eventids)
    else:
        raw = str(eventids or '').replace(',', ' ').split()
    out = []
    for e in raw:
        s = str(e or '').strip()
        if not s.isdigit():
            raise ValueError('event id must be numeric')
        if s not in out:
            out.append(s)
    if not out:
        raise ValueError('no event ids')
    if len(out) > MAX_BULK:
        raise ValueError('at most %d events per action' % MAX_BULK)
    return out


def suppress_until(duration, now, admin=False):
    """Turn a duration key into a Zabbix `suppress_until` timestamp.

    0 means indefinite and is reserved for administrators: an operator can
    silence a problem for at most 24 hours. The duration is a key from
    SUPPRESS_MINUTES, never a raw number from the form.
    """
    key = str(duration).strip()
    if key not in SUPPRESS_MINUTES:
        raise ValueError('unknown suppression duration')
    minutes = SUPPRESS_MINUTES[key]
    if minutes == 0:
        if not admin:
            raise ValueError('only an administrator may suppress indefinitely')
        return 0
    if not admin and minutes > OPERATOR_MAX_MINUTES:
        raise ValueError('operators may suppress for at most 24 hours')
    return int(now) + minutes * 60


def ack_params(eventids, action, message=None, until=None):
    """Build the event.acknowledge payload.

    Raises ValueError for an unknown action, a bad event id, a message on an
    action that does not carry one, or a suppression without a reason and an
    end time. The event id comes from a URL path, so it is validated before it
    is put in a request; an unknown action must never reach Zabbix as a 0
    bitmask.
    """
    bit = ACTIONS.get(action)
    if bit is None:
        raise ValueError('unknown problem action: %r' % (action,))

    ids = _event_ids(eventids)
    note = (message or '').strip()
    if note and action not in NOTE_ACTIONS:
        raise ValueError('un-acknowledge does not take a comment')

    end = None
    if action == 'suppress':
        if not note:
            raise ValueError('suppression needs a reason')
        if until is None:
            raise ValueError('suppression needs an end time (0 = indefinite)')
        end = int(until)
        if end < 0:
            raise ValueError('suppression end must not be negative')
        # Suppressing is also acknowledging: the operator has seen the
        # problem and taken responsibility for it. Without bit 2 it would
        # stay in the Unacknowledged filter they just cleared.
        bit |= ACKNOWLEDGE
    elif until is not None:
        raise ValueError('only suppress takes an end time')

    params = {'eventids': ids, 'action': bit}
    if note:
        params['action'] |= ADD_MESSAGE
        params['message'] = note[:2048]
    if action == 'suppress':
        params['suppress_until'] = end
    return params


def suppression_state(problem):
    """(suppressed, suppress_until, maintenanceid) for one problem.get row.

    `suppressed` is authoritative and available on older builds that do not
    return suppression_data. The expiry and the maintenance id come from
    `selectSuppressionData`, when the build supports it.
    """
    p = problem or {}
    suppressed = str(p.get('suppressed') or '0') in ('1', 'True', 'true')
    raw = p.get('suppression_data') or []
    if isinstance(raw, dict):
        raw = [raw]
    until, maintenance = 0, ''
    for s in raw:
        if not isinstance(s, dict):
            continue
        try:
            u = int(s.get('suppress_until') or 0)
        except (TypeError, ValueError):
            u = 0
        if u > until:
            until = u
        # Zabbix sends "0" when the suppression is manual; a truthiness
        # check would label every manual suppression as maintenance.
        mid = str(s.get('maintenanceid') or '').strip()
        if mid and mid != '0':
            maintenance = mid
    return suppressed, until, maintenance


def notes_from(acknowledges):
    """Normalise a problem's `acknowledges` array, newest first.

    Only entries carrying a message are useful on the page: a plain
    acknowledge or un-acknowledge is already represented by the Ack badge.
    Kept pure so the ordering and the ignore-empty rules are testable - the
    portal shows the newest note on the row and the full history in its
    tooltip, and either would be wrong if an empty un-acknowledge event were
    allowed to hide a real comment.
    """
    out = []
    for a in acknowledges or []:
        if not isinstance(a, dict):
            continue
        msg = (a.get('message') or '').strip()
        if not msg:
            continue
        try:
            clock = int(a.get('clock') or 0)
        except (TypeError, ValueError):
            clock = 0
        out.append({
            'clock': clock,
            'user': (a.get('username') or '').strip(),
            'userid': str(a.get('userid') or ''),
            'message': msg,
        })
    out.sort(key=lambda n: n['clock'], reverse=True)
    return out
