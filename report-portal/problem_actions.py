"""report-portal/problem_actions.py — Zabbix problem actions, pure logic.

Kept out of the blueprint so the payload can be tested without Flask: the
Monitoring Center is read-only elsewhere, and the first write path on that page
is exactly where a wrong bitmask would be hard to notice (Zabbix answers with a
generic error, and the operator only sees "it did not work").
"""

# Zabbix `event.acknowledge` uses a bitmask for `action`. Only the two
# reversible states are exposed for now; adding close (1), severity (8),
# suppress (32) or unsuppress (64) means adding a name here and a button in
# the template - the payload builder needs no change.
ACTIONS = {
    'acknowledge': 2,
    'unacknowledge': 16,
}

# Adding a message is a separate bit. It is only combined with acknowledge:
# the note then appears in Zabbix's own event history beside the ack, which is
# the audit trail an operator actually reads later.
ADD_MESSAGE = 4

LABELS = {
    'acknowledge': 'Acknowledged',
    'unacknowledge': 'Unacknowledged',
}


def ack_params(eventid, action, message=None):
    """Build the event.acknowledge payload.

    Raises ValueError for an unknown action, a non-numeric event id, or a
    message on an action that does not carry one. The event id comes from a
    URL path, so it is validated before it is put in a request; an unknown
    action must never reach Zabbix as a 0 bitmask.
    """
    bit = ACTIONS.get(action)
    if bit is None:
        raise ValueError('unknown problem action: %r' % (action,))

    eid = str(eventid or '').strip()
    if not eid.isdigit():
        raise ValueError('event id must be numeric')

    note = (message or '').strip()
    if note and action != 'acknowledge':
        # Bit 4 without meaning would silently turn into "add a message" on
        # actions where the UI never offers a field.
        raise ValueError('only acknowledge takes a comment')

    params = {'eventids': [eid], 'action': bit}
    if note:
        params['action'] = bit | ADD_MESSAGE
        params['message'] = note[:2048]
    return params
