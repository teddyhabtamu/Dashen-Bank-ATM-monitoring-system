"""The fleet sweep reads identity, EJ and image evidence without losing the
reporting verdict when a side table is missing.

These pin the query-to-facts contract: the fleet report is only as honest as
the evidence it is handed, and a missing table must be recorded as unavailable
rather than as "this ATM has no identity".
"""
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import verify  # noqa: E402


class FakeCursor:
    def __init__(self, identity=None, ej=None, roots=None, fail_identity=False):
        self.identity = identity
        self.ej = ej
        self.roots = roots
        self.fail_identity = fail_identity
        self.rows = []
        self.sqls = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if 'atm_agent_identity' in sql:
            if self.fail_identity:
                raise RuntimeError('relation "atm_agent_identity" does not exist')
            self.rows = self.identity or []
        elif 'ej_parse_runs' in sql:
            self.rows = self.ej or []
        elif 'atm_locations' in sql:
            self.rows = self.roots or []
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


def test_identity_ej_and_roots_are_read_into_facts():
    cur = FakeCursor(
        identity=[('ATM-1', datetime(2026, 10, 31, 9, tzinfo=timezone.utc),
                   'TID006', 'TESTMACHINE',
                   [{'field': 'model', 'severity': 'low'}],
                   ['C:\\images'], {'atm.parts': {'sha256': 'x'}},
                   'A' * 64, '2026-10-31T09:00:00Z')],
        ej=[('ATM-1', date(2026, 10, 31), 2,
             datetime(2026, 10, 31, 9, 5, tzinfo=timezone.utc))],
        roots=[('ATM-1', ['C:\\images'])],
    )
    out = verify._read_onboarding(cur, ['ATM-1'])['ATM-1']
    assert out['identity']['terminal_id'] == 'TID006'
    assert out['identity']['image_roots'] == ['C:\\images']
    assert out['identity']['has_probes'] is True
    assert out['identity']['profile_hash'] == 'a' * 64
    assert out['identity']['applied_at'] == '2026-10-31T09:00:00Z'
    assert out['ej']['journal_date'] == '2026-10-31'
    assert out['ej']['parse_errors'] == 2
    assert out['image_roots'] == ['C:\\images']
    assert not out.get('identity_unavailable')
    assert not out.get('ej_unavailable')
    assert not out.get('image_roots_unavailable')


def test_a_missing_identity_table_does_not_hide_ej_evidence():
    cur = FakeCursor(
        identity=[],
        ej=[('ATM-1', date(2026, 10, 31), 0, None)],
        fail_identity=True,
    )
    out = verify._read_onboarding(cur, ['ATM-1'])['ATM-1']
    assert out['identity_unavailable'], 'the missing table must be recorded'
    assert out['ej']['journal_date'] == '2026-10-31', (
        'the EJ query must still run after the identity savepoint rolls back')
    assert not out.get('ej_unavailable')


def test_no_identity_row_is_none_not_an_error():
    cur = FakeCursor(identity=[], ej=[])
    out = verify._read_onboarding(cur, ['ATM-1'])['ATM-1']
    assert out['identity'] is None
    assert out['ej'] is None
    assert not out.get('identity_unavailable')
    assert not out.get('ej_unavailable')


def test_current_profile_hash_matches_the_profile_the_portal_hands_out():
    """The fleet compares the box's applied hash with this value. It must be
    the same deterministic profile the download route renders, not a lookalike
    built from a different row shape."""
    from agent_profile import build_profile, load_manifest

    row = {
        'atm_id': 'ATM-TEST-NCR', 'vendor': 'NCR', 'model': 'SelfServ 82',
        'district': 'NAD', 'city': 'Addis Ababa', 'terminal_id': 'TID006',
        'ej_hostname': 'TESTNCR', 'ej_log_format': 'ncr',
        'image_roots': ['C:\\ISUITE\\ICAM\\Permit\\icamimages'],
        'atm_name': 'Test NCR',
    }
    expected = build_profile(row, load_manifest())['profile_hash']
    assert verify._current_profile_hash(row) == expected


def test_current_profile_hash_is_empty_for_a_row_that_cannot_have_a_profile():
    assert verify._current_profile_hash({'atm_id': 'ATM-1', 'vendor': 'WINCOR'}) == ''
    assert verify._current_profile_hash({}) == ''
