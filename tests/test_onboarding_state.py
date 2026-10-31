"""Tests for report-portal/onboarding_state.py — the five-stage answer.

An ATM can report telemetry every minute and still not be onboarded: no
identity, no journal, an unapproved image root. These tests pin the rule that
"ready" means every stage is evidenced, and that a broken stage names the one
next action rather than repeating a diagnosis.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import onboarding_state as ob  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 31)


def row(status='reporting', plan_action='skipped', changes=None,
        blocker='', ip='10.41.30.114', vendor='NCR'):
    return {
        'atm_id': 'ATM-TEST-NCR', 'status': status, 'verdict': status,
        'plan_action': plan_action, 'changes': changes or [],
        'blocker': blocker, 'blockers': [blocker] if blocker else [],
        'ip': ip, 'vendor': vendor, 'items_reporting': 90, 'items_total': 90,
    }


def identity(**overrides):
    base = {
        'reported_at': (NOW - timedelta(minutes=5)).isoformat(),
        'terminal_id': 'TID006',
        'ej_hostname': 'TESTMACHINE',
        'conflicts': [],
        'image_roots': ['C:\\ISUITE\\ICAM\\Permit\\icamimages'],
        'has_probes': True,
    }
    base.update(overrides)
    return base


def facts(identity_value='default', ej_value='default', roots=None,
          ej_hostname='TESTMACHINE'):
    out = {
        'vendor': 'NCR',
        'ip_address': '10.41.30.114',
        'ej_hostname': ej_hostname,
        'approved_image_roots': (
            ['C:\\ISUITE\\ICAM\\Permit\\icamimages'] if roots is None else roots),
        'identity': identity() if identity_value == 'default' else identity_value,
        'ej': ({'journal_date': TODAY.isoformat(), 'parse_errors': 0,
                'parsed_at': NOW.isoformat()}
               if ej_value == 'default' else ej_value),
    }
    return out


def evaluate(r, f):
    return ob.evaluate(r, facts=f, now=NOW, today=TODAY)


def test_a_fully_evidenced_atm_is_complete():
    out = evaluate(row(), facts())
    assert out['status'] == 'complete'
    assert out['needs_action'] is False
    assert out['next_action'] == 'No action needed.'
    assert all(s['status'] == 'ok' for s in out['stages'].values())


def test_identity_missing_is_pending_and_names_the_install():
    out = evaluate(row(), facts(identity_value=None, ej_value=None, roots=[]))
    assert out['status'] == 'in_progress'
    assert out['stages']['identity']['status'] == 'pending'
    assert out['needs_action'] is True
    assert 'agent profile' in out['next_action']
    assert 'Refresh from Zabbix' in out['next_action']


def test_a_material_identity_conflict_needs_review():
    out = evaluate(row(), facts(identity_value=identity(conflicts=[
        {'field': 'terminal_id', 'severity': 'high'}])))
    assert out['status'] == 'review'
    assert out['stages']['identity']['status'] == 'review'
    assert 'terminal_id' in out['next_action']


def test_a_low_severity_note_does_not_block_onboarding():
    """Model drift is descriptive; it must not keep a working ATM in the
    action queue forever."""
    out = evaluate(row(), facts(identity_value=identity(conflicts=[
        {'field': 'model', 'severity': 'low'}])))
    assert out['stages']['identity']['status'] == 'ok'
    assert out['status'] == 'complete'


def test_stale_identity_names_the_agent_and_port():
    out = evaluate(row(), facts(identity_value=identity(
        reported_at=(NOW - timedelta(hours=30)).isoformat())))
    assert out['stages']['identity']['status'] == 'stale'
    assert '10050' in out['next_action']


def test_ej_without_a_hostname_is_blocked_once_identity_has_arrived():
    out = evaluate(row(), facts(ej_hostname=''))
    assert out['stages']['ej']['status'] == 'blocked'
    assert 'EJ hostname' in out['next_action']


def test_a_new_atm_with_no_identity_is_not_called_blocked_for_ej():
    """Identity can seed an empty EJ hostname. Calling that a blocked registry
    edit on day one sends an operator to fix something that is still coming."""
    out = evaluate(row(), facts(identity_value=None, ej_hostname='',
                                ej_value=None, roots=[]))
    assert out['stages']['ej']['status'] == 'pending'
    assert out['status'] == 'in_progress'


def test_ej_with_no_ingested_journal_is_pending():
    out = evaluate(row(), facts(ej_value=None))
    assert out['stages']['ej']['status'] == 'pending'
    assert 'ej_pull_targets' in out['next_action']


def test_ej_parse_error_is_review():
    out = evaluate(row(), facts(ej_value={
        'journal_date': TODAY.isoformat(), 'parse_errors': 2, 'parsed_at': ''}))
    assert out['stages']['ej']['status'] == 'review'
    assert 'EJ Journal' in out['next_action']


def test_ej_older_than_yesterday_is_stale():
    old = (TODAY - timedelta(days=3)).isoformat()
    out = evaluate(row(), facts(ej_value={
        'journal_date': old, 'parse_errors': 0, 'parsed_at': ''}))
    assert out['stages']['ej']['status'] == 'stale'
    assert old in out['next_action']


def test_an_unapproved_image_root_is_review_and_requires_reapply():
    out = evaluate(row(), facts(
        identity_value=identity(image_roots=['D:\\new-images']),
        roots=['C:\\approved']))
    assert out['stages']['images']['status'] == 'review'
    assert 'Approve' in out['next_action']
    assert 'reapply' in out['next_action']


def test_approved_image_roots_are_ready():
    out = evaluate(row(), facts())
    assert out['stages']['images']['status'] == 'ok'


def test_no_reported_image_root_is_pending_not_approved():
    out = evaluate(row(), facts(identity_value=identity(image_roots=[])))
    assert out['stages']['images']['status'] == 'pending'
    assert 'no image root' in out['next_action'].lower()


def test_provisioning_failure_outranks_a_healthy_looking_identity():
    out = evaluate(row(status='failed', plan_action='failed',
                       blocker='host.create rejected'), facts())
    assert out['status'] == 'blocked'
    assert out['stages']['provisioning']['status'] == 'blocked'
    assert 'host.create rejected' in out['next_action']


def test_a_planned_create_is_pending_not_ready():
    out = evaluate(row(status='not_provisioned', plan_action='create',
                       changes=['host']), facts())
    assert out['stages']['provisioning']['status'] == 'pending'
    assert 'Provision it' in out['next_action']


def test_missing_evidence_is_not_evaluated_and_invents_no_work():
    """A caller that did not supply identity/EJ evidence is not the same as an
    ATM with no identity: the report must not send someone to the box."""
    out = evaluate(row(), {})
    assert out['needs_action'] is False
    assert out['next_action'] == 'No action needed.'
    assert out['stages']['identity']['status'] == 'unknown'
    assert out['stages']['ej']['status'] == 'unknown'


def test_the_next_action_is_the_first_broken_stage_in_fix_order():
    out = evaluate(row(), facts(identity_value=None, ej_value=None, roots=[]))
    assert out['stages']['identity']['status'] == 'pending'
    assert out['stages']['ej']['status'] == 'pending'
    assert out['stages']['images']['status'] == 'pending'
    assert 'agent profile' in out['next_action'], (
        'identity must be fixed before EJ and images')
