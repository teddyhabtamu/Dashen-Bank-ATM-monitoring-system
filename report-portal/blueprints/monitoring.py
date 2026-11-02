"""
blueprints/monitoring.py
In-portal Monitoring Center — Zabbix problems + host states without
leaving the portal (Phase 1 of single-login UX).

Reads via the Zabbix API service account (same helpers as admin.py).
Acknowledge / un-acknowledge are the first write actions, OPERATOR and above.
Suppress, close and severity changes still happen in Zabbix itself.
"""
import logging
import time
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
_cache = {'ts': 0.0, 'hosts': None, 'problems': None, 'maintenances': None}
CACHE_TTL = 20.0

# Both tables are paged server-side. The host sweep already returns the whole
# fleet; slicing before rendering keeps 1,200 rows out of the DOM and out of
# the 20s live payload. Problems used to stop at 100; the higher fetch cap
# makes the rest reachable by pagination instead of silently hiding them.
MC_PAGE_SIZES = (25, 50, 100)
MC_DEFAULT_PAGE_SIZE = 50
PROBLEM_FETCH_LIMIT = 5000


def _mc_per_page():
    try:
        per_page = int(request.args.get('per_page', MC_DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = MC_DEFAULT_PAGE_SIZE
    return per_page if per_page in MC_PAGE_SIZES else MC_DEFAULT_PAGE_SIZE


def _mc_page(rows, arg, per_page):
    """(slice, page, pages, start, end) with the page clamped to real bounds."""
    try:
        page = max(1, int(request.args.get(arg, 1)))
    except (TypeError, ValueError):
        page = 1
    total = len(rows)
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    return rows[start:end], page, pages, start, end


def _cached_zabbix():
    """Return (hosts, problems, maintenances) raw rows, at most every CACHE_TTL."""
    now = time.time()
    if _cache['hosts'] is not None and now - _cache['ts'] < CACHE_TTL:
        return _cache['hosts'], _cache['problems'], _cache['maintenances']
    auth = _zbx_login()
    maintenances = []
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
                return None, None, []
            return _cache['hosts'], _cache['problems'], _cache['maintenances']
    # host.get, problem.get and maintenance.get are independent — run them
    # together so a slow Zabbix costs one timeout window, not three.
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            fh = ex.submit(_zbx_call, 'host.get', {
                'output': ['hostid', 'host', 'name', 'status', 'available',
                           'snmp_available', 'jmx_available', 'ipmi_available'],
                'selectInterfaces': ['ip'],
                'selectGroups': ['name'],
                'sortfield': 'host',
            }, auth)
            fp = ex.submit(_problem_get, auth)
            fm = ex.submit(_zbx_call, 'maintenance.get', {
                'output': ['maintenanceid', 'name', 'active_since', 'active_till',
                           'maintenance_type'],
                'selectHosts': ['hostid'],
            }, auth)
            hosts_raw = fh.result(timeout=12)
            problems = fp.result(timeout=12)
            try:
                maintenances = fm.result(timeout=12) or []
            except _FutureTimeout:
                # Maintenance is decoration; never let a slow read discard a
                # healthy host/problem pair.
                logger.warning('maintenance.get exceeded the 12s budget')
                maintenances = []
    except _FutureTimeout:
        logger.warning('Zabbix fetch exceeded 12s budget')
        hosts_raw, problems = None, None
    if hosts_raw is None or problems is None:
        # Either leg failed: prefer a consistent stale pair over mixing a
        # fresh empty with live rows (which reads as "all clear").
        if _cache['hosts'] is not None:
            return _cache['hosts'], _cache['problems'], _cache['maintenances']
        if hosts_raw is None and problems is None:
            return None, None, maintenances  # unreachable, nothing stale to show
        return hosts_raw or [], problems or [], maintenances
    hosts = hosts_raw
    _cache.update(ts=now, hosts=hosts, problems=problems or [],
                  maintenances=maintenances)
    return hosts, problems or [], maintenances


def _problem_get(auth):
    """Active problems, including their acknowledgement history.

    selectAcknowledges carries the operator's comment (the reason for the
    ack). A build that rejects the parameter must not take the whole problem
    list down with it - the page still works, just without the notes.
    """
    params = {
        'output': ['eventid', 'clock', 'name', 'severity', 'acknowledged',
                   'suppressed', 'objectid'],
        'selectHosts': ['hostid', 'host', 'name'],
        'selectAcknowledges': 'extend',
        'selectSuppressionData': 'extend',
        'selectTags': 'extend',
        # NB: problem.get allows sorting by eventid only (clock is rejected);
        # eventids grow with time so DESC is newest-first.
        'sortfield': 'eventid',
        'sortorder': 'DESC',
        'limit': PROBLEM_FETCH_LIMIT,
    }
    res = _zbx_call('problem.get', params, auth)
    if res is None:
        # A build that rejects either select must still show the problem
        # list; the page degrades to no notes and no expiry, not to blank.
        params.pop('selectAcknowledges', None)
        params.pop('selectSuppressionData', None)
        params.pop('selectTags', None)
        res = _zbx_call('problem.get', params, auth)
    return res


_user_names = {}


def _user_map(userids):
    """userid -> username, cached per process.

    Older builds return only userid on an acknowledge. One extra call is
    cheaper than showing "1" where a person's name belongs, and the cache
    keeps it to one call per process.
    """
    missing = [u for u in userids if u and u not in _user_names]
    if missing:
        auth = _zbx_login()
        rows = _zbx_call('user.get', {'userids': missing,
                                      'output': ['userid', 'username']},
                         auth) if auth else None
        for r in (rows or []):
            _user_names[str(r.get('userid'))] = r.get('username') or ''
    return _user_names


def _fmt_clock(clock):
    try:
        dt = datetime.fromtimestamp(int(clock), tz=timezone.utc)
        return dt.astimezone(EAT).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return '—'


def _fmt_short(clock):
    try:
        dt = datetime.fromtimestamp(int(clock), tz=timezone.utc)
        return dt.astimezone(EAT).strftime('%d %b %H:%M')
    except (TypeError, ValueError):
        return ''


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def center():
    severity = request.args.get('severity', '').strip()
    ack = request.args.get('ack', '').strip()  # '' | 'no' | 'yes'
    per_page = _mc_per_page()
    summary, all_problems, all_hosts, error, at_cap = _collect(severity, ack)
    problems, ppage, ppages, prob_start, prob_end = _mc_page(
        all_problems, 'ppage', per_page)
    hosts, hpage, hpages, host_start, host_end = _mc_page(
        all_hosts, 'hpage', per_page)
    # The duration list is rendered from the same table the route validates
    # against, so the UI cannot offer a value the backend will reject.
    from problem_actions import SUPPRESS_LABELS
    is_admin = session.get('role') == ROLE_ADMIN
    keys = ('30', '120', '480', '1440', '0') if is_admin else ('30', '120', '480', '1440')
    return render_template('monitoring_center.html',
                           problems=problems, hosts=hosts, summary=summary,
                           problem_total=len(all_problems),
                           host_total=len(all_hosts),
                           ppage=ppage, ppages=ppages,
                           prob_start=prob_start, prob_end=prob_end,
                           hpage=hpage, hpages=hpages,
                           host_start=host_start, host_end=host_end,
                           per_page=per_page, page_sizes=MC_PAGE_SIZES,
                           severities=SEVERITIES, sev_colors=SEV_COLORS,
                           f_severity=severity, f_ack=ack, error=error,
                           at_cap=at_cap,
                           suppress_options=[(k, SUPPRESS_LABELS[k]) for k in keys],
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
    """Acknowledge, un-acknowledge, suppress or unsuppress a problem.

    A write path on a page that was read-only, so it is OPERATOR and above.
    Zabbix records the API service account as the actor; the human is recorded
    in the portal audit log, which is the only place that knows who clicked.
    """
    from problem_actions import LABELS, ack_params, suppress_until
    note = (request.form.get('message') or '').strip()
    until = None
    if action == 'suppress':
        try:
            until = suppress_until(request.form.get('duration', ''),
                                   time.time(),
                                   admin=session.get('role') == ROLE_ADMIN)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(_back())
    try:
        params = ack_params(eventid, action, note, until=until)
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(_back())

    auth = _zbx_login()
    if not auth or _zbx_call('event.acknowledge', params, auth) is None:
        flash('Zabbix did not accept the change. It may already be in that '
              'state, or the API account may lack permission.', 'error')
        return redirect(_back())

    # The page redirects into a 20s read cache. Without this the Ack and
    # Suppression columns still show the old state and the action looks like
    # it did nothing.
    _cache['ts'] = 0.0
    detail = 'eventid=%s' % eventid
    if action == 'suppress':
        detail += ' until=%s' % ('indefinite' if not until else _fmt_clock(until))
    if note:
        detail += ' — ' + note
    log_action('PROBLEM_' + action.upper(), detail)
    if action == 'suppress':
        when = 'indefinitely' if not until else 'until ' + _fmt_clock(until)
        flash('Suppressed event %s %s.' % (eventid, when), 'success')
    elif action == 'unsuppress':
        flash('Suppression removed from event %s.' % eventid, 'success')
    else:
        flash('%s event %s.' % (LABELS.get(action, action), eventid), 'success')
    return redirect(_back())


BULK_ACTIONS = {'acknowledge', 'suppress'}


@bp.route('/bulk/<action>', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def problem_bulk_action(action):
    """Acknowledge or suppress many problems in one Zabbix call.

    Bulk un-acknowledge is deliberately not offered: during a storm it would
    erase the ownership operators just took. The route refuses it even if a
    crafted form asks, rather than relying on the toolbar not rendering it.
    """
    from problem_actions import ack_params, suppress_until
    if action not in BULK_ACTIONS:
        flash('That action is not available in bulk.', 'error')
        return redirect(_back())

    note = (request.form.get('message') or '').strip()
    until = None
    if action == 'suppress':
        try:
            until = suppress_until(request.form.get('duration', ''),
                                   time.time(),
                                   admin=session.get('role') == ROLE_ADMIN)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(_back())
    try:
        params = ack_params(request.form.get('eventids', ''), action, note, until=until)
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(_back())

    auth = _zbx_login()
    if not auth or _zbx_call('event.acknowledge', params, auth) is None:
        flash('Zabbix did not accept the bulk change. Nothing was applied.', 'error')
        return redirect(_back())

    _cache['ts'] = 0.0
    count = len(params['eventids'])
    detail = 'events=%d' % count
    if action == 'suppress':
        detail += ' until=%s' % ('indefinite' if not until else _fmt_clock(until))
    if note:
        detail += ' — ' + note
    log_action('PROBLEM_BULK_' + action.upper(), detail)
    if action == 'suppress':
        when = 'indefinitely' if not until else 'until ' + _fmt_clock(until)
        flash('Suppressed %d problems %s.' % (count, when), 'success')
    else:
        flash('Acknowledged %d problems.' % count, 'success')
    return redirect(_back())


@bp.route('/api/live')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def api_live():
    """Pollable twin of center() for the 20s autosync (same shaping + pages)."""
    severity = request.args.get('severity', '').strip()
    ack = request.args.get('ack', '').strip()
    per_page = _mc_per_page()
    summary, all_problems, all_hosts, error, at_cap = _collect(severity, ack)
    problems, ppage, ppages, _ps, _pe = _mc_page(all_problems, 'ppage', per_page)
    hosts, hpage, hpages, _hs, _he = _mc_page(all_hosts, 'hpage', per_page)
    for p in problems:
        p['sev_color'] = SEV_COLORS.get(p['severity'], '#97AAB3')
    return jsonify({'summary': summary, 'problems': problems, 'hosts': hosts,
                    'error': error, 'at_cap': at_cap,
                    'problem_total': len(all_problems),
                    'host_total': len(all_hosts),
                    'ppage': ppage, 'ppages': ppages,
                    'hpage': hpage, 'hpages': hpages,
                    'per_page': per_page})


def _collect(severity, ack):
    error = None
    problems = []
    hosts = []
    summary = {'hosts': 0, 'up': 0, 'down': 0, 'problems': 0, 'unacked': 0}

    raw_hosts, raw_problems, raw_maintenances = _cached_zabbix()
    if raw_hosts is None:
        error = 'Cannot reach Zabbix API — showing last known state is unavailable. Check that zabbix-web is running.'
        raw_hosts, raw_problems = [], []
    from maintenance import active_by_host
    active_maint = active_by_host(raw_maintenances, time.time())
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
        m = active_maint.get(str(h.get('hostid')))
        hosts.append({
            'hostid': h.get('hostid'),
            'host': h.get('host'),
            'name': h.get('name') or h.get('host'),
            'ip': (h.get('interfaces') or [{}])[0].get('ip', '—'),
            'groups': groups or '—',
            'available': signals[0],
            'state': state,
            'maintenance_name': (m or {}).get('name') or '',
            'maintenance_until': _fmt_short(m.get('active_till')) if m else '',
        })
    summary['hosts'] = len(hosts)
    summary['up'] = sum(1 for h in hosts if h['state'] == 'UP')
    summary['down'] = sum(1 for h in hosts if h['state'] == 'DOWN')

    # ── Active problems (summary over ALL cached rows; severity/ack
    # filters apply to the displayed list only) ──
    from problem_actions import glpi_ticket, notes_from, suppression_state
    all_problems = []
    for p in raw_problems:
        host = (p.get('hosts') or [{}])[0]
        notes = notes_from(p.get('acknowledges'))
        suppressed, sup_until, maintenance = suppression_state(p)
        ticket_id = glpi_ticket(p)
        all_problems.append({
            'eventid': p.get('eventid'),
            'clock': _fmt_clock(p.get('clock')),
            'name': p.get('name'),
            'severity': str(p.get('severity', '0')),
            'severity_name': SEVERITIES.get(str(p.get('severity', '0')), '?'),
            'acknowledged': str(p.get('acknowledged')) == '1',
            'host': host.get('name') or host.get('host') or '—',
            'objectid': p.get('objectid'),
            'ack_notes': notes,
            'ack_note': notes[0]['message'] if notes else '',
            'suppressed': suppressed,
            'suppress_until': sup_until,
            'suppress_until_label': _fmt_short(sup_until) if sup_until else '',
            'maintenance': maintenance,
            'glpi_id': ticket_id,
            'ticket_url': ('/incidents/%s' % ticket_id) if ticket_id else '',
        })
    # Resolve any acknowledge author that came back as a bare userid, then
    # format the history once for both the row and its tooltip.
    needed = {n['userid'] for p in all_problems for n in p['ack_notes']
              if n.get('userid') and not n.get('user')}
    names = _user_map(needed) if needed else {}
    for p in all_problems:
        for n in p['ack_notes']:
            if not n.get('user') and n.get('userid'):
                n['user'] = names.get(n['userid'], '')
            n['when'] = _fmt_clock(n['clock'])
        p['ack_tip'] = '\n'.join(
            '%s — %s: %s' % (n['when'], n['user'] or 'unknown', n['message'])
            for n in p['ack_notes'])

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
    return (summary, problems, hosts, error,
            len(raw_problems) >= PROBLEM_FETCH_LIMIT)
