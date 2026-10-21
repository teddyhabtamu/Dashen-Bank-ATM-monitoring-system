"""maintenance.py — Zabbix maintenance windows for a single ATM (pure logic).

The window arithmetic and the one-time time-period payload live here, away
from Flask and the Zabbix client, because they are the parts that fail
silently: a wrong period or a start_date outside active_since can create a
window that never activates, and a wrong host scope puts the whole branch in
maintenance.
"""

WITH_DATA_COLLECTION = 0  # keep polling; hide alerts only

DURATIONS = {
    '30': 30,
    '60': 60,
    '120': 120,
    '240': 240,
    '480': 480,
    '1440': 1440,
}
DURATION_LABELS = {
    '30': '30 minutes',
    '60': '1 hour',
    '120': '2 hours',
    '240': '4 hours',
    '480': '8 hours',
    '1440': '24 hours',
}
OPERATOR_MAX_MINUTES = 8 * 60
ADMIN_MAX_MINUTES = 24 * 60


def options(admin=False):
    """Duration choices for the form, newest-safe set first."""
    keys = ['30', '60', '120', '240', '480']
    if admin:
        keys.append('1440')
    return [(k, DURATION_LABELS[k]) for k in keys]


def window(duration, now, admin=False):
    """(active_since, active_till, period_seconds) for a one-off window.

    All three are minute-aligned: Zabbix rounds start_date down to the minute,
    and a period that starts before active_since is the classic way to create
    a maintenance that sits there looking active but suppresses nothing.
    """
    key = str(duration).strip()
    if key not in DURATIONS:
        raise ValueError('Unknown maintenance duration.')
    minutes = DURATIONS[key]
    limit = ADMIN_MAX_MINUTES if admin else OPERATOR_MAX_MINUTES
    if minutes > limit:
        raise ValueError('Operators may schedule at most 8 hours of service mode.')
    start = int(now) // 60 * 60
    period = minutes * 60
    return start, start + period, period


def create_params(hostid, atm_id, reason, duration, now, admin=False, actor=''):
    """maintenance.create payload for exactly one host."""
    reason = ' '.join((reason or '').split())
    if len(reason) < 5:
        raise ValueError('A reason of at least 5 characters is required.')
    start, till, period = window(duration, now, admin=admin)
    name = ('%s — %s' % (atm_id, reason))[:128]
    return {
        'name': name,
        'description': 'Service mode started from the ATM Monitoring System by %s.'
                       % (actor or 'unknown'),
        'active_since': start,
        'active_till': till,
        'hosts': [{'hostid': str(hostid)}],
        'timeperiods': [{
            'timeperiod_type': 0,  # one time only
            'start_date': start,
            'period': period,
        }],
        'maintenance_type': WITH_DATA_COLLECTION,
    }


def active_window(maintenances, hostid, now):
    """The maintenance covering hostid at now, or None."""
    now = int(now)
    hid = str(hostid)
    for m in maintenances or []:
        try:
            since = int(m.get('active_since') or 0)
            till = int(m.get('active_till') or 0)
        except (TypeError, ValueError):
            continue
        if not (since <= now < till):
            continue
        hosts = m.get('hosts') or []
        if hosts and not any(str(h.get('hostid')) == hid
                             for h in hosts if isinstance(h, dict)):
            continue
        return m
    return None


def active_by_host(maintenances, now):
    """{hostid: maintenance} for every host covered at now."""
    now = int(now)
    out = {}
    for m in maintenances or []:
        try:
            since = int(m.get('active_since') or 0)
            till = int(m.get('active_till') or 0)
        except (TypeError, ValueError):
            continue
        if not (since <= now < till):
            continue
        for h in m.get('hosts') or []:
            hid = str((h or {}).get('hostid') or '')
            if hid:
                out[hid] = m
    return out
