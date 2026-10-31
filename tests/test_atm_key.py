"""Allocation rules for the internal ATM key derived from a terminal ID.

The ATM team uploads terminal IDs; the portal owns atm_id (also the Zabbix
technical host name). These tests pin the rules every entry point shares:
readable derivation, reuse of a known terminal ID, collision suffixing, and
the 20-character column limit.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'report-portal'))

from atm_key import (MAX_ATM_ID, choose_atm_id,  # noqa: E402
                     derived_atm_id, terminal_key)


def test_terminal_key_is_case_and_space_insensitive():
    assert terminal_key('  patml001 ') == 'PATML001'


def test_short_terminal_id_keeps_the_readable_form():
    assert derived_atm_id('PATML001') == 'ATM-PATML001'


def test_unsafe_characters_are_normalised():
    assert derived_atm_id(' TID 001/A ') == 'ATM-TID-001-A'


def test_long_terminal_id_stays_within_the_column_and_is_distinct():
    a = derived_atm_id('TERMINAL-ID-THAT-IS-FAR-TOO-LONG-001')
    b = derived_atm_id('TERMINAL-ID-THAT-IS-FAR-TOO-LONG-002')
    assert len(a) <= MAX_ATM_ID and len(b) <= MAX_ATM_ID
    assert a != b


def test_a_known_terminal_id_reuses_its_existing_key():
    existing = {'PATML001': 'ATM-LEGACY-7'}
    assert choose_atm_id('patml001', {'ATM-LEGACY-7', 'ATM-OTHER'},
                         existing) == 'ATM-LEGACY-7'


def test_collision_gets_a_suffix_within_the_limit():
    chosen = choose_atm_id('PATML001', {'ATM-PATML001'})
    assert chosen == 'ATM-PATML001-2'
    assert len(chosen) <= MAX_ATM_ID


def test_collision_suffix_does_not_collapse_long_ids():
    base = derived_atm_id('TERMINAL-ID-THAT-IS-FAR-TOO-LONG-001')
    chosen = choose_atm_id('TERMINAL-ID-THAT-IS-FAR-TOO-LONG-001', {base})
    assert chosen != base
    assert len(chosen) <= MAX_ATM_ID


def test_no_terminal_id_means_no_key():
    assert choose_atm_id('', set()) is None
    assert derived_atm_id('   ') is None
