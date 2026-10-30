"""GRG XFS metrics are translated, never guessed.

The live TESTGRG sample is the fixture: four dispense cassettes, a reject bin,
and peripheral items that are present but not queried (255). The translation
must expose the cash values, keep missing peripherals missing, and let every
consumer reach the same state.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from hardware_view import build_device_rows, build_hardware
from zabbix_modules.status_rules import interpret_state
from zabbix_modules.xfs_metrics import DISPENSE_UNITS, is_xfs, normalize


def live_xfs(**overrides):
    m = {
        'atm.xfs.cdm': '{"cdm": {"status": {"device": 0}}}',
        'atm.xfs.cdm.device_status': '0',
        'atm.xfs.cdm.safe_door': '3',
        'atm.xfs.cdm.dispenser': '0',
        'atm.xfs.cdm.total_notes': '3140',
        'atm.xfs.cdm.total_amount': '393800.00',
        'atm.xfs.cdm.reject_count': '4',
        'atm.xfs.cdm.retract_count': '0',
        'atm.xfs.cdm.cassette.count[0050A]': '100',
        'atm.xfs.cdm.cassette.count[0100A]': '100',
        'atm.xfs.cdm.cassette.count[0100B]': '2092',
        'atm.xfs.cdm.cassette.count[0200A]': '848',
        'atm.xfs.cdm.cassette.reject[0050A]': '0',
        'atm.xfs.cdm.cassette.reject[0100A]': '0',
        'atm.xfs.cdm.cassette.reject[0100B]': '1',
        'atm.xfs.cdm.cassette.reject[0200A]': '3',
        'atm.xfs.idc.device_status': '255',
        'atm.xfs.idc.media_status': '255',
        'atm.xfs.idc.retain_bin': '255',
        'atm.xfs.idc.cards': '0',
        'atm.xfs.ptr.device_status': '255',
        'atm.xfs.ptr.paper_status': '255',
        'atm.xfs.pin.device_status': '255',
        'atm.xfs.siu.cabinet_door': '255',
        'atm.xfs.siu.safe_door': '255',
        'atm.xfs.siu.supervisor_mode': '255',
        'atm.xfs.siu.tamper': '255',
    }
    m.update(overrides)
    return m


def test_normalizes_cash_and_state():
    out = normalize(live_xfs())
    assert is_xfs(out)
    assert out['atm.status'] == 1
    assert out['atm.net.link'] == 1
    assert out['atm.cash.cassette1'] == 100
    assert out['atm.cash.cassette2'] == 100
    assert out['atm.cash.cassette3'] == 2092
    assert out['atm.cash.cassette4'] == 848
    assert out['atm.cash.total_notes'] == 3140
    assert out['atm.cash.reject_count'] == 4
    assert interpret_state('GRG', out) == 'IN_SERVICE'


def test_missing_peripherals_stay_missing_not_red():
    out = normalize(live_xfs())
    assert 'atm.card.reader' not in out
    assert 'atm.hw.pinpad' not in out
    assert 'atm.printer.paper' not in out
    chips, source = build_hardware('GRG', out)
    assert source == 'XFS'
    got = {c['name']: c['status'] for c in chips}
    assert got['Cash dispenser'] == 'ok'
    assert got['Safe door'] == 'ok'
    assert got['Reject bin'] == 'ok'
    assert got['Card reader'] == 'unknown'
    assert got['Printer'] == 'unknown'
    assert got['PIN pad'] == 'unknown'
    # normalize() forces atm.net.link=1 for every XFS host; a green chip would
    # be a false claim. Reachability is the agent-status card.
    assert got['Network'] == 'unknown'
    assert got['UPS'] == 'unknown'


def test_zero_total_is_out_of_cash():
    m = live_xfs(**{
        'atm.xfs.cdm.total_notes': '0',
        'atm.xfs.cdm.cassette.count[0050A]': '0',
        'atm.xfs.cdm.cassette.count[0100A]': '0',
        'atm.xfs.cdm.cassette.count[0100B]': '0',
        'atm.xfs.cdm.cassette.count[0200A]': '0',
    })
    assert interpret_state('GRG', m) == 'OUT_OF_CASH'


def test_stopped_dispenser_is_hardware_fault():
    assert interpret_state('GRG', live_xfs(**{'atm.xfs.cdm.dispenser': '2'})) == 'HARDWARE_FAULT'


def test_offline_or_error_cdm_is_out_of_service():
    assert interpret_state('GRG', live_xfs(**{'atm.xfs.cdm.device_status': '1'})) == 'OUT_OF_SERVICE'
    assert interpret_state('GRG', live_xfs(**{'atm.xfs.cdm.device_status': '4'})) == 'OUT_OF_SERVICE'


def test_siu_supervisor_switch_sets_supervisor_state():
    assert interpret_state('GRG', live_xfs(**{'atm.xfs.siu.supervisor_mode': '1'})) == 'IN_SUPERVISOR'


def test_printer_or_pin_error_is_hardware_fault():
    assert interpret_state('GRG', live_xfs(**{'atm.xfs.ptr.device_status': '4'})) == 'HARDWARE_FAULT'
    assert interpret_state('GRG', live_xfs(**{'atm.xfs.pin.device_status': '4'})) == 'HARDWARE_FAULT'


def test_card_jam_and_paper_out_are_visible_when_present():
    m = live_xfs(**{
        'atm.xfs.idc.device_status': '0',
        'atm.xfs.idc.media_status': '3',
        'atm.xfs.ptr.device_status': '0',
        'atm.xfs.ptr.paper_status': '2',
    })
    assert interpret_state('GRG', m) == 'HARDWARE_FAULT'
    chips, source = build_hardware('GRG', normalize(m))
    got = {c['name']: c['status'] for c in chips}
    assert source == 'XFS'
    assert got['Card reader'] == 'error'
    assert got['Printer'] == 'error'


def test_unsupported_door_and_paper_codes_are_not_alarms():
    """NCR returns 1 for a door it does not support and 4 for paper status it
    cannot report. Treating either as a fault would make a healthy ATM red."""
    m = live_xfs(**{
        'atm.xfs.cdm.safe_door': '1',
        'atm.xfs.siu.cabinet_door': '1',
        'atm.xfs.siu.safe_door': '1',
        'atm.xfs.ptr.device_status': '0',
        'atm.xfs.ptr.paper_status': '4',
    })
    out = normalize(m)
    assert 'atm.security.safedoor' not in out
    assert 'atm.security.cabdoor' not in out
    assert 'atm.printer.paper' not in out
    assert interpret_state('GRG', m) == 'IN_SERVICE'


def test_open_safe_door_is_reported_as_open():
    out = normalize(live_xfs(**{'atm.xfs.cdm.safe_door': '2'}))
    assert out['atm.security.safedoor'] == 1
    out = normalize(live_xfs(**{'atm.xfs.cdm.safe_door': '4'}))
    assert out['atm.security.safedoor'] == 0


def test_device_rows_show_counts_and_not_queried_peripherals():
    rows = build_device_rows(normalize(live_xfs()))
    by_name = {r['name']: r for r in rows}
    assert by_name['Cassette 0050A']['word'] == '100 notes'
    assert by_name['Cassette 0100B']['word'] == '2092 notes'
    assert by_name['Card reader']['word'] == 'Not queried'
    assert by_name['PIN pad']['word'] == 'Not queried'
    assert all(r['css'] != 'error' for r in rows if r['word'] == 'Not queried')


def test_cassette_order_is_stable_when_one_unit_is_missing():
    m = live_xfs()
    del m['atm.xfs.cdm.cassette.count[0100A]']
    out = normalize(m)
    assert out['atm.cash.cassette1'] == 100
    assert 'atm.cash.cassette2' not in out
    assert out['atm.cash.cassette3'] == 2092
    assert out['atm.cash.cassette4'] == 848


def test_non_xfs_metrics_are_untouched():
    m = {'atm.status': '1', 'atm.cash.cassette1': '100'}
    assert normalize(m) == m
    assert DISPENSE_UNITS == ('0050A', '0100A', '0100B', '0200A')
