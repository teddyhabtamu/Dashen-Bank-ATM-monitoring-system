"""report-portal/fleet_report.py — fleet-wide onboarding health, pure logic.

For 1,202 ATMs the wizard is the wrong tool: onboarding becomes "import, look
at one report, fix the rows it names". This module turns the two workers
(provision.plan/provision, verify.verify) into a single answer:

    for every ATM: is it provisioned, is it reporting, and if not, WHY

Kept stdlib-only and separate from Flask so the aggregation - the part that
decides what an operator must act on - is unit-tested rather than eyeballed.
No writes, ever: the caller decides what to do with the rows.
"""

# Ordered worst-first so the top of any report is what needs attention.
SEVERITY = ('failed', 'no_ip', 'provisioned_not_reporting', 'not_provisioned',
            'review', 'unknown', 'reporting')

SEVERITY_LABEL = {
    'reporting': 'Reporting',
    'provisioned_not_reporting': 'Provisioned, not reporting',
    'not_provisioned': 'Not provisioned',
    'review': 'Needs review',
    'unknown': 'Could not be checked',
    'failed': 'Failed',
    'no_ip': 'No IP address',
}


def summarise(verify_results, plan_results=None):
    """Combine verification and a provisioning plan into one row per ATM.

    verify_results: [{atm_id, verdict, ok, blockers, facts, checks}, ...]
    plan_results:   [{atm_id, action, changes, error}, ...] (optional)

    Row shape:
        {atm_id, status, needs_action, summary, blocker, vendor, ip,
         items_reporting, items_total, changes, verdict}

    needs_action is the only field callers should branch on: it means a human
    must do something. Everything else is context for that action.
    """
    plans = {}
    for p in plan_results or []:
        if p and p.get('atm_id'):
            plans[p['atm_id']] = p

    rows = []
    for r in verify_results or []:
        if not r or not r.get('atm_id'):
            continue
        plan = plans.get(r['atm_id'])
        verdict = r.get('verdict') or 'unknown'
        status = verdict
        # A provisioning problem outranks a merely silent host: it is the
        # actionable cause, not the symptom. 'no_ip' only ever comes from the
        # plan, so ignoring plan actions here would hide it.
        if plan and plan.get('action') in ('failed', 'review', 'no_ip'):
            status = plan['action']
        facts = r.get('facts') or {}
        blockers = r.get('blockers') or []
        # The provisioning error names the cause; the verification blocker is
        # the symptom it produced. Prefer the cause when both exist.
        if plan and plan.get('error'):
            blockers = [plan['error']] + [b for b in blockers if b != plan['error']]
        needs_action = status != 'reporting' or bool(blockers)
        row = {
            'atm_id': r['atm_id'],
            'status': status,
            'status_label': SEVERITY_LABEL.get(status, status.replace('_', ' ').title()),
            'needs_action': bool(needs_action),
            'verdict': verdict,
            'blocker': blockers[0] if blockers else '',
            'blockers': blockers,
            'vendor': facts.get('vendor') or '',
            'ip': facts.get('ip_address') or '',
            'items_reporting': facts.get('items_reporting'),
            'items_total': facts.get('items_total'),
            'changes': (plan or {}).get('changes') or [],
            'plan_action': (plan or {}).get('action') or '',
        }
        row['summary'] = _summary(row)
        rows.append(row)

    rows.sort(key=lambda x: (SEVERITY.index(x['status'])
                             if x['status'] in SEVERITY else len(SEVERITY),
                             x['atm_id']))
    return rows


def _summary(row):
    """One line an operator can act on. Never says more than the evidence."""
    if row['status'] == 'reporting':
        items = (f"{row['items_reporting']}/{row['items_total']} items"
                 if row['items_total'] is not None else 'telemetry arriving')
        return f'Reporting ({items}).'
    if row['status'] == 'not_provisioned':
        return 'No Zabbix host yet - run provisioning.'
    if row['status'] == 'failed':
        return f"Provisioning failed: {row['blocker'] or 'see Zabbix error'}."
    if row['status'] == 'review':
        return f"Needs engineer review: {row['blocker'] or 'unknown vendor'}."
    if row['status'] == 'no_ip':
        return 'No IP address recorded, so no host can be created.'
    if row['status'] == 'unknown':
        return 'Zabbix could not be read - reporting is unverified.'
    return row['blocker'] or 'Provisioned but nothing is reporting.'


def counts(rows):
    out = {k: 0 for k in SEVERITY}
    out['total'] = len(rows or [])
    for r in rows or []:
        if r['status'] in out:
            out[r['status']] += 1
    out['reporting'] = out.get('reporting', 0)
    out['needs_action'] = sum(1 for r in rows or [] if r.get('needs_action'))
    out['healthy'] = out['total'] - out['needs_action']
    return out


def to_csv(rows):
    """CSV for the rollout team: what is dark, and why. Stdlib only."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['atm_id', 'status', 'needs_action', 'vendor', 'ip_address',
                'items_reporting', 'items_total', 'provisioning_action',
                'planned_changes', 'blocker'])
    for r in rows or []:
        w.writerow([
            r.get('atm_id', ''), r.get('status', ''),
            'yes' if r.get('needs_action') else 'no',
            r.get('vendor', ''), r.get('ip', ''),
            r.get('items_reporting'), r.get('items_total'),
            r.get('plan_action', ''), '; '.join(r.get('changes') or []),
            r.get('blocker', ''),
        ])
    return buf.getvalue()


def recommend_next_action(row):
    """The single next thing for one ATM. Used by the UI and the CLI."""
    if row['status'] == 'not_provisioned':
        return 'Provision it (Step 3), then verify.'
    if row['status'] == 'failed':
        return f"Fix provisioning: {row['blocker']}"
    if row['status'] == 'review':
        return 'Send to engineer review (unknown vendor/model).'
    if row['status'] == 'no_ip':
        return 'Record the IP address in ATM Registration.'
    if row['status'] == 'unknown':
        return 'Check Zabbix connectivity, then re-run the report.'
    if row['status'] == 'reporting' and row.get('changes'):
        return f"Planned change is ready to apply: {', '.join(row['changes'])}"
    return row['blocker'] or 'Investigate on the box with the network team.'
