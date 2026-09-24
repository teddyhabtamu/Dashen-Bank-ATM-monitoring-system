"""
blueprints/monitoring.py
In-portal Monitoring Center — Zabbix problems + host states without
leaving the portal (Phase 1 of single-login UX).

Reads via the Zabbix API service account (same helpers as admin.py).
Read-only: acknowledge/escalate still happen in Zabbix itself for now.
"""
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from blueprints.admin import _zbx_login, _zbx_call

logger = logging.getLogger(__name__)

bp = Blueprint('monitoring', __name__, url_prefix='/monitoring')

SEVERITIES = {
    '0': 'Not classified',
    '1': 'Information',
    '2': 'Warning',
    '3': 'Average',
    '4': 'High',
    '5': 'Disaster',
}

SEV_COLORS = {
    '0': '#97AAB3',
    '1': '#7499FF',
    '2': '#FFC859',
    '3': '#FFA059',
    '4': '#E97659',
    '5': '#E45959',
}

EAT = timezone(timedelta(hours=3))


def _fmt_clock(clock):
    try:
        dt = datetime.fromtimestamp(int(clock), tz=timezone.utc)
        return dt.astimezone(EAT).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return '—'


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def center():
    severity = request.args.get('severity', '').strip()
    ack = request.args.get('ack', '').strip()  # '' | 'no' | 'yes'
    error = None
    problems = []
    hosts = []
    summary = {'hosts': 0, 'up': 0, 'down': 0, 'problems': 0, 'unacked': 0}

    auth = _zbx_login()
    if not auth:
        error = 'Cannot reach Zabbix API — showing last known state is unavailable. Check that zabbix-web is running.'
    else:
        # ── Hosts ──
        raw_hosts = _zbx_call('host.get', {
            'output': ['hostid', 'host', 'name', 'status', 'available'],
            'selectInterfaces': ['ip'],
            'sortfield': 'host',
        }, auth) or []
        for h in raw_hosts:
            if str(h.get('status')) != '0':
                continue  # unmonitored — not part of the fleet view
            avail = str(h.get('available', '0'))
            hosts.append({
                'hostid': h.get('hostid'),
                'host': h.get('host'),
                'name': h.get('name') or h.get('host'),
                'ip': (h.get('interfaces') or [{}])[0].get('ip', '—'),
                'available': avail,
                'state': 'UP' if avail == '1' else ('DOWN' if avail == '2' else 'UNKNOWN'),
            })
        summary['hosts'] = len(hosts)
        summary['up'] = sum(1 for h in hosts if h['state'] == 'UP')
        summary['down'] = sum(1 for h in hosts if h['state'] == 'DOWN')

        # ── Active problems ──
        params = {
            'output': ['eventid', 'clock', 'name', 'severity', 'acknowledged', 'objectid'],
            'selectHosts': ['hostid', 'host', 'name'],
            # NB: problem.get allows sorting by eventid only (clock is
            # rejected); eventids grow with time so DESC is newest-first.
            'sortfield': 'eventid',
            'sortorder': 'DESC',
            'limit': 100,
        }
        if severity in SEVERITIES:
            params['severities'] = [int(severity)]
        if ack in ('yes', 'no'):
            # acknowledged filter is supported on problem.get; if the server
            # rejects it the call returns None and we retry unfiltered.
            filtered = _zbx_call('problem.get',
                                 dict(params, acknowledged=(ack == 'yes')), auth)
            raw_problems = filtered if filtered is not None else _zbx_call(params, auth)
        else:
            raw_problems = _zbx_call('problem.get', params, auth)
        if raw_problems is None:
            error = (error + ' ' if error else '') + 'Problem query failed.'
            raw_problems = []
        for p in raw_problems:
            if severity in SEVERITIES and str(p.get('severity')) != severity:
                continue
            if ack == 'no' and str(p.get('acknowledged')) == '1':
                continue
            if ack == 'yes' and str(p.get('acknowledged')) != '1':
                continue
            host = (p.get('hosts') or [{}])[0]
            problems.append({
                'eventid': p.get('eventid'),
                'clock': _fmt_clock(p.get('clock')),
                'name': p.get('name'),
                'severity': str(p.get('severity', '0')),
                'severity_name': SEVERITIES.get(str(p.get('severity', '0')), '?'),
                'acknowledged': str(p.get('acknowledged')) == '1',
                'host': host.get('name') or host.get('host') or '—',
                'objectid': p.get('objectid'),
            })
        # Fallback: some builds drop selectHosts on problem.get — resolve the
        # missing host names through their triggers in one batch call.
        missing = [p['objectid'] for p in problems
                   if p['host'] == '—' and p.get('objectid')]
        if missing:
            triggers = _zbx_call('trigger.get', {
                'triggerids': missing,
                'output': ['triggerid'],
                'selectHosts': ['hostid', 'host', 'name'],
            }, auth) or []
            tmap = {}
            for t in triggers:
                th = (t.get('hosts') or [{}])[0]
                tmap[t.get('triggerid')] = th.get('name') or th.get('host') or '—'
            for p in problems:
                if p['host'] == '—' and p.get('objectid') in tmap:
                    p['host'] = tmap[p['objectid']]
        summary['problems'] = len(problems)
        summary['unacked'] = sum(1 for p in problems if not p['acknowledged'])

    return render_template('monitoring_center.html',
                           problems=problems, hosts=hosts, summary=summary,
                           severities=SEVERITIES, sev_colors=SEV_COLORS,
                           f_severity=severity, f_ack=ack, error=error)
