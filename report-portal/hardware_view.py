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


def build_device_rows(m):
    """Every WMI device as (name, word, css), faults-first. See above."""
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


def build_hardware(vendor, m):
    """Hardware component status pills. ok=green, warn=amber, error=red.

    The components shown here MUST mirror the OIDs that drive the overall
    ATM state in `state_manager.determine_state` (cash_jam + card_reader),
    otherwise the detail page disagrees with the HARDWARE_FAULT badge.

    NCR agent hosts: WMI dependents are authoritative (SNMP OIDs are
    unmapped simulator roots on real hardware) — returns (chips, 'WMI').
    Otherwise the legacy SNMP mapping — returns (chips, 'SNMP').
    """
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

