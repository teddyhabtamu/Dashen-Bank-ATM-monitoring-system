"""A state that flickers is not a state.

Seen live on UAT 2026-10-17: ATM-TEST-NCR logged

    UNREACHABLE -> HARDWARE_FAULT -> UNREACHABLE -> HARDWARE_FAULT

on every 30-second poll, for 24 hours. Zabbix's interface.available holds
whichever poll last succeeded, so a single unlucky timeout against a flaky link
inverts the verdict.

The consequences are not cosmetic:

  * one atm_state_history segment is written per flip, and the 7-day
    Availability Trend is computed from those segments - so the headline
    availability number stops meaning anything;
  * the dashboard flips under the operator's cursor;
  * once triggers follow state changes, it pages someone every 30 seconds, and
    that is precisely how a team learns to ignore alerts.

So the reading must be confirmed before it is believed. These tests pin the
confirmation arithmetic, the state it must NOT write, and the tunability.
"""
import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def load_state_manager():
    """Import simulators/state_manager.py without dragging in psycopg2."""
    path = REPO / 'simulators' / 'state_manager.py'
    spec = importlib.util.spec_from_file_location('sm_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    # The module imports db/psycopg2 at import time; stub what it needs so the
    # pure logic can be tested without a database.
    for name in ('db', 'psycopg2', 'psycopg2.pool'):
        if name not in sys.modules:
            sys.modules[name] = type('m', (), {})()
    sys.modules['psycopg2'].pool = type('pool', (), {'ThreadedConnectionPool': object})()
    sys.modules['db'].get_db = lambda: None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sm():
    mod = load_state_manager()
    mod._pending.clear()
    yield mod
    mod._pending.clear()


def test_default_requires_two_consecutive_readings(sm):
    assert sm.STATE_CONFIRMATIONS == 2, (
        'one is no debounce; two rejects a single unlucky poll and settles '
        'a real change within the 30s operator budget')


def test_a_single_sighting_is_never_believed(sm):
    ok, seen = sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    assert ok is False
    assert seen == 1


def test_two_sightings_settle(sm):
    sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    ok, seen = sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    assert ok is True
    assert seen == 2


def test_a_settled_candidate_does_not_need_a_third_poll(sm):
    committed = 'IN_SERVICE'
    for _ in range(2):
        settled, _ = sm._debounced('ATM-1', committed, 'UNREACHABLE')
        if settled:
            committed = 'UNREACHABLE'
    assert committed == 'UNREACHABLE'


def test_flapping_is_suppressed_entirely(sm):
    """The live failure mode: the reading alternates faster than the window.

    After many flips the candidate count must never reach the threshold, so the
    committed state never changes. That is the whole point.
    """
    committed = 'IN_SERVICE'
    changes = 0
    readings = ['UNREACHABLE', 'HARDWARE_FAULT'] * 40
    for reading in readings:
        settled, _ = sm._debounced('ATM-1', committed, reading)
        if settled:
            committed = reading
            changes += 1
    assert changes == 0, 'a flapping ATM must not churn its state'
    assert committed == 'IN_SERVICE'


def test_a_stable_new_state_does_settle(sm):
    """Debouncing must not make the system deaf to a real fault."""
    committed = 'IN_SERVICE'
    for _ in range(3):
        settled, _ = sm._debounced('ATM-1', committed, 'UNREACHABLE')
        if settled:
            committed = 'UNREACHABLE'
    assert committed == 'UNREACHABLE', 'a genuine, sustained outage must be reported'


def test_a_reading_matching_the_current_state_clears_the_pending_candidate(sm):
    """One good reading means the previous bad one was noise."""
    sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    ok, _ = sm._debounced('ATM-1', 'IN_SERVICE', 'IN_SERVICE')
    assert ok is True
    assert 'ATM-1' not in sm._pending, 'a settled reading must clear the candidate'


def test_candidates_are_tracked_per_atm(sm):
    """A flapping ATM must not delay a real fault on a different one."""
    sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    ok, seen = sm._debounced('ATM-2', 'IN_SERVICE', 'HARDWARE_FAULT')
    assert ok is False and seen == 1, 'each ATM must count its own way to settled'


def test_switching_candidate_restarts_the_count(sm):
    """UNREACHABLE then HARDWARE_FAULT is not 'two sightings of one thing'."""
    sm._debounced('ATM-1', 'IN_SERVICE', 'UNREACHABLE')
    ok, seen = sm._debounced('ATM-1', 'IN_SERVICE', 'HARDWARE_FAULT')
    assert ok is False
    assert seen == 1, 'a different candidate must restart the count'


def test_threshold_is_tunable_for_a_flaky_environment(sm):
    assert 'STATE_CONFIRMATIONS' in (REPO / 'simulators' / 'state_manager.py').read_text()


def test_held_states_still_advance_last_seen(sm):
    """A held reading must still prove the box is alive.

    Dropping last_seen during a flap is how "we could not read this machine"
    turns into "this machine went away" - the exact conflation this project has
    been removing.
    """
    src = (REPO / 'simulators' / 'state_manager.py').read_text()
    block = src[src.index('if not settled:'):src.index('if current_state != new_state:')]
    assert 'last_seen = NOW()' in block, (
        'a held state must keep last_seen moving')
    assert 'continue' in block, 'and must not write the state'
    # Only last_seen/updated_at may be touched while a reading is held.
    writes = [ln.strip() for ln in block.split('\n')
              if 'UPDATE' in ln or 'SET' in ln]
    for w in writes:
        assert 'last_seen' in w or 'updated_at' in w, w