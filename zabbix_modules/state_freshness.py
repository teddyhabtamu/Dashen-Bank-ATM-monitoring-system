"""Freshness of the Zabbix values that decide an ATM's state.

The state manager and the report portal must use the same clock.  A stale XFS
snapshot is still a real reading, but it is not evidence of what the ATM is
doing now; using it to override a persisted "agent disconnected" is how the
registry once showed a fault recomputed from 25-minute-old data.

Zabbix reports item delays as strings such as ``30s``, ``1m`` and ``5m``.
Dependent items have delay ``0`` and inherit their master's clock, so they are
not allowed to define the cadence on their own.

Stdlib only: imported by the state-manager container and by the portal.
"""

import re
import time

STATE_SOURCE_KEYS = ('atm.status', 'atm.net.link', 'atm.cash.cassette1')
XFS_PREFIX = 'atm.xfs.'
WMI_MASTER_KEY = 'atm.parts'

# A once-a-minute item gets two missed polls plus slack before it is stale; a
# five-minute WMI probe is not dead after six minutes.  The cap stops a
# once-an-hour item from hiding an agent that stopped answering long ago.
MIN_ALLOWED_SECONDS = 120
MAX_ALLOWED_SECONDS = 900


def item_delay_seconds(value):
    """Zabbix item delay ('30s', '1m', '5m', '1m/1m') -> seconds."""
    match = re.match(r'\s*(\d+)\s*([smhd]?)', str(value or ''))
    if not match:
        return 60
    number = int(match.group(1))
    unit = match.group(2) or 's'
    return max(1, number * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit])


def source_keys(metrics):
    """Keys whose values actually drive the state for this host.

    XFS hosts are judged on their XFS master/dependents, WMI hosts on the
    ``atm.parts`` master, everything else on the SNMP core keys.  Keeping the
    choice here means the portal's live badge and the persisted state cannot
    disagree about which reading is authoritative.
    """
    metrics = metrics or {}
    if any(str(key).startswith(XFS_PREFIX) for key in metrics):
        return [key for key in metrics if str(key).startswith(XFS_PREFIX)]
    if metrics.get(WMI_MASTER_KEY) not in (None, ''):
        return [WMI_MASTER_KEY]
    return [key for key in STATE_SOURCE_KEYS
            if metrics.get(key) not in (None, '')]


def source_freshness(metrics, clocks, delays, now=None):
    """Return ``(age_seconds, allowed_seconds)`` for the state's own source.

    ``(None, None)`` means no relevant item has ever collected a value.  The
    caller must not treat that as proof the ATM is down; absence of telemetry
    is not absence of a machine.
    """
    keys = source_keys(metrics)
    seen = [((clocks or {}).get(key) or 0, (delays or {}).get(key))
            for key in keys]
    seen = [(clock, delay) for clock, delay in seen if clock]
    if not seen:
        return None, None

    newest = max(clock for clock, _delay in seen)
    cadences = [item_delay_seconds(delay) for _clock, delay in seen]
    cadence = max([value for value in cadences if value > 0] or [60])
    allowed = min(MAX_ALLOWED_SECONDS,
                  max(MIN_ALLOWED_SECONDS, int(cadence * 2.5) + 60))
    return max(0, int((now or time.time()) - newest)), allowed


def source_is_current(metrics, clocks, delays, now=None):
    """True only while the state's own source is inside its polling budget."""
    age, allowed = source_freshness(metrics, clocks, delays, now=now)
    return age is not None and age <= allowed
