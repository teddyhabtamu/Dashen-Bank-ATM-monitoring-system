"""
blueprints/monitoring.py
In-portal Monitoring Center — Zabbix problems + host states without
leaving the portal (Phase 1 of single-login UX).

Reads via the Zabbix API service account (same helpers as admin.py).
Acknowledge / un-acknowledge are the first write actions, OPERATOR and above.
Suppress, close and severity changes still happen in Zabbix itself.
"""
import logging
from datetime import datetime, timezone, timedelta
from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from blueprints.admin import _zbx_login, _zbx_call
from audit import log_action

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

# Short-lived cache aligned UNDER the 20s frontend poll: every visible tick
# can carry fresh numbers (a TTL above the poll interval would make updates
# land on alternating ticks — burst-y, not rhythmic). Each load costs up to
# 3 sequential API round-trips; at 20s cadence Zabbix won't feel it.
_cache = {'ts': 0.0, 'hosts': None, 'problems': None}
CACHE_TTL = 20.0


def _cached_zabbix():
    """Return (hosts, problems) raw rows, refreshing at most every CACHE_TTL."""
    import time
    now = time.time()
    if _cache['hosts'] is not None and now - _cache['ts'] < CACHE_TTL:
        return _cache['hosts'], _cache['problems']
    auth = _zbx_login()
    hosts, problems = [], None
    if not auth:
        if _cache['hosts'] is None:
            # Cold cache (fresh worker boot) + failed login: one retry —
            # transient account blocks and slow API starts are the common
            # causes, and a single cold-window failure is exactly what
            # renders the Access-Denied banner (proven live 2026-10-12:
            # 3 gunicorn workers × cold caches = flapping banner).
            time.sleep(3)
            auth = _zbx_login()
        if not auth:
            if _cache['hosts'] is None:
                return None, None  # unreachable, nothing stale to show
            return _cache['hosts'], _cache['problems']
    # host.get + problem.get are independent — run them together so a slow
    # Zabbix costs one timeout window, not two, before stale cache serves.
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fh = ex.submit(_zbx_call, 'host.get', {
                'output': ['hostid', 'host', 'name', 'status', 'available',
                           'snmp_available', 'jmx_available', 'ipmi_available'],
                'selectInterfaces': ['ip'],
                'selectGroups': ['name'],
                'sortfield': 'host',
            }, auth)
            fp = ex.submit(_zbx_call, 'problem.get', {
                'output': ['eventid', 'clock', 'name', 'severity', 'acknowledged', 'objectid'],
                'selectHosts': ['hostid', 'host', 'name'],
                # NB: problem.get allows sorting by eventid only (clock is
                # rejected); eventids grow with time so DESC is newest-first.
                'sortfield': 'eventid',
                'sortorder': 'DESC',
                'limit': 100,
            }, auth)
            hosts_raw = fh.result(timeout=12)
            problems = fp.result(timeout=12)
    except _FutureTimeout:
        logger.warning('Zabbix fetch exceeded 12s budget')
        hosts_raw, problems = None, None
    if hosts_raw is None or problems is None:
        # Either leg failed: prefer a consistent stale pair over mixing a
        # fresh empty with live rows (which reads as "all clear").
        if _cache['hosts'] is not None:
            return _cache['hosts'], _cache['problems']
        if hosts_raw is None and problems is None:
            return None, None  # unreachable, nothing stale to show
        return hosts_raw or [], problems or []
    hosts = hosts_raw
    _cache.update(ts=now, hosts=hosts, problems=problems or [])
    return hosts, problems or []


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
    summary, problems, hosts, error, at_cap = _collect(severity, ack)
    return render_template('monitoring_center.html',
                           problems=problems, hosts=hosts, summary=summary,
                           severities=SEVERITIES, sev_colors=SEV_COLORS,
                           f_severity=severity, f_ack=ack, error=error,
                           at_cap=at_cap,
                           can_act=session.get('role') in (ROLE_OPERATOR, ROLE_ADMIN))


def _back():
    """Where to return after an action: the page the operator came from.

    Validated to this blueprint: a form field must never become an open
    redirect.
    """
    back = (request.form.get('back') or '').strip()
    if back.startswith('/monitoring') and '\\' not in back and '\n' not in back:
        return back
    return url_for('.center')


@bp.route('/problems/<eventid>/<action>', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def problem_action(eventid, action):
    """Acknowledge or un-acknowledge a problem without opening Zabbix.

    A write path on a page that was read-only, so it is OPERATOR and above.
    Zabbix records the API service account as the actor; the human is recorded
    in the portal audit log, which is the only place that knows who clicked.
    """
    from problem_actions import LABELS, ack_params
    try:
        params = ack_params(eventid, action)
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(_back())

    auth = _zbx_login()
    if not auth or _zbx_call('event.acknowledge', params, auth) is None:
        flash('Zabbix did not accept the change. It may already be in that '
              'state, or the API account may lack permission.', 'error')
        return redirect(_back())

    # The page redirects into a 20s read cache. Without this the Ack column
    # still shows the old value and the action looks like it did nothing.
    _cache['ts'] = 0.0
    log_action('PROBLEM_' + action.upper(), 'eventid=%s' % eventid)
    flash('%s event %s.' % (LABELS.get(action, action), eventid), 'success')
    return redirect(_back())


@bp.route('/api/live')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def api_live():
    """Pollable twin of center() for the 30s autosync (same shaping)."""
    severity = request.args.get('severity', '').strip()
    ack = request.args.get('ack', '').strip()
    summary, problems, hosts, error, at_cap = _collect(severity, ack)
    for p in problems:
        p['sev_color'] = SEV_COLORS.get(p['severity'], '#97AAB3')
    return jsonify({'summary': summary, 'problems': problems, 'hosts': hosts,
                    'error': error, 'at_cap': at_cap})


def _collect(severity, ack):
    error = None
    problems = []
    hosts = []
    summary = {'hosts': 0, 'up': 0, 'down': 0, 'problems': 0, 'unacked': 0}

    raw_hosts, raw_problems = _cached_zabbix()
    if raw_hosts is None:
        error = 'Cannot reach Zabbix API — showing last known state is unavailable. Check that zabbix-web is running.'
        raw_hosts, raw_problems = [], []
    # ── Hosts ──
    # Interface-aware state: our ATM hosts are SNMP-polled (type 2), so the
    # agent-only `available` field sits at 0/unknown forever. UP if ANY
    # interface reports 1, DOWN if any reports 2 with none up, else UNKNOWN.
    for h in raw_hosts:
        if str(h.get('status')) != '0':
            continue  # unmonitored — not part of the fleet view
        signals = [str(h.get(k, '0')) for k in
                   ('available', 'snmp_available', 'jmx_available', 'ipmi_available')]
        state = ('UP' if '1' in signals
                 else 'DOWN' if '2' in signals else 'UNKNOWN')
        groups = ', '.join(g.get('name', '') for g in (h.get('groups') or []) if g.get('name'))
        hosts.append({
            'hostid': h.get('hostid'),
            'host': h.get('host'),
            'name': h.get('name') or h.get('host'),
            'ip': (h.get('interfaces') or [{}])[0].get('ip', '—'),
            'groups': groups or '—',
            'available': signals[0],
            'state': state,
        })
    summary['hosts'] = len(hosts)
    summary['up'] = sum(1 for h in hosts if h['state'] == 'UP')
    summary['down'] = sum(1 for h in hosts if h['state'] == 'DOWN')

    # ── Active problems (summary over ALL cached rows; severity/ack
    # filters apply to the displayed list only) ──
    all_problems = []
    for p in raw_problems:
        host = (p.get('hosts') or [{}])[0]
        all_problems.append({
            'eventid': p.get('eventid'),
            'clock': _fmt_clock(p.get('clock')),
            'name': p.get('name'),
            'severity': str(p.get('severity', '0')),
            'severity_name': SEVERITIES.get(str(p.get('severity', '0')), '?'),
            'acknowledged': str(p.get('acknowledged')) == '1',
            'host': host.get('name') or host.get('host') or '—',
            'objectid': p.get('objectid'),
        })
    problems = [p for p in all_problems
                if (severity not in SEVERITIES or p['severity'] == severity)
                and (ack != 'no' or not p['acknowledged'])
                and (ack != 'yes' or p['acknowledged'])]
    # Fallback: some builds drop selectHosts on problem.get — resolve the
    # missing host names through their triggers in one batch call.
    missing = [p['objectid'] for p in all_problems
               if p['host'] == '—' and p.get('objectid')]
    if missing:
        auth = _zbx_login()
        triggers = _zbx_call('trigger.get', {
            'triggerids': missing,
            'output': ['triggerid'],
            'selectHosts': ['hostid', 'host', 'name'],
        }, auth) or [] if auth else []
        tmap = {}
        for t in triggers:
            th = (t.get('hosts') or [{}])[0]
            tmap[t.get('triggerid')] = th.get('name') or th.get('host') or '—'
        for p in all_problems:
            if p['host'] == '—' and p.get('objectid') in tmap:
                p['host'] = tmap[p['objectid']]
    summary['problems'] = len(all_problems)
    summary['unacked'] = sum(1 for p in all_problems if not p['acknowledged'])
    return summary, problems, hosts, error, len(raw_problems) >= 100
