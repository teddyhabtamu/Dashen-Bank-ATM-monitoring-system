"""Tests for report-portal/fleet_report.py — the 1,202-ATM answer.

What matters here is that the report never overstates health and never buries
the thing a human must fix: an ATM that is merely silent is a symptom, while a
failed provisioning or an unknown vendor is the cause, and the cause sorts
first.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import fleet_report as fr  # noqa: E402


def v(atm_id, verdict='reporting', blockers=(), reporting=10, total=90,
      vendor='NCR', ip='10.0.0.1'):
    return {'atm_id': atm_id, 'verdict': verdict, 'ok': verdict == 'reporting',
            'blockers': list(blockers),
            'facts': {'items_reporting': reporting, 'items_total': total,
                      'vendor': vendor, 'ip_address': ip},
            'checks': []}


def test_a_reporting_atm_needs_no_action():
    row = fr.summarise([v('ATM-0001')])[0]
    assert row['needs_action'] is False
    assert row['status'] == 'reporting'
    assert 'Reporting' in row['summary']


def test_silent_atm_is_flagged_with_its_cause():
    row = fr.summarise([v('ATM-0002', 'provisioned_not_reporting',
                          ['No NCR telemetry (atm.parts never collected).'])])[0]
    assert row['needs_action'] is True
    assert row['blocker'].startswith('No NCR telemetry')
    # The next action names the FIX (agent on 10050), not the diagnosis again.
    action = fr.recommend_next_action(row)
    assert 'Zabbix agent' in action and '10050' in action
    assert action != row['blocker']


def test_provisioning_failure_outranks_a_silent_host():
    """The failed write is the cause; 'not reporting' is only the symptom."""
    results = [v('ATM-0003', 'provisioned_not_reporting', ['nothing arriving'])]
    plans = [{'atm_id': 'ATM-0003', 'action': 'failed', 'changes': [],
              'error': 'host.create rejected'}]
    row = fr.summarise(results, plans)[0]
    assert row['status'] == 'failed'
    assert 'host.create rejected' in row['summary']


def test_unknown_vendor_goes_to_review():
    results = [v('ATM-0004', 'provisioned_not_reporting', ['nothing'])]
    plans = [{'atm_id': 'ATM-0004', 'action': 'review', 'changes': [], 'error': ''}]
    row = fr.summarise(results, plans)[0]
    assert row['status'] == 'review'
    assert 'engineer review' in fr.recommend_next_action(row)


def test_worst_first_ordering():
    results = [
        v('ATM-GOOD', 'reporting'),
        v('ATM-SILENT', 'provisioned_not_reporting', ['quiet']),
        v('ATM-NONE', 'not_provisioned'),
    ]
    plans = [{'atm_id': 'ATM-BAD', 'action': 'failed', 'changes': [], 'error': 'boom'},
             {'atm_id': 'ATM-GOOD', 'action': 'skipped', 'changes': [], 'error': ''}]
    results.append(v('ATM-BAD', 'provisioned_not_reporting', ['quiet']))
    order = [r['atm_id'] for r in fr.summarise(results, plans)]
    assert order[0] == 'ATM-BAD', 'a failed write must be the first thing seen'
    assert order[-1] == 'ATM-GOOD', 'healthy ATMs go last'


def test_counts_split_healthy_from_actionable():
    rows = fr.summarise([
        v('ATM-1', 'reporting'),
        v('ATM-2', 'provisioned_not_reporting', ['quiet']),
        v('ATM-3', 'not_provisioned'),
    ])
    c = fr.counts(rows)
    assert c['total'] == 3 and c['reporting'] == 1 and c['needs_action'] == 2
    assert c['healthy'] == 1


def test_an_atm_missing_from_verification_is_not_invented():
    """Only ATMs that were actually checked may appear in the report."""
    rows = fr.summarise([v('ATM-1', 'reporting')], [{'atm_id': 'ATM-GHOST',
                                                     'action': 'skipped', 'changes': []}])
    assert [r['atm_id'] for r in rows] == ['ATM-1']


def test_plan_changes_are_surfaced_for_an_healthy_host():
    """A host that reports fine but is missing the agent interface still has
    work queued; the report must not hide it behind 'healthy'."""
    rows = fr.summarise([v('ATM-5', 'reporting')],
                        [{'atm_id': 'ATM-5', 'action': 'update',
                          'changes': ['agent interface'], 'error': ''}])
    row = rows[0]
    assert row['changes'] == ['agent interface']
    assert 'agent interface' in fr.recommend_next_action(row)


def test_csv_carries_the_actionable_rows():
    rows = fr.summarise([
        v('ATM-1', 'reporting'),
        v('ATM-2', 'provisioned_not_reporting', ['No GRG telemetry'], vendor='GRG'),
    ])
    csv = fr.to_csv(rows)
    lines = csv.strip().splitlines()
    assert lines[0].startswith('atm_id,status,needs_action')
    assert 'ATM-2' in lines[1] and 'No GRG telemetry' in lines[1]
    assert 'no' in [l for l in lines if l.startswith('ATM-1')][0].split(',')[2]


def test_next_action_per_status():
    # status -> (verify verdict, provision action, expected next-step phrase)
    cases = {
        'not_provisioned': ('not_provisioned', None, 'Provision it'),
        'failed': ('provisioned_not_reporting', 'failed', 'Fix provisioning'),
        'review': ('provisioned_not_reporting', 'review', 'engineer review'),
        'no_ip': ('not_provisioned', 'no_ip', 'IP address'),
        'unknown': ('unknown', None, 'Zabbix connectivity'),
    }
    for status, (verdict, action, expected) in cases.items():
        atm_id = f'ATM-{status}'
        results = [v(atm_id, verdict, ['why'])]
        plans = ([{'atm_id': atm_id, 'action': action, 'changes': [], 'error': 'boom'}]
                 if action else [])
        row = fr.summarise(results, plans)[0]
        assert row['status'] == status, f'{status} collapsed to {row["status"]}'
        assert row['needs_action'] is True
        assert expected in fr.recommend_next_action(row)


# ── the wiring between verify.fleet() and the two workers ───────────────
def test_fleet_joins_verification_with_a_provisioning_plan():
    """Regression: fleet() unpacked provision_atms() as a tuple when it
    returns a dict, so the whole plan was swallowed and planned_changes came
    out empty. This pins both return-shape contracts."""
    import verify

    def fake_verify(atm_id, window_minutes=15):
        return {'atm_id': atm_id, 'verdict': 'reporting', 'ok': True,
                'blockers': [], 'facts': {'items_reporting': 5, 'items_total': 5,
                                          'vendor': 'NCR', 'ip_address': '10.0.0.9'},
                'checks': []}

    def fake_plan(atm_ids, dry_run=True, actor='', max_batch=None):
        assert dry_run is True, 'the fleet report must never write'
        return {'ok': True, 'error': '', 'summary': {},
                'results': [{'atm_id': a, 'action': 'update',
                             'changes': ['agent interface'], 'error': ''}
                            for a in atm_ids]}

    out = verify.fleet(atm_ids=['ATM-9'], verify_fn=fake_verify, plan_fn=fake_plan)
    row = out['rows'][0]
    assert row['changes'] == ['agent interface']
    assert 'agent interface' in row['next_action']
    assert 'agent interface' in out['csv'], 'the plan must reach the CSV too'


def test_fleet_survives_a_planner_that_blows_up():
    import verify

    def fake_verify(atm_id, window_minutes=15):
        return {'atm_id': atm_id, 'verdict': 'provisioned_not_reporting', 'ok': False,
                'blockers': ['quiet'], 'facts': {'items_reporting': 0, 'items_total': 9},
                'checks': []}

    def broken_plan(*a, **k):
        raise RuntimeError('zabbix down')

    out = verify.fleet(atm_ids=['ATM-8'], verify_fn=fake_verify, plan_fn=broken_plan)
    assert out['rows'][0]['status'] == 'provisioned_not_reporting'
    assert out['counts']['needs_action'] == 1


def test_fleet_reports_a_planner_error_instead_of_hiding_it():
    import verify

    def fake_verify(atm_id, window_minutes=15):
        return {'atm_id': atm_id, 'verdict': 'reporting', 'ok': True,
                'blockers': [], 'facts': {'items_reporting': 1, 'items_total': 1},
                'checks': []}

    def plan_with_error(atm_ids, dry_run=True, actor='', max_batch=None):
        return {'ok': False, 'error': 'Zabbix login failed', 'summary': {}, 'results': []}

    out = verify.fleet(atm_ids=['ATM-7'], verify_fn=fake_verify, plan_fn=plan_with_error)
    assert out['rows'][0]['status'] == 'reporting', 'verification still stands'


# ── the next action must never invent work ──────────────────────────────
def test_a_healthy_atm_is_never_given_a_task():
    """Regression seen on UAT: the green ATM-TEST-NCR row said "Investigate on
    the box with the network team". Telling someone to go and investigate a
    healthy ATM is how a column of advice stops being read."""
    row = fr.summarise([v('ATM-OK', 'reporting')])[0]
    assert row['needs_action'] is False
    assert fr.recommend_next_action(row) == 'No action needed.'


def test_a_healthy_atm_with_queued_work_still_names_it():
    rows = fr.summarise([v('ATM-OK', 'reporting')],
                       [{'atm_id': 'ATM-OK', 'action': 'update',
                         'changes': ['agent interface'], 'error': ''}])
    assert 'agent interface' in fr.recommend_next_action(rows[0])


def test_a_silent_atm_is_told_the_fix_not_repeated_the_diagnosis():
    """Repeating "atm.parts never collected" in the next-action column tells
    the operator nothing. It must name the fix and the address."""
    grg = fr.summarise([v('ATM-G', 'provisioned_not_reporting',
                          ['No GRG telemetry (atm.net.link never collected).'],
                          vendor='GRG', ip='172.23.248.6')])[0]
    ncr = fr.summarise([v('ATM-N', 'provisioned_not_reporting',
                          ['No NCR telemetry (atm.parts never collected).'],
                          vendor='NCR', ip='10.30.41.116')])[0]
    for row in (grg, ncr):
        action = fr.recommend_next_action(row)
        assert action != row['blocker'], 'next action must not echo the blocker'
        assert row['ip'] in action, 'the action must name the box'
    assert 'UDP/161' in fr.recommend_next_action(grg)
    assert '10050' in fr.recommend_next_action(ncr)


def test_timestamps_are_labelled_in_the_timezone_they_are_shown_in():
    assert fr.format_eat('2026-10-15T18:38:48+00:00') == '2026-10-15 21:38 EAT'
    assert fr.format_eat(None) == ''
    assert fr.format_eat('nonsense') == 'nonsense'
