"""Tests for report-portal/hardware_view.py — what the portal claims about a
machine's physical state.

The rule under test: **a chip is never red unless a value was actually
collected and said so.** These exist because the SNMP branch used to coerce
missing metrics to 0 and then read 0 as "fault", so an ATM Zabbix had never
heard from showed four red Fault chips — a hardware claim with no evidence.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

from hardware_view import build_device_rows, build_hardware  # noqa: E402

# An NCR host linked only to the SNMP template: WMI item absent, every SNMP
# item present but never collected -> fetch_zabbix_maps those to None.
NEVER_COLLECTED = {'atm.card.reader': None, 'atm.printer.paper': None,
                   'atm.cash.jam': None, 'atm.cash.rejectbin': None,
                   'atm.net.link': None, 'atm.power.ups': None}

ALL_OK = {'atm.card.reader': 1, 'atm.printer.paper': 1, 'atm.cash.jam': 0,
          'atm.cash.rejectbin': 10, 'atm.net.link': 1, 'atm.power.ups': 1}

ALL_BAD = {'atm.card.reader': 2, 'atm.printer.paper': 0, 'atm.cash.jam': 1,
           'atm.cash.rejectbin': 95, 'atm.net.link': 2, 'atm.power.ups': 0}


def chips(m, vendor='NCR'):
    out, source = build_hardware(vendor, m)
    return {c['name']: c['status'] for c in out}, source


# ── the regression: silence is not a fault ────────────────────────────────
def test_no_data_at_all_makes_no_hardware_claims():
    got, source = chips({})
    assert source == 'SNMP'
    assert set(got.values()) == {'unknown'}, got
    assert got['Card reader'] == 'unknown'


def test_never_collected_items_are_not_faults():
    """The exact UAT case: host exists, items exist, none ever collected."""
    got, _ = chips(NEVER_COLLECTED)
    assert got == {'Card reader': 'unknown', 'Printer': 'unknown', 'Pin pad': 'unknown',
                   'Cash jam': 'unknown', 'Retract bin': 'unknown',
                   'Network': 'unknown', 'UPS': 'unknown'}


def test_zero_is_a_real_reading_not_a_missing_one():
    got, _ = chips(dict(ALL_OK, **{'atm.net.link': 0}))
    assert got['Network'] == 'error', 'a collected 0 is a genuine link-down'


def test_pin_pad_has_no_snmp_source_so_nothing_is_claimed():
    got, _ = chips(ALL_OK)
    assert got['Pin pad'] == 'unknown'


# ── live data still reports honestly ─────────────────────────────────────
def test_healthy_snmp_host_reads_green():
    got, _ = chips(ALL_OK)
    assert got['Card reader'] == 'ok' and got['Printer'] == 'ok'
    assert got['Cash jam'] == 'ok' and got['Network'] == 'ok' and got['UPS'] == 'ok'


def test_faulty_snmp_host_reads_red():
    got, _ = chips(ALL_BAD)
    assert got['Card reader'] == 'error' and got['Printer'] == 'error'
    assert got['Cash jam'] == 'error' and got['Network'] == 'error'
    assert got['UPS'] == 'error'
    assert got['Retract bin'] == 'warn', 'a full reject bin is a warning, not a fault'


def test_partial_data_only_claims_what_arrived():
    got, _ = chips({'atm.card.reader': 2})
    assert got['Card reader'] == 'error'
    assert got['Printer'] == 'unknown' and got['Network'] == 'unknown'


def test_string_values_from_the_api_are_handled():
    got, _ = chips({'atm.card.reader': '1', 'atm.net.link': '2',
                    'atm.power.ups': '1', 'atm.printer.paper': '1',
                    'atm.cash.jam': '0', 'atm.cash.rejectbin': '5'})
    assert got['Card reader'] == 'ok'
    assert got['Network'] == 'error'


def test_blank_strings_count_as_missing_not_zero():
    got, _ = chips({k: '' for k in ALL_OK})
    assert set(got.values()) == {'unknown'}


def test_garbage_values_are_unknown_not_faults():
    got, _ = chips({'atm.card.reader': 'not-a-number'})
    assert got['Card reader'] == 'unknown'


# ── the WMI branch keeps its own contract ────────────────────────────────
def test_wmi_branch_is_used_when_parts_data_exists():
    m = {'atm.parts': '{}',
         'atm.part.part_status[Card Reader]': 0,
         'atm.part.part_status[Print Head]': 0,
         'atm.part.part_status[PinPad]': 0,
         'atm.part.part_status[Mechanism]': 0,
         'atm.part.part_status[Retract Area]': 0}
    got, source = chips(m)
    assert source == 'WMI'
    assert got['Card reader'] == 'ok' and got['Network'] == 'unknown'


def test_xfs_device_rows_fall_back_to_wmi_for_card_and_pin():
    m = {'atm.xfs.cdm': '{}',
         'atm.xfs.idc.device_status': '255',
         'atm.xfs.pin.device_status': '255',
         'atm.part.part_status[Card Reader]': 0,
         'atm.part.part_status[PinPad]': 0}
    rows = {r['name']: r['word'] for r in build_device_rows(m)}
    assert rows['Card reader'] == 'OK'
    assert rows['PIN pad'] == 'OK'


def test_xfs_missing_card_and_pin_fall_back_to_wmi():
    """NCR can hold IDC/PIN open, so XFS returns -48 and no status. The WMI
    parts are still valid evidence for those two devices."""
    m = {'atm.xfs.cdm': '{}',
         'atm.xfs.idc.device_status': '255',
         'atm.xfs.pin.device_status': '255',
         'atm.part.part_status[Card Reader]': 0,
         'atm.part.part_status[PinPad]': 0}
    got, source = chips(m)
    assert source == 'XFS'
    assert got['Card reader'] == 'ok'
    assert got['PIN pad'] == 'ok'


def test_xfs_fault_still_wins_over_wmi():
    m = {'atm.xfs.cdm': '{}',
         'atm.xfs.idc.device_status': '4',
         'atm.xfs.pin.device_status': '4',
         'atm.part.part_status[Card Reader]': 0,
         'atm.part.part_status[PinPad]': 0}
    got, _ = chips(m)
    assert got['Card reader'] == 'error'
    assert got['PIN pad'] == 'error'


def test_wmi_missing_part_is_grey_never_red():
    m = {'atm.parts': '{}', 'atm.part.part_status[Card Reader]': 2}
    got, source = chips(m)
    assert source == 'WMI'
    assert got['Card reader'] == 'error'   # a measured fault
    assert got['Printer'] == 'unknown'     # nothing measured


def test_grg_uses_the_same_snmp_honesty_rules():
    got, source = chips(NEVER_COLLECTED, vendor='GRG')
    assert source == 'SNMP'
    assert set(got.values()) == {'unknown'}