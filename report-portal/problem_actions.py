"""report-portal/problem_actions.py — Zabbix problem actions, pure logic.

Kept out of the blueprint so the payload can be tested without Flask: the
Monitoring Center is read-only elsewhere, and the first write path on that page
is exactly where a wrong bitmask would be hard to notice (Zabbix answers with a
generic error, and the operator only sees "it did not work").
"""

# Zabbix `event.acknowledge` uses a bitmask for `action`. Only the two
# reversible states are exposed for now; adding close (1), message (4),
# severity (8), suppress (32) or unsuppress (64) means adding a name here and
# a button in the template - the payload builder needs no change.
ACTIONS = {
    'acknowledge': 2,
    'unacknowledge': 16,
}

# What each action means to a human, for the flash message and the audit log.
LABELS = {
    'acknowledge': 'Acknowledged',
    'unacknowledge': 'Unacknowledged',
}


def ack_params(eventid, action):
    """Build the event.acknowledge payload.

    Raises ValueError for an unknown action or a non-numeric event id:
    an unknown name must never reach Zabbix as a 0 bitmask, and the event id
    comes from a URL path, so it is validated before it is put in a request.
    """
    bit = ACTIONS.get(action)
    if bit is None:
        raise ValueError('unknown problem action: %r' % (action,))
    eid = str(eventid or '').strip()
    if not eid.isdigit():
        raise ValueError('event id must be numeric')
    return {'eventids': [eid], 'action': bit}
