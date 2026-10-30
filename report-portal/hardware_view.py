"""report-portal/hardware_view.py — hardware chip / parts rendering, pure.

Extracted from blueprints/admin.py for the same reason as onboard_rules.py:
this logic is load-bearing (it decides what the portal claims about a machine's
physical state) and must be unit-tested on every commit, not only exercised by
opening a page. Stdlib-only — no Flask, no DB, no Zabbix API.

The one rule this module exists to keep: **a chip is never red unless a value
was actually collected and said so.** Missing data is grey 'unknown'. The WMI
helpers always did this; the SNMP branch did not, and it produced the portal's
oldest lie — four red "Fault" chips on ATMs nobody had ever measured.
"""

from zabbix_modules.xfs_metrics import DISPENSE_UNITS, is_xfs


def _wmi_chip(m, *part_names):
    """Worst-of chip status across WMI part instances.

    Vendor legend (NCR_DevDevicePart qualifiers): 0 OK, 1 Warning, 2 Error,
    other nonzero Unknown; missing data renders grey 'unknown' — never red.
    No-data-red was the portal's oldest lie (Card Reader showed Fault while
    WMI read OK); this helper makes that impossible by construction.
    """
    worst, rank, seen = 'unknown', -1, False
    for p in part_names:
        v = m.get(f'atm.part.part_status[{p}]')
        if v is None or v == '':
            continue
        seen = True
        try:
            code = int(float(v))
        except (TypeError, ValueError):
            continue
        if code == 2:
            r, s = 3, 'error'
        elif code == 1:
            r, s = 2, 'warn'
        elif code == 0:
            r, s = 0, 'ok'
        else:
            r, s = 1, 'unknown'
        if r > rank:
            worst, rank = s, r
    return worst if seen else 'unknown'


def _wmi_dev(m, *dev_names):
    """Worst-of chip status across WMI devices (DeviceStatus legend:
    0 OK, 1 Warning, 2 Unavailable; anything else Unknown; missing Unknown).
    Same no-data-never-red contract as _wmi_chip."""
    worst, rank, seen = 'unknown', -1, False
    for d in dev_names:
        v = m.get(f'atm.device.status[{d}]')
        if v is None or v == '':
            continue
        seen = True
        try:
            code = int(float(v))
        except (TypeError, ValueError):
            continue
        if code == 2:
            r, s = 3, 'error'
        elif code == 1:
            r, s = 2, 'warn'
        elif code == 0:
            r, s = 0, 'ok'
        else:
            r, s = 1, 'unknown'
        if r > rank:
            worst, rank = s, r
    return worst if seen else 'unknown'


_RANK = {'ok': 0, 'unknown': 1, 'warn': 2, 'error': 3}


def _worst(*statuses):
    """Worst of several chip statuses (lets part- and device-derived
    readings share one chip honestly)."""
    return max(statuses, key=lambda s: _RANK.get(s, 1))


PART_WORDS = {0: ('OK', 'ok'), 1: ('Warning', 'warn'), 2: ('Error', 'error')}
DEV_WORDS = {0: ('OK', 'ok'), 1: ('Warning', 'warn'), 2: ('Unavailable', 'error')}

AGENT_SOURCED = ('XFS', 'WMI')


def snapshot_is_stale(hw_source, agent_status):
    """True when the hardware snapshot rides the Zabbix agent and the agent
    has stopped answering.

    XFS (GRG) and WMI (NCR) readings are delivered through the Zabbix agent,
    so when the agent is unreachable the values on screen are the last ones
    Zabbix received — not live data.  SNMP core items can keep other panels
    fresh at the same time, which is exactly the contradiction this separates.
    """
    return (hw_source in AGENT_SOURCED
            and bool(agent_status)
            and agent_status.get('state') == 'unreachable')


def _wmi_code(m, key):
    try:
        return int(float(m.get(key)))
    except (TypeError, ValueError):
        return None


def build_part_rows(m):
    """Every WMI part as (name, word, css) — the detail behind the 7 chips.

    Unknown codes and missing data render grey 'Unknown', never red.
    Sorted faults-first so a technician sees the broken rows on top.
    """
    rows = []
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..'))
        from zabbix_modules.status_rules import part_display as _display
    except Exception:
        _display = None
    for k in sorted(m):
        if not (k.startswith('atm.part.part_status[') and k.endswith(']')):
            continue
        name = k[len('atm.part.part_status['):-1]
        code = _wmi_code(m, k)
        if _display is not None:
            repl = _wmi_code(m, f'atm.part.replenishment_status[{name}]')
            word, css = _display(code, repl)
        else:
            word, css = PART_WORDS.get(code, ('Unknown', 'unknown')) \
                if code is not None else ('Unknown', 'unknown')
        rows.append({'name': name, 'word': word, 'css': css,
                     'rank': _RANK[css]})
    rows.sort(key=lambda r: (-r['rank'], r['name']))
    return [{'name': r['name'], 'word': r['word'], 'css': r['css']}
            for r in rows]


def _xfs_num(m, key):
    try:
        value = int(float(m.get(key)))
    except (TypeError, ValueError):
        return None
    return None if value in (-1, 255, 65535) else value


XFS_DEVICE_WORDS = {
    0: ('Online', 'ok'), 1: ('Offline', 'error'), 2: ('Power off', 'error'),
    3: ('No device', 'error'), 4: ('Hardware error', 'error'),
    5: ('User error', 'error'), 6: ('Busy', 'warn'), 7: ('Fraud attempt', 'error'),
}
XFS_DOOR_WORDS = {
    1: ('Not supported', 'unknown'), 2: ('Open', 'warn'),
    3: ('Closed', 'ok'), 4: ('Locked', 'ok'), 5: ('Unknown', 'unknown'),
}
XFS_PAPER_WORDS = {
    0: ('OK', 'ok'), 1: ('Low', 'warn'), 2: ('Out', 'error'),
    3: ('Not supported', 'unknown'), 4: ('Unknown', 'unknown'),
}


def _xfs_rows(m):
    rows = []

    def add(name, word, css):
        rows.append({'name': name, 'word': word, 'css': css, 'rank': _RANK[css]})

    def add_code(name, key, words):
        if key not in m:
            return
        code = _xfs_num(m, key)
        if code is None:
            add(name, 'Not queried', 'unknown')
            return
        word, css = words.get(code, ('Unknown', 'unknown'))
        add(name, word, css)

    def add_code_or_wmi(name, key, words, *wmi_parts):
        """Use the XFS reading when it exists; otherwise WMI parts.

        The ATM application can hold IDC/PIN open, so XFS returns -48 and the
        dependent item is 255. The WMI parts are still valid evidence.
        """
        if key in m:
            code = _xfs_num(m, key)
            if code is not None:
                word, css = words.get(code, ('Unknown', 'unknown'))
                add(name, word, css)
                return
        for part in wmi_parts:
            code = _wmi_code(m, f'atm.part.part_status[{part}]')
            if code is not None:
                word, css = PART_WORDS.get(code, ('Unknown', 'unknown'))
                add(name, word, css)
                return
        add(name, 'Not queried', 'unknown')

    add_code('Cash dispenser', 'atm.xfs.cdm.device_status', XFS_DEVICE_WORDS)
    add_code('Safe door', 'atm.xfs.cdm.safe_door', XFS_DOOR_WORDS)
    add_code_or_wmi('Card reader', 'atm.xfs.idc.device_status', XFS_DEVICE_WORDS,
                    'Card Reader', 'Smart Card Reader')
    add_code('Printer', 'atm.xfs.ptr.device_status', XFS_DEVICE_WORDS)
    add_code('Printer paper', 'atm.xfs.ptr.paper_status', XFS_PAPER_WORDS)
    add_code_or_wmi('PIN pad', 'atm.xfs.pin.device_status', XFS_DEVICE_WORDS,
                    'PinPad')
    add_code('Cabinet door', 'atm.xfs.siu.cabinet_door', XFS_DOOR_WORDS)
    add_code('Supervisor mode', 'atm.xfs.siu.supervisor_mode',
             {0: ('Normal run', 'ok'), 1: ('Supervisor', 'warn')})
    add_code('Tamper sensor', 'atm.xfs.siu.tamper',
             {0: ('OK', 'ok'), 1: ('Alarm', 'error')})

    for unit in DISPENSE_UNITS:
        key = f'atm.xfs.cdm.cassette.count[{unit}]'
        if key not in m:
            continue
        count = _xfs_num(m, key)
        if count is None:
            add(f'Cassette {unit}', 'Not queried', 'unknown')
        elif count == 0:
            add(f'Cassette {unit}', 'Empty', 'error')
        else:
            add(f'Cassette {unit}', f'{count} notes', 'ok')

    rows.sort(key=lambda r: (-r['rank'], r['name']))
    return [{'name': r['name'], 'word': r['word'], 'css': r['css']} for r in rows]


def build_device_rows(m):
    """Every live device as (name, word, css), faults-first. See above."""
    if is_xfs(m):
        return _xfs_rows(m)
    rows = []
    for k in sorted(m):
        if not (k.startswith('atm.device.status[') and k.endswith(']')):
            continue
        name = k[len('atm.device.status['):-1]
        code = _wmi_code(m, k)
        word, css = DEV_WORDS.get(code, ('Unknown', 'unknown')) \
            if code is not None else ('Unknown', 'unknown')
        rows.append({'name': name, 'word': word, 'css': css,
                     'rank': _RANK[css]})
    rows.sort(key=lambda r: (-r['rank'], r['name']))
    return [{'name': r['name'], 'word': r['word'], 'css': r['css']}
            for r in rows]


def _xfs_device_chip(m, key):
    code = _xfs_num(m, key)
    if code is None:
        return 'unknown'
    if code == 0:
        return 'ok'
    return 'warn' if code == 6 else 'error'


def _xfs_door_chip(m, key):
    code = _xfs_num(m, key)
    if code is None:
        return 'unknown'
    if code in (3, 4):
        return 'ok'
    return 'warn' if code == 2 else 'unknown'


def _xfs_paper_chip(m):
    code = _xfs_num(m, 'atm.xfs.ptr.paper_status')
    if code is None:
        return 'unknown'
    return {0: 'ok', 1: 'warn', 2: 'error'}.get(code, 'unknown')


def _xfs_alarm_chip(m, key):
    code = _xfs_num(m, key)
    if code is None:
        return 'unknown'
    return 'error' if code == 1 else 'ok'


def _xfs_hardware(m):
    cash = _xfs_device_chip(m, 'atm.xfs.cdm.device_status')
    if _xfs_num(m, 'atm.xfs.cdm.dispenser') == 2:
        cash = _worst(cash, 'error')

    card = _xfs_device_chip(m, 'atm.xfs.idc.device_status')
    if card == 'unknown':
        # The ATM application can hold the IDC/PIN services open; XFS then
        # returns -48 and no status. WMI is still valid evidence for those
        # two devices, so do not throw it away.
        card = _wmi_chip(m, 'Card Reader', 'Smart Card Reader')
    if _xfs_num(m, 'atm.xfs.idc.media_status') == 3:
        card = _worst(card, 'error')
    if _xfs_num(m, 'atm.xfs.idc.retain_bin') == 2:
        card = _worst(card, 'warn')

    printer = _worst(_xfs_device_chip(m, 'atm.xfs.ptr.device_status'),
                     _xfs_paper_chip(m))
    if printer == 'unknown':
        printer = _worst(_wmi_chip(m, 'Print Head', 'Paper'),
                         _wmi_dev(m, 'RPNT-01-UsbThermal'))
    pin = _xfs_device_chip(m, 'atm.xfs.pin.device_status')
    if pin == 'unknown':
        pin = _wmi_chip(m, 'PinPad')

    rejects = _xfs_num(m, 'atm.xfs.cdm.reject_count')
    reject = 'unknown' if rejects is None else ('warn' if rejects >= 250 else 'ok')

    # XFS has no network-link signal. normalize() forces atm.net.link=1
    # whenever the XFS master exists, so a green Network chip would be a
    # false positive. The agent-reachability card is the real signal.
    network = 'unknown'

    comps = [
        ('Cash dispenser', cash),
        ('Safe door', _xfs_door_chip(m, 'atm.xfs.cdm.safe_door')),
        ('Cabinet door', _xfs_door_chip(m, 'atm.xfs.siu.cabinet_door')),
        ('Tamper', _xfs_alarm_chip(m, 'atm.xfs.siu.tamper')),
        ('Card reader', card),
        ('Printer', printer),
        ('PIN pad', pin),
        ('Reject bin', reject),
        ('Network', network),
        ('UPS', 'unknown'),
    ]
    return ([{'name': name, 'status': status} for name, status in comps], 'XFS')


def build_hardware(vendor, m):
    """Hardware component status pills. ok=green, warn=amber, error=red.

    The components shown here MUST mirror the OIDs that drive the overall
    ATM state in `state_manager.determine_state` (cash_jam + card_reader),
    otherwise the detail page disagrees with the HARDWARE_FAULT badge.

    NCR agent hosts: WMI dependents are authoritative (SNMP OIDs are
    unmapped simulator roots on real hardware) — returns (chips, 'WMI').
    GRG XFS hosts: XFS-dependent items are authoritative — returns 'XFS'.
    Otherwise the legacy SNMP mapping — returns (chips, 'SNMP').
    """
    if is_xfs(m):
        return _xfs_hardware(m)
    if m.get('atm.parts') not in (None, ""):
        comps = [
            ('Card reader', _wmi_chip(m, 'Card Reader', 'Smart Card Reader')),
            # Printer: part states plus the RPNT device (Unavailable(2) today
            # while Print Head reads 0 — the chip must agree with ticket #19).
            ('Printer', _worst(_wmi_chip(m, 'Print Head', 'Paper'),
                                _wmi_dev(m, 'RPNT-01-UsbThermal'))),
            ('Pin pad', _wmi_chip(m, 'PinPad')),
            # Cash jam: mechanism parts plus the CASH dispenser device
            # (Warning(1) today reads amber, not red, not green).
            ('Cash jam', _worst(_wmi_chip(m, 'Mechanism', 'Single Note Transport'),
                                _wmi_dev(m, 'CASH-01-UsbMediaDispenser'))),
            ('Retract bin', _wmi_chip(m, 'Retract Area', 'Purge Bin', 'Reject Area')),
            ('Network', 'unknown'),  # no WMI source — grey, never red
            ('UPS', 'unknown'),      # no WMI source — grey, never red
        ]
        return ([{'name': n, 'status': s} for n, s in comps], 'WMI')
    # Same no-data-never-red contract as the WMI branch above. A metric that
    # was never collected is grey 'unknown' — NOT a fault. Reporting "Card
    # reader: Fault" for a host Zabbix has never heard from is a hardware
    # claim we cannot evidence, and at 1,202 ATMs it just trains staff to
    # ignore red. The red UNREACHABLE badge, "not reachable" parts panel and
    # Last Seen already carry the real message.
    def num(v):
        # Zabbix returns every value as a string, or None when an item has
        # never collected. None must stay None: 0 is a real reading, and
        # conflating the two is exactly the lie being fixed here.
        if v is None or v == '':
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    def chip(key, is_ok, bad='error'):
        """ok/bad from a live value; 'unknown' when nothing was collected."""
        v = num(m.get(key))
        if v is None:
            return 'unknown'
        return 'ok' if is_ok(v) else bad

    comps = [
        ('Card reader', chip('atm.card.reader', lambda v: v == 1)),
        ('Printer',     chip('atm.printer.paper', lambda v: v == 1)),
        # No pin-pad item exists in the SNMP OID set — there is nothing to
        # read, so nothing may be claimed about it.
        ('Pin pad',     'unknown'),
        ('Cash jam',    chip('atm.cash.jam', lambda v: v != 1)),
        ('Retract bin', chip('atm.cash.rejectbin', lambda v: v < 80, bad='warn')),
        ('Network',     chip('atm.net.link', lambda v: v == 1)),
        ('UPS',         chip('atm.power.ups', lambda v: v > 0)),
    ]
    return ([{'name': n, 'status': s} for n, s in comps], 'SNMP')

