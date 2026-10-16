"""A state we cannot verify must not be painted like an outage.

The reported symptom: the Overview donut showed all 3 ATMs UNREACHABLE while
`zabbix_get -s 10.41.30.114 -k atm.parts` returned live hardware data from the NCR
box within the hour.

The cause was in simulators/state_manager.py: `fetch_host_metrics` returned None
for three different situations - the Zabbix API failing, the host being absent
from Zabbix, and (via `interpret_state`) a host with no collected telemetry - and
the caller wrote UNREACHABLE for all of them. A monitoring system that reports
its own failure as a network outage trains people to ignore it.

These tests pin the separation, and pin the visual consequence: UNKNOWN and
NOT_PROVISIONED are setup gaps, not incidents, and must not be red.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
STATE_MANAGER = REPO / 'simulators' / 'state_manager.py'


def _module():
    return ast.parse(STATE_MANAGER.read_text())


def _function(name):
    for node in ast.walk(_module()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('%s not found in state_manager.py' % name)


def _src(name):
    return ast.get_source_segment(STATE_MANAGER.read_text(), _function(name)) or ''


def test_api_failure_has_its_own_sentinel():
    """A None return cannot express 'Zabbix did not answer' separately from
    'there is nothing to report'."""
    assert 'FETCH_FAILED' in STATE_MANAGER.read_text()


def test_api_failure_does_not_overwrite_a_healthy_machine():
    """The important behaviour: on a Zabbix hiccup, leave the state alone.

    Writing UNREACHABLE would both invent an outage and destroy the last known
    good state, so the row is skipped instead.
    """
    src = _src('update_states')
    assert 'if m is FETCH_FAILED:' in src
    skip_branch = src.split('if m is FETCH_FAILED:')[1].split('if m is None:')[0]
    assert 'continue' in skip_branch, 'a failed query must skip the ATM, not write it'
    assert 'UNREACHABLE' not in skip_branch


def test_absent_from_zabbix_is_a_provisioning_gap_not_an_outage():
    src = _src('update_states')
    assert "state = 'NOT_PROVISIONED'" in src, (
        'registered here but absent in Zabbix is a setup gap')


def test_reachability_comes_from_positive_evidence():
    """Only Zabbix's own agent-interface verdict of 'unavailable' proves a machine
    is down. Absence of telemetry is not proof of absence of a machine."""
    src = _src('determine_state')
    assert 'availability == 2' in src and "return 'UNREACHABLE'" in src
    assert 'availability == 1' in src and "return 'UNKNOWN'" in src, (
        'agent reachable but nothing collected yet is UNKNOWN, not UNREACHABLE')


def test_interface_availability_is_actually_requested():
    src = _src('fetch_host_metrics')
    assert 'selectInterfaces' in src and 'available' in src


def test_unknown_and_not_provisioned_are_not_rendered_red():
    """The visual half. These two states previously fell into an
    `else -> red` branch, which is how 'we cannot tell' started looking like
    'the machine is broken'."""
    # Read the map via AST rather than importing helpers: it imports flask, and
    # a test that cannot run locally is a test that stops being run.
    tones = _state_tones()
    assert tones['IN_SERVICE'] == 'good'
    for outage in ('UNREACHABLE', 'OFFLINE', 'OUT_OF_SERVICE', 'HARDWARE_FAULT'):
        assert tones[outage] == 'bad', f'{outage} is a real outage and must be red'
    for unknown in ('UNKNOWN', 'NOT_PROVISIONED', 'INACTIVE'):
        assert tones[unknown] == 'muted', (
            f'{unknown} means "not established", not "broken"')
    assert all(v in ('good', 'warn', 'bad', 'muted') for v in tones.values())


def _state_tones():
    tree = ast.parse((REPO / 'report-portal' / 'helpers.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'STATE_TONES':
            return ast.literal_eval(node.value)
    raise AssertionError('STATE_TONES not found in helpers.py')


def test_templates_do_not_hardcode_a_binary_good_or_red():
    """Both ATM templates used `green if IN_SERVICE else red`. Grep-able so the
    pattern cannot creep back in."""
    for name in ('admin_atm.html', 'admin_atm_detail.html'):
        html = (REPO / 'report-portal' / 'templates' / name).read_text()
        assert "'green' if live=='IN_SERVICE' else 'red'" not in html, (
            '%s still paints every non-healthy state red' % name)
        assert 'state_colour' in html or 'state_tone' in html


def test_donut_can_render_the_new_state():
    html = (REPO / 'report-portal' / 'templates' / 'portal.html').read_text()
    assert "'NOT_PROVISIONED'" in html, (
        'an unlabelled slice falls back to grey and loses its meaning')