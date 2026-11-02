"""The state must come from the interface and device that actually carry it.

UAT symptom: `zabbix_get -s 10.41.30.115 -k atm.xfs.cdm` returned fresh GRG
data while the portal said "Agent Disconnected".  The state manager asked
Zabbix for every interface's ``available`` flag and took the first one; hosts
are created with SNMP first and the agent second, so a dead SNMP interface
(real GRG ATMs do not answer the simulator OIDs) condemned a healthy XFS
agent.

The same read also treated interface.available as stronger evidence than fresh
telemetry.  A single transient interface poll could therefore flip a machine
between UNREACHABLE and its real device state even though the XFS snapshot was
current.

These tests pin the transport choice, the data-wins rule, and the optional
GRG check printer that must not put the ATM out of service.
"""
import importlib.util
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent


def load_state_manager():
    path = REPO / 'simulators' / 'state_manager.py'
    spec = importlib.util.spec_from_file_location('sm_source_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    for name in ('db', 'psycopg2', 'psycopg2.pool'):
        if name not in sys.modules:
            sys.modules[name] = type('m', (), {})()
    sys.modules['psycopg2'].pool = type('pool', (), {'ThreadedConnectionPool': object})()
    sys.modules['db'].get_db = lambda: None
    spec.loader.exec_module(mod)
    return mod


GRG_MASTER = json.dumps({
    'vendor': 'GRG',
    'cdm': {
        'status': {'device': 0, 'safe_door': 3, 'dispenser': 0},
        'summary': {'total_notes': 3140},
        'cassettes': {},
    },
    'idc': {'device': 0, 'media': 2},
    # The GRG probe opens CheckPrinter, not the customer receipt printer.
    'ptr': {'service': 'CheckPrinter', 'device': 4, 'paper': 4},
    'pin': {'device': 0},
    'siu': {'safe_door': 3, 'cabinet_door': 3,
            'supervisor_switch': 0, 'tamper_sensor': 0},
})

GRG_METRICS = {
    'atm.xfs.cdm': GRG_MASTER,
    'atm.xfs.cdm.device_status': '0',
    'atm.xfs.cdm.dispenser': '0',
    'atm.xfs.cdm.total_notes': '3140',
    'atm.xfs.idc.device_status': '0',
    'atm.xfs.idc.media_status': '2',
    'atm.xfs.ptr.device_status': '4',
    'atm.xfs.ptr.paper_status': '4',
    'atm.xfs.pin.device_status': '0',
    'atm.xfs.siu.cabinet_door': '3',
    'atm.xfs.siu.safe_door': '3',
    'atm.xfs.siu.supervisor_mode': '0',
    'atm.xfs.siu.tamper': '0',
}


def test_xfs_uses_the_agent_interface_not_whichever_comes_first():
    sm = load_state_manager()
    interfaces = [
        {'type': '2', 'main': '1', 'available': '2'},  # SNMP, dead
        {'type': '1', 'main': '1', 'available': '1'},  # agent, healthy
    ]
    chosen, agent, snmp = sm._relevant_availability(GRG_METRICS, interfaces)
    assert (agent, snmp) == (1, 2)
    assert chosen == 1, 'GRG XFS lives on the agent interface'


def test_snmp_telemetry_uses_the_snmp_interface():
    sm = load_state_manager()
    interfaces = [
        {'type': '2', 'available': '1'},
        {'type': '1', 'available': '2'},
    ]
    metrics = {'atm.status': '1', 'atm.net.link': '1',
               'atm.cash.cassette1': '100'}
    chosen, _agent, _snmp = sm._relevant_availability(metrics, interfaces)
    assert chosen == 1, 'legacy SNMP state must be judged on SNMP'


def test_fresh_data_beats_a_transient_unavailable_flag():
    sm = load_state_manager()
    state = sm.determine_state(GRG_METRICS, 'GRG', availability=2,
                               source_current=True)
    assert state == 'IN_SERVICE', (
        'a current XFS reading proves the agent answered; a lagging interface '
        'flag must not turn it into Agent Disconnected')


def test_stale_data_plus_unavailable_is_unreachable():
    sm = load_state_manager()
    assert sm.determine_state(GRG_METRICS, 'GRG', availability=2,
                              source_current=False) == 'UNREACHABLE'


def test_no_data_without_a_transport_verdict_is_unknown_not_down():
    sm = load_state_manager()
    assert sm.determine_state({}, 'GRG', availability=None,
                              source_current=False) == 'UNKNOWN'
    assert sm.determine_state({}, 'GRG', availability=0,
                              source_current=False) == 'UNKNOWN'
    assert sm.determine_state({}, 'GRG', availability=2,
                              source_current=False) == 'UNREACHABLE'


def test_source_freshness_uses_the_state_source_not_wall_clock():
    from zabbix_modules.state_freshness import source_freshness
    now = int(time.time())
    clocks = {key: now - 30 for key in GRG_METRICS}
    delays = {key: '1m' for key in GRG_METRICS}
    age, allowed = source_freshness(GRG_METRICS, clocks, delays, now=now)
    assert age == 30
    assert allowed >= 120
    assert source_freshness({}, {}, {}, now=now) == (None, None)


def test_grg_check_printer_fault_is_not_an_atm_fault():
    from zabbix_modules.xfs_metrics import normalize
    from zabbix_modules.status_rules import interpret_state

    metrics = normalize(GRG_METRICS)
    assert 'atm.printer.receipt' not in metrics, (
        'CheckPrinter is an auxiliary device, not the customer receipt printer')
    assert metrics.get('atm.printer.check') == 2
    assert interpret_state('GRG', metrics) == 'IN_SERVICE', (
        'the ATM can still serve cash with a failed check printer')


def test_a_real_receipt_printer_fault_still_faults_the_atm():
    from zabbix_modules.xfs_metrics import normalize
    from zabbix_modules.status_rules import interpret_state

    master = json.loads(GRG_MASTER)
    master['ptr'] = {'service': 'ReceiptPrinter1', 'device': 4, 'paper': 4}
    metrics = dict(GRG_METRICS)
    metrics['atm.xfs.cdm'] = json.dumps(master)
    normalized = normalize(metrics)
    assert normalized.get('atm.printer.receipt') == 2
    assert interpret_state('GRG', normalized) == 'HARDWARE_FAULT'
