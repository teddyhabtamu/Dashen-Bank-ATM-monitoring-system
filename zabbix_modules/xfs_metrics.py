"""Translate GRG CEN/XFS item keys into the portal's canonical ATM keys.

The XFS probe emits device-level keys (`atm.xfs.cdm.*`, `atm.xfs.idc.*`, ...)
while the portal, state manager and cash forecast use the older SNMP/WMI
contract (`atm.status`, `atm.cash.cassette1`, ...). Keeping the translation in
one pure module means every consumer sees the same values and an unknown
reading can never be mistaken for zero.

Stdlib only. No Zabbix, database or Flask imports.
"""

import json

XFS_MASTER_KEY = 'atm.xfs.cdm'
XFS_PREFIX = 'atm.xfs.'

DISPENSE_UNITS = ('0050A', '0100A', '0100B', '0200A')

XFS_STATE_KEYS = (
    XFS_MASTER_KEY,
    'atm.xfs.cdm.device_status',
    'atm.xfs.cdm.safe_door',
    'atm.xfs.cdm.dispenser',
    'atm.xfs.cdm.total_notes',
    'atm.xfs.cdm.reject_count',
    'atm.xfs.cdm.retract_count',
    *(f'atm.xfs.cdm.cassette.count[{unit}]' for unit in DISPENSE_UNITS),
    *(f'atm.xfs.cdm.cassette.reject[{unit}]' for unit in DISPENSE_UNITS),
    'atm.xfs.idc.device_status',
    'atm.xfs.idc.media_status',
    'atm.xfs.idc.retain_bin',
    'atm.xfs.ptr.device_status',
    'atm.xfs.ptr.paper_status',
    'atm.xfs.pin.device_status',
    'atm.xfs.siu.cabinet_door',
    'atm.xfs.siu.safe_door',
    'atm.xfs.siu.supervisor_mode',
    'atm.xfs.siu.tamper',
)

STATUS_SENTINELS = frozenset((-1, 255, 65535))


def is_xfs(metrics):
    """True when the metrics dict came from the GRG XFS collector."""
    return any(str(k).startswith(XFS_PREFIX) for k in (metrics or {}))


def xfs_service(metrics, class_key):
    """Logical XFS service name for a class (``ptr``, ``idc``, ...).

    The master ``atm.xfs.cdm`` item carries the probe's full JSON, including
    which logical service answered.  That matters for the printer: the GRG
    probe opens ``CheckPrinter``, which is not the receipt printer that serves
    customers, so its hardware error must not put the whole ATM out of service.
    """
    raw = (metrics or {}).get(XFS_MASTER_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return ''
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ''
    node = payload.get(class_key) if isinstance(payload, dict) else None
    if not isinstance(node, dict):
        return ''
    return str(node.get('service') or '').strip()


def is_receipt_printer(service):
    """True when a PTR service is the customer receipt printer.

    An empty/unknown service keeps the historical mapping so old snapshots and
    tests without the master JSON do not silently lose the printer reading.
    """
    name = str(service or '').strip().lower()
    if not name:
        return True
    return 'receipt' in name or name in ('ptr', 'rptr')


def _int(value, missing=()):
    if value is None or value == '':
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return None if number in missing else number


def _device_online(code):
    return code in (0, 6)


def _cassette_count(m, unit):
    return _int(m.get(f'atm.xfs.cdm.cassette.count[{unit}]'))


def _cassette_reject(m, unit):
    return _int(m.get(f'atm.xfs.cdm.cassette.reject[{unit}]'))


def normalize(metrics):
    """Return a copy of `metrics` with XFS values aliased to canonical keys.

    Existing keys are preserved, so callers that understand XFS can still read
    the original values. Non-XFS dicts are returned unchanged.
    """
    out = dict(metrics or {})
    if not is_xfs(out):
        return out

    master_present = any(v not in (None, '') for k, v in out.items()
                         if str(k).startswith(XFS_PREFIX))
    if master_present:
        out['atm.net.link'] = 1

    device = _int(out.get('atm.xfs.cdm.device_status'), STATUS_SENTINELS)
    if device is not None:
        out['atm.status'] = 1 if _device_online(device) else 2
        if device == 7:
            out['atm.security.alarm'] = 1

    dispenser = _int(out.get('atm.xfs.cdm.dispenser'), STATUS_SENTINELS)
    if dispenser is not None:
        out['atm.cash.jam'] = 1 if dispenser == 2 else 0

    counts = []
    for index, unit in enumerate(DISPENSE_UNITS, start=1):
        count = _cassette_count(out, unit)
        if count is None:
            continue
        out[f'atm.cash.cassette{index}'] = count
        counts.append(count)

    total_notes = _int(out.get('atm.xfs.cdm.total_notes'))
    if total_notes is not None:
        out['atm.cash.total_notes'] = total_notes
    elif counts:
        out['atm.cash.total_notes'] = sum(counts)

    reject_count = _int(out.get('atm.xfs.cdm.reject_count'))
    if reject_count is None:
        rejects = [_cassette_reject(out, unit) for unit in DISPENSE_UNITS]
        rejects = [r for r in rejects if r is not None]
        if rejects:
            reject_count = sum(rejects)
    if reject_count is not None:
        out['atm.cash.reject_count'] = reject_count

    retract_count = _int(out.get('atm.xfs.cdm.retract_count'))
    if retract_count is not None:
        out['atm.cash.retract_count'] = retract_count

    idc_device = _int(out.get('atm.xfs.idc.device_status'), STATUS_SENTINELS)
    idc_media = _int(out.get('atm.xfs.idc.media_status'), STATUS_SENTINELS)
    if idc_device is not None:
        out['atm.card.reader'] = 1 if _device_online(idc_device) else 2
    if idc_media == 3:
        out['atm.card.reader'] = 2

    ptr_device = _int(out.get('atm.xfs.ptr.device_status'), STATUS_SENTINELS)
    ptr_paper = _int(out.get('atm.xfs.ptr.paper_status'), STATUS_SENTINELS)
    ptr_service = xfs_service(out, 'ptr')
    if ptr_device is not None:
        if is_receipt_printer(ptr_service):
            out['atm.printer.receipt'] = 1 if _device_online(ptr_device) else 2
        else:
            # A check/journal printer is an auxiliary device. Its fault is
            # shown in the hardware panel, but it is not an ATM outage.
            out['atm.printer.check'] = 1 if _device_online(ptr_device) else 2
    if ptr_paper == 2:
        out['atm.printer.paper'] = 2
    elif ptr_paper in (0, 1):
        out['atm.printer.paper'] = 1

    pin_device = _int(out.get('atm.xfs.pin.device_status'), STATUS_SENTINELS)
    if pin_device is not None:
        out['atm.hw.pinpad'] = 1 if _device_online(pin_device) else 2

    # Door codes 1 (not supported) and 5 (unknown) must stay unmapped. The
    # old "anything not closed is open" rule turned NCR's unsupported door
    # into a security alarm.
    safe_door = _int(out.get('atm.xfs.cdm.safe_door'), STATUS_SENTINELS)
    if safe_door == 2:
        out['atm.security.safedoor'] = 1
    elif safe_door in (3, 4):
        out['atm.security.safedoor'] = 0

    cabinet_door = _int(out.get('atm.xfs.siu.cabinet_door'), STATUS_SENTINELS)
    if cabinet_door == 2:
        out['atm.security.cabdoor'] = 1
    elif cabinet_door in (3, 4):
        out['atm.security.cabdoor'] = 0

    siu_safe_door = _int(out.get('atm.xfs.siu.safe_door'), STATUS_SENTINELS)
    if siu_safe_door == 2:
        out['atm.security.safedoor'] = 1
    elif siu_safe_door in (3, 4):
        out['atm.security.safedoor'] = 0

    supervisor = _int(out.get('atm.xfs.siu.supervisor_mode'), STATUS_SENTINELS)
    if supervisor == 1:
        out['atm.op.supervisor'] = 1

    tamper = _int(out.get('atm.xfs.siu.tamper'), STATUS_SENTINELS)
    if tamper is not None:
        out['atm.security.alarm'] = 1 if tamper == 1 else 0

    return out
