"""report-portal/onboarding_state.py — one ATM's onboarding stages, pure logic.

An ATM is onboarded when five independent things are true:

    provisioning  a Zabbix host exists and needs no planned repair
    reporting     the vendor's authoritative telemetry is fresh
    identity      atm.identity arrived, is fresh, and has no material conflict
    ej            an EJ hostname is set and a recent journal has been parsed
    images        every image root the box reported is approved

Each stage is one of ok / pending / stale / review / blocked / unknown. The
overall status and the single next action come from the FIRST stage that is
not ok, in that order. "Healthy" must mean complete: a green row that still
needs an image approval teaches operators to ignore the colour.

Pure module: no DB, no Flask, no Zabbix. The fleet sweep passes evidence in;
tests exercise every rule directly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))

STAGES = ('provisioning', 'reporting', 'identity', 'ej', 'images')

STAGE_LABEL = {
    'provisioning': 'Provisioning',
    'reporting': 'Reporting',
    'identity': 'Identity',
    'ej': 'EJ',
    'images': 'Images',
}

STATUS_LABEL = {
    'ok': 'Ready',
    'pending': 'Pending',
    'stale': 'Stale',
    'review': 'Needs review',
    'blocked': 'Blocked',
    'unknown': 'Unknown',
}

OVERALL_LABEL = {
    'complete': 'Onboarded',
    'in_progress': 'In progress',
    'review': 'Needs review',
    'blocked': 'Blocked',
    'unknown': 'Unknown',
}

# 24h is the same window identity_sync uses: the passive item runs hourly, so
# an older value means the transport stopped, not that the box is slow.
IDENTITY_MAX_AGE = timedelta(hours=24)

# Any of these means a human still has work to do. `unknown` deliberately does
# not: it says the report could not be evaluated, not that the ATM is broken.
ACTIONABLE = ('blocked', 'review', 'stale', 'pending')


def _stage(status, detail='', action=''):
    return {
        'status': status,
        'label': STATUS_LABEL.get(status, status.replace('_', ' ').title()),
        'detail': detail or '',
        'action': action or '',
    }


def _parse_ts(value):
    """ISO timestamp -> aware datetime, or None. Naive values are UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _age_label(delta):
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f'{max(1, seconds // 60)} min'
    if seconds < 86400:
        return f'{seconds // 3600} h'
    return f'{seconds // 86400} d'


def _reporting_action(row):
    """The fix for a silent host, never the diagnosis repeated.

    A GRG with fresh SNMP but a dead XFS/agent feed needs the agent checked,
    not the community. Read the blocker rather than guessing from the vendor.
    """
    blockers = ' '.join(row.get('blockers') or [row.get('blocker') or '']).lower()
    vendor = (row.get('vendor') or '').upper()
    ip = row.get('ip') or 'the box'
    agent_side = ('xfs/agent' in blockers or 'zabbix agent' in blockers
                  or '10050' in blockers or 'agent interface' in blockers)
    if agent_side:
        return (f'Check the Zabbix agent on {ip} (TCP/10050) and that the '
                f'vendor collector script is installed, then recompute.')
    if vendor == 'GRG':
        return (f'Open UDP/161 from the collector to {ip}, or confirm the SNMP '
                f'community, then recompute.')
    if vendor == 'NCR':
        return (f'Check the Zabbix agent on {ip} (TCP/10050) and that '
                f'atm-wmi-collector.ps1 is installed, then recompute.')
    return f'Check that {ip} is reachable from the Zabbix server, then recompute.'


def _provisioning_stage(row):
    status = row.get('status')
    plan = row.get('plan_action')
    changes = row.get('changes') or []
    if status == 'failed' or plan == 'failed':
        error = row.get('blocker') or 'see Zabbix error'
        return _stage('blocked', error, f'Fix provisioning: {error}')
    if status == 'review' or plan == 'review':
        return _stage('review', row.get('blocker') or 'Unknown vendor or model',
                      'Send to engineer review (unknown vendor/model).')
    if status == 'no_ip' or plan == 'no_ip':
        return _stage('blocked', 'No IP address recorded',
                      'Record the IP address in ATM Registration.')
    if status == 'not_provisioned' or plan == 'create':
        return _stage('pending', 'No Zabbix host yet',
                      'Provision it (wizard Step 3), then verify.')
    if plan == 'blank':
        return _stage('blocked', 'No ATM ID on the registry row',
                      'Fix the ATM record before provisioning it.')
    if status == 'unknown':
        return _stage('unknown', 'Zabbix could not be read',
                      'Check Zabbix connectivity, then re-run the report.')
    if changes:
        return _stage('pending', ', '.join(changes),
                      f"Apply the planned Zabbix change: {', '.join(changes)}.")
    return _stage('ok', 'Host exists; no planned repair')


def _reporting_stage(row):
    status = row.get('status')
    if status == 'reporting':
        items = row.get('items_reporting')
        total = row.get('items_total')
        detail = (f'{items}/{total} items' if total is not None
                  else 'telemetry arriving')
        return _stage('ok', detail)
    if status == 'not_provisioned':
        return _stage('pending', 'No host to report from',
                      'Provision it (wizard Step 3), then verify.')
    if status == 'unknown':
        return _stage('unknown', 'Zabbix could not be read',
                      'Check Zabbix connectivity, then re-run the report.')
    return _stage('blocked', row.get('blocker') or 'Nothing is reporting',
                  _reporting_action(row))


def _identity_stage(row, facts, now):
    ip = row.get('ip') or 'the box'
    atm_id = row.get('atm_id') or 'this ATM'
    if 'identity' not in facts:
        return _stage('unknown', 'Not evaluated')
    if facts.get('identity_unavailable') or facts.get('onboarding_error'):
        return _stage('unknown', 'Identity evidence unavailable',
                      'Recompute the fleet report; identity could not be read.')
    identity = facts.get('identity')
    if not identity:
        return _stage('pending', 'No identity received',
                      f'Apply the current agent profile on {ip}, then use '
                      f'Refresh from Zabbix on Agent setup.')

    reported = _parse_ts(identity.get('reported_at'))
    if not reported:
        return _stage('pending', 'No identity timestamp',
                      f'Apply the current agent profile on {ip}, then use '
                      f'Refresh from Zabbix on Agent setup.')
    age = now - reported
    if age > IDENTITY_MAX_AGE:
        label = _age_label(age)
        return _stage('stale', f'Last reported {label} ago',
                      f'Check the Zabbix agent on {ip} (TCP/10050); identity '
                      f'last reported {label} ago.')

    conflicts = identity.get('conflicts') or []
    material = [c for c in conflicts
                if str((c or {}).get('severity') or '').lower() in ('high', 'medium')]
    if material:
        fields = ', '.join(sorted({str(c.get('field') or '?') for c in material}))
        return _stage('review', f'{len(material)} conflict(s): {fields}',
                      f'Review the identity conflict(s) for {atm_id} ({fields}); '
                      f'the registry is not changed automatically.')

    detail = f"TID {identity.get('terminal_id') or '—'} · {_age_label(age)} ago"
    return _stage('ok', detail)


def _ej_stage(row, facts, today):
    atm_id = row.get('atm_id') or 'this ATM'
    vendor = row.get('vendor') or 'vendor'
    if 'ej' not in facts:
        return _stage('unknown', 'Not evaluated')
    hostname = str(facts.get('ej_hostname') or '').strip()
    if not hostname:
        # A new ATM may not have a hostname yet because identity has not
        # arrived to seed it. That is progress, not a blocked registry edit;
        # once identity is present and still no hostname, a human must set it.
        if not facts.get('identity'):
            return _stage('pending', 'Waiting for identity or EJ hostname',
                          'Apply the current agent profile; identity can seed '
                          'the EJ hostname from the journal filename.')
        return _stage('blocked', 'No EJ hostname',
                      'Set the EJ hostname in ATM Registration; identity can '
                      'seed it from the journal filename.')
    if facts.get('ej_unavailable') or facts.get('onboarding_error'):
        return _stage('unknown', 'EJ evidence unavailable',
                      'Recompute the fleet report; the EJ tables could not be read.')
    ej = facts.get('ej')
    if not ej:
        return _stage('pending', 'No journal ingested',
                      f'Confirm the journal pull includes {atm_id}: run '
                      f'ej_pull_targets.py --explain, then check the {vendor} pull cron.')

    journal_date = _parse_date(ej.get('journal_date'))
    if journal_date is None:
        return _stage('pending', 'No journal date',
                      f'Confirm the journal pull includes {atm_id}: run '
                      f'ej_pull_targets.py --explain, then check the {vendor} pull cron.')
    errors = int(ej.get('parse_errors') or 0)
    if errors:
        return _stage('review', f'{errors} parse error(s) on {journal_date.isoformat()}',
                      'Open EJ Journal, fix the parse error, then re-run the pull and ingest.')
    if journal_date < today - timedelta(days=1):
        return _stage('stale', f'Latest journal {journal_date.isoformat()}',
                      f'Check the {vendor} journal pull for {atm_id}; the latest '
                      f'journal is {journal_date.isoformat()} (older than yesterday).')
    return _stage('ok', f'Journal {journal_date.isoformat()}')


def _images_stage(row, facts):
    ip = row.get('ip') or 'the box'
    if 'identity' not in facts:
        return _stage('unknown', 'Not evaluated')
    if facts.get('identity_unavailable') or facts.get('onboarding_error'):
        return _stage('unknown', 'Image evidence unavailable',
                      'Recompute the fleet report; identity could not be read.')
    identity = facts.get('identity')
    if not identity:
        return _stage('pending', 'Waiting for identity',
                      f'Apply the current agent profile on {ip}; the identity '
                      f'probe has not reported image roots yet.')
    if 'approved_image_roots' not in facts:
        return _stage('unknown', 'Not evaluated')
    if facts.get('image_roots_unavailable'):
        return _stage('unknown', 'Approved roots unavailable',
                      'Recompute the fleet report; the image allow-list could not be read.')
    observed = identity.get('image_roots') or []
    approved = {str(p).lower() for p in (facts.get('approved_image_roots') or [])}
    unapproved = [p for p in observed if str(p).lower() not in approved]
    if unapproved:
        return _stage('review', f'{len(unapproved)} root(s) need approval',
                      f'Approve the reported image root(s) on Agent setup, then '
                      f'reapply the profile on {ip}.')
    if observed:
        return _stage('ok', f'{len(observed)} root(s) approved')
    return _stage('pending', 'No image root reported',
                  f'Confirm the camera/image path exists on {ip}; the identity '
                  f'probe reported no image root.')


def overall_status(stages):
    """Worst stage wins, in the order an operator must fix them."""
    statuses = [s.get('status') for s in (stages or {}).values()]
    if statuses and all(s == 'ok' for s in statuses):
        return 'complete'
    if 'blocked' in statuses:
        return 'blocked'
    if 'review' in statuses:
        return 'review'
    if any(s in ('pending', 'stale') for s in statuses):
        return 'in_progress'
    if 'unknown' in statuses:
        return 'unknown'
    return 'unknown'


def next_action(stages):
    for key in STAGES:
        stage = (stages or {}).get(key) or {}
        if stage.get('status') in ACTIONABLE:
            return stage.get('action') or stage.get('detail') or 'Investigate this stage.'
    return 'No action needed.'


def needs_action(stages):
    return any(((stages or {}).get(key) or {}).get('status') in ACTIONABLE
               for key in STAGES)


def evaluate(row, facts=None, now=None, today=None):
    """Return the onboarding verdict for one fleet row.

    facts is the verify() facts dict. `now`/`today` are injection points for
    tests; production uses the current UTC time and the EAT calendar date.
    """
    facts = facts if facts is not None else (row.get('facts') or {})
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone(EAT).date()

    stages = {
        'provisioning': _provisioning_stage(row),
        'reporting': _reporting_stage(row),
        'identity': _identity_stage(row, facts, now),
        'ej': _ej_stage(row, facts, today),
        'images': _images_stage(row, facts),
    }
    status = overall_status(stages)
    return {
        'status': status,
        'label': OVERALL_LABEL.get(status, status.replace('_', ' ').title()),
        'stages': stages,
        'next_action': next_action(stages),
        'needs_action': needs_action(stages),
    }
