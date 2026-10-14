"""Shared ATM state interpretation, driven by config/status_map.json.

Both state_manager (authoritative ATM state in atm_current_state) and the
report-portal live badge use this so they never drift. The integer codes are
vendor-specific and frequently wrong on a fresh real-ATM cutover, so they live in
a config file: during UAT, edit status_map.json instead of touching code.

Precedence (most severe first): OFFLINE -> OUT_OF_SERVICE -> SUPERVISOR ->
OUT_OF_CASH -> HARDWARE_FAULT -> IN_SERVICE -> UNKNOWN.

NCR agent (WMI) path: when the atm.parts master is fresh, WMI is
authoritative for that host and the SNMP keys (simulator OIDs, unmapped on
real hardware) are ignored. Vendor legend from NCR_DevDevicePart
qualifiers (TESTMACHINE 2026-10-09): PartStatus 0 OK, 1 Warning, 2 Error,
3 Not Configured, 4 Unknown, 5 Accessed. Only Error feeds state today;
Warning/Unknown/PM are v2 after burn-in.
Device branch (NCR_Device sweep, added 2026-10-12; DeviceStatus 0 OK,
1 Warning, 2 Unavailable): CASH-cluster Unavailable → OUT_OF_SERVICE
(dispenser down means no cash — reasoned policy, no guessing: the prefix
is the cluster by construction); other-device Unavailable → HARDWARE_FAULT.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "status_map.json")

DEFAULTS = {
    "in_service": 1,
    "out_of_service": 2,
    "partial_service": 3,
    "supervisor": 4,
    "offline_net_link": 2,
    "cash_jam_fault": 1,
    "card_reader_fault": 2,
}


def _to_int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_status_map():
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return {"default": dict(DEFAULTS)}


WMI_MASTER_KEY = 'atm.parts'
WMI_STATUS_PREFIX = 'atm.part.part_status['
WMI_DEV_PREFIX = 'atm.device.status['


def part_display(part_code, repl_code):
    """Human word + css for one part from its two codes.

    A review (2026-10-14) argued part_status 4 should read 'Not Present'
    instead of 'Unknown'. Rejected on vendor evidence: NCR_DevDevicePart's
    own qualifiers say 4 = Unknown (3 = Not Configured) — renaming to an
    invented term would break the traceability chain. What the review got
    RIGHT is composition: 4+replenishment pairs carry the real meaning
    (Paper 4+2 is empty, cassettes 4+1 are low), so the word is derived
    from the PAIR, never from part_status alone:
      part Error(2) -> Error | repl Error(2) -> Empty |
      part Warning(1) -> Warning | repl Warning(1) -> Low |
      part 3/4/5 or unreadable -> Unknown | else OK.
    """
    if part_code == 2:
        return ('Error', 'error')
    if repl_code == 2:
        return ('Empty', 'error')
    if part_code == 1:
        return ('Warning', 'warn')
    if repl_code == 1:
        return ('Low', 'warn')
    if part_code in (None, 3, 4, 5):
        return ('Unknown', 'unknown')
    if part_code == 0:
        return ('OK', 'ok')
    return ('Unknown', 'unknown')


def wmi_part_errors(m):
    """Sorted names of parts whose WMI part_status == 2 (Error).

    Values arrive as strings ('2'), None when never collected. Anything
    unparseable is ignored (never fault on unreadable data).
    """
    out = []
    for k, v in (m or {}).items():
        if k.startswith(WMI_STATUS_PREFIX) and k.endswith(']') \
                and _to_int(v, -1) == 2:
            out.append(k[len(WMI_STATUS_PREFIX):-1])
    return sorted(out)


def wmi_device_unavail(m):
    """Sorted names of devices whose WMI device_status == 2 (Unavailable)."""
    out = []
    for k, v in (m or {}).items():
        if k.startswith(WMI_DEV_PREFIX) and k.endswith(']') \
                and _to_int(v, -1) == 2:
            out.append(k[len(WMI_DEV_PREFIX):-1])
    return sorted(out)


def interpret_state(vendor, m):
    """Return ATM state from a metrics dict keyed by Zabbix item keys."""
    if not m:
        return 'UNREACHABLE'
    if m.get(WMI_MASTER_KEY) not in (None, ""):
        # NCR agent path live: WMI authoritative, SNMP ignored for host.
        devs = wmi_device_unavail(m)
        if any(d.startswith('CASH-') for d in devs):
            return 'OUT_OF_SERVICE'
        if devs or wmi_part_errors(m):
            return 'HARDWARE_FAULT'
        return 'IN_SERVICE'
    # If even the core items were never collected, the ATM is unreachable
    # rather than (falsely) healthy. Guards against incomplete OID mapping.
    # Empty strings count as missing: Zabbix returns "" (not None) for items
    # that exist but never collected, and "" must not masquerade as 0 —
    # otherwise a dataless host reads OUT_OF_CASH instead of UNREACHABLE.
    if all(m.get(k) in (None, "") for k in ('atm.status', 'atm.net.link', 'atm.cash.cassette1')):
        return 'UNREACHABLE'

    sm = load_status_map()
    r = sm.get(vendor, sm.get('default', DEFAULTS))
    IN_SERVICE = r.get('in_service', DEFAULTS['in_service'])
    OUT_OF_SERVICE = r.get('out_of_service', DEFAULTS['out_of_service'])
    SUPERVISOR = r.get('supervisor', DEFAULTS['supervisor'])
    OFFLINE_NET = r.get('offline_net_link', DEFAULTS['offline_net_link'])
    JAM = r.get('cash_jam_fault', DEFAULTS['cash_jam_fault'])
    CARD = r.get('card_reader_fault', DEFAULTS['card_reader_fault'])

    status = _to_int(m.get('atm.status'), None)
    net = _to_int(m.get('atm.net.link'), None)
    cash1 = _to_int(m.get('atm.cash.cassette1'), 0)
    cash2 = _to_int(m.get('atm.cash.cassette2'), 0)
    cash_jam = _to_int(m.get('atm.cash.jam'), 0)
    card = _to_int(m.get('atm.card.reader'), 0)

    if net == OFFLINE_NET:
        return 'OFFLINE'
    if status == OUT_OF_SERVICE:
        return 'OUT_OF_SERVICE'
    if status == SUPERVISOR:
        return 'IN_SUPERVISOR'
    if cash1 == 0 and cash2 == 0:
        return 'OUT_OF_CASH'
    if cash_jam == JAM or card == CARD:
        return 'HARDWARE_FAULT'
    if status == IN_SERVICE:
        return 'IN_SERVICE'
    return 'UNKNOWN'
