"""
blueprints/incidents.py
In-portal Incident Management — GLPI tickets without leaving the
portal (Phase 3 of single-login UX).

Reads via the GLPI legacy REST API with the app token + service account
(GLPI_APP_TOKEN / GLPI_USER / GLPI_API_PASSWORD). Writes (acknowledge,
assign, note, solve) go through the same service account; every action is
attributed to the portal user in the followup text + portal audit log.
"""
import logging
import requests
import os
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, jsonify, session
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from audit import log_action

logger = logging.getLogger(__name__)

bp = Blueprint('incidents', __name__, url_prefix='/incidents')

GLPI_URL = os.environ.get('GLPI_UI_URL', 'http://localhost:8082').rstrip('/')
GLPI_APP_TOKEN = os.environ.get('GLPI_APP_TOKEN', '')
GLPI_USER = os.environ.get('GLPI_USER', 'glpi')
GLPI_API_PASSWORD = os.environ.get('GLPI_API_PASSWORD', '')

STATUS_NAMES = {
    1: 'New', 2: 'Processing (assigned)', 3: 'Processing (planned)',
    4: 'Pending', 5: 'Solved', 6: 'Closed',
}
PRIORITY_NAMES = {
    1: 'Very low', 2: 'Low', 3: 'Medium', 4: 'High', 5: 'Very high', 6: 'Major',
}

EAT = timezone(timedelta(hours=3))

# Short-lived cache: one page load costs 3 sequential GLPI round-trips
# (session open, list, close). 60s TTL; filters apply per request.
_cache = {'ts': 0.0, 'tickets': None}
CACHE_TTL = 60.0


def _cached_tickets():
    """Return raw ticket rows, refreshing at most every CACHE_TTL."""
    import time
    now = time.time()
    if _cache['tickets'] is not None and now - _cache['ts'] < CACHE_TTL:
        return _cache['tickets']
    token = _session()
    if not token:
        return None
    try:
        raw = _tickets(token)
        if raw is None and _cache['tickets'] is not None:
            return _cache['tickets']  # hiccup: serve stale
        _cache.update(ts=now, tickets=raw or [])
        return raw or []
    finally:
        _close(token)


def _fmt_ts(ts):
    if not ts:
        return '—'
    try:
        dt = datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return dt.astimezone(EAT).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return str(ts)[:16]


def _session():
    """Open a GLPI API session; return session token or None."""
    if not (GLPI_APP_TOKEN and GLPI_USER and GLPI_API_PASSWORD):
        return None
    try:
        r = requests.get(
            f'{GLPI_URL}/apirest.php/initSession',
            headers={'App-Token': GLPI_APP_TOKEN},
            auth=(GLPI_USER, GLPI_API_PASSWORD),
            timeout=6,
        )
        if r.status_code == 200:
            return r.json().get('session_token')
        logger.warning('GLPI initSession status=%s', r.status_code)
    except Exception as e:
        logger.warning('GLPI initSession failed: %s', e)
    return None


def _close(token):
    try:
        requests.get(f'{GLPI_URL}/apirest.php/killSession',
                     headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
                     timeout=10)
    except Exception:
        pass


def _tickets(token):
    try:
        r = requests.get(
            f'{GLPI_URL}/apirest.php/Ticket/',
            params={'range': '0-99', 'order': 'DESC', 'expand_dropdowns': 'true'},
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            timeout=12,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning('GLPI Ticket.list status=%s', r.status_code)
    except Exception as e:
        logger.warning('GLPI Ticket.list failed: %s', e)
    return None


def _invalidate_cache():
    _cache.update(ts=0.0, tickets=None)


def _api(token, method, path, payload=None):
    """One GLPI call; returns (ok, data_or_error). Never raises."""
    try:
        r = requests.request(
            method, f'{GLPI_URL}/apirest.php/{path}',
            json=payload,
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            timeout=12,
        )
        if r.status_code in (200, 201):
            try:
                return True, r.json()
            except ValueError:
                return True, {}
        try:
            err = r.json()
            msg = err[1] if isinstance(err, list) and len(err) > 1 else err
        except ValueError:
            msg = r.text[:200]
        logger.warning('GLPI %s %s → %s: %s', method, path, r.status_code, msg)
        return False, f'GLPI refused ({r.status_code}): {msg}'
    except Exception as e:
        logger.warning('GLPI %s %s failed: %s', method, path, e)
        return False, f'Cannot reach GLPI: {e}'


def _add_followup(token, ticket_id, content):
    """Version-tolerant followup add (ITILFollowup on 10+, TicketFollowup before)."""
    for item in ('ITILFollowup', 'TicketFollowup'):
        ok, data = _api(token, 'POST', f'{item}/', {'input': {
            'itemtype': 'Ticket', 'items_id': int(ticket_id), 'content': content}})
        if ok:
            return True, data
        if 'Invalid itemtype' in str(data) or '404' in str(data):
            continue
        # Real refusal (rights, validation) — don't mask it with the fallback.
        if item == 'ITILFollowup':
            ok2, data2 = _api(token, 'POST', 'TicketFollowup/', {'input': {
                'itemtype': 'Ticket', 'items_id': int(ticket_id), 'content': content}})
            return ok2, data2
        return ok, data
    return False, 'Followup endpoint not found'


def _mutate(ticket_id, fn):
    """Open session → run fn(token) → close. Returns JSON-ready result."""
    token = _session()
    if not token:
        return {'success': False,
                'message': 'Cannot reach the GLPI API — check credentials and API access.'}
    try:
        ok, message = fn(token)
    finally:
        _close(token)
    if ok:
        _invalidate_cache()
    return {'success': ok, 'message': message}


def _actor():
    try:
        return session.get('username', 'system')
    except Exception:
        return 'system'


_ALLOWED_TAGS = {'p', 'br', 'b', 'strong', 'i', 'em', 'u', 'ul', 'ol', 'li',
                 'a', 'blockquote', 'div', 'span', 'pre', 'code', 'hr'}


def _clean_html(html):
    """Strip everything but simple formatting (GLPI stores rich text)."""
    import re
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []

        def handle_starttag(self, tag, attrs):
            if tag in _ALLOWED_TAGS:
                if tag == 'a':
                    href = next((v for k, v in attrs if k == 'href'), '#')
                    href = href.replace('"', '')
                    self.out.append(f'<a href="{href}" target="_blank" rel="noopener">')
                elif tag == 'br':
                    self.out.append('<br>')
                else:
                    self.out.append(f'<{tag}>')

        def handle_endtag(self, tag):
            if tag in _ALLOWED_TAGS and tag != 'br':
                self.out.append(f'</{tag}>')

        def handle_data(self, data):
            self.out.append(data.replace('<', '&lt;').replace('>', '&gt;'))

    p = _P()
    try:
        p.feed(html or '')
    except Exception:
        return re.sub(r'<[^>]+>', '', html or '')
    return ''.join(p.out).strip()


def _subitems(token, ticket_id, sub):
    ok, data = _api(token, 'GET', f'Ticket/{ticket_id}/{sub}/', None)
    if ok and isinstance(data, list):
        return data
    return []


def _user_label(token, users_id, cache):
    if not users_id or users_id in cache:
        return cache.get(users_id, '—')
    ok, data = _api(token, 'GET', f'User/{users_id}', None)
    if ok and isinstance(data, dict):
        name = ' '.join(p for p in ((data.get('realname') or '').strip(),
                                    (data.get('firstname') or '').strip()) if p)
        cache[users_id] = name or (data.get('name') or f'#{users_id}')
    else:
        cache[users_id] = f'#{users_id}'
    return cache[users_id]


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def board():
    status = request.args.get('status', '').strip()
    error = None
    tickets = []
    counts = {'open': 0, 'pending': 0, 'solved': 0}

    raw = _cached_tickets()
    if raw is None:
        error = ('Cannot reach the GLPI API — check GLPI_APP_TOKEN / GLPI_USER / '
                 'GLPI_API_PASSWORD and that the API client is enabled.')
        raw = []
    else:
        for t in raw:
            try:
                st = int(t.get('status', 0))
            except (TypeError, ValueError):
                st = 0
            if st in (1, 2, 3):
                counts['open'] += 1
            elif st == 4:
                counts['pending'] += 1
            elif st in (5, 6):
                counts['solved'] += 1
            if status == 'open' and st not in (1, 2, 3):
                continue
            if status == 'pending' and st != 4:
                continue
            if status == 'solved' and st not in (5, 6):
                continue
            try:
                pr = int(t.get('priority', 0))
            except (TypeError, ValueError):
                pr = 0
            tickets.append({
                'id': t.get('id'),
                'title': t.get('name') or '(no title)',
                'status': STATUS_NAMES.get(st, str(st)),
                'status_n': st,
                'priority': PRIORITY_NAMES.get(pr, str(pr) if pr else '—'),
                'priority_n': pr,
                'category': t.get('itilcategories_id') or '—',
                'requester': t.get('users_id_recipient') or '—',
                'created': _fmt_ts(t.get('date_creation')),
                'updated': _fmt_ts(t.get('date_mod')),
            })

    return render_template('incidents.html', tickets=tickets, counts=counts,
                           f_status=status, error=error)


@bp.route('/<int:ticket_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def detail(ticket_id):
    error = None
    ticket, timeline = None, []
    token = _session()
    if not token:
        error = ('Cannot reach the GLPI API — check GLPI_APP_TOKEN / GLPI_USER / '
                 'GLPI_API_PASSWORD and that the API client is enabled.')
    else:
        try:
            r = requests.get(f'{GLPI_URL}/apirest.php/Ticket/{ticket_id}',
                             params={'expand_dropdowns': 'true'},
                             headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
                             timeout=12)
            if r.status_code != 200:
                error = f'Ticket {ticket_id} not found in GLPI.'
            else:
                t = r.json()
                try:
                    st = int(t.get('status', 0))
                except (TypeError, ValueError):
                    st = 0
                try:
                    pr = int(t.get('priority', 0))
                except (TypeError, ValueError):
                    pr = 0
                names = {}
                ticket = {
                    'id': t.get('id'), 'title': t.get('name') or '(no title)',
                    'status': STATUS_NAMES.get(st, str(st)), 'status_n': st,
                    'priority': PRIORITY_NAMES.get(pr, str(pr) if pr else '—'),
                    'priority_n': pr,
                    'category': t.get('itilcategories_id') or '—',
                    'requester': t.get('users_id_recipient') or '—',
                    'created': _fmt_ts(t.get('date_creation')),
                    'updated': _fmt_ts(t.get('date_mod')),
                    'content': _clean_html(t.get('content') or ''),
                }
                for f in _subitems(token, ticket_id, 'ITILFollowup') + \
                         _subitems(token, ticket_id, 'TicketFollowup'):
                    timeline.append({
                        'kind': 'followup',
                        'author': _user_label(token, f.get('users_id'), names),
                        'date': _fmt_ts(f.get('date_creation') or f.get('date')),
                        'private': bool(int(f.get('is_private') or 0)),
                        'html': _clean_html(f.get('content') or ''),
                    })
                for s in _subitems(token, ticket_id, 'ITILSolution'):
                    timeline.append({
                        'kind': 'solution',
                        'author': _user_label(token, s.get('users_id'), names),
                        'date': _fmt_ts(s.get('date_creation') or s.get('date')),
                        'private': False,
                        'html': _clean_html(s.get('content') or ''),
                    })
                for k in _subitems(token, ticket_id, 'TicketTask'):
                    timeline.append({
                        'kind': 'task',
                        'author': _user_label(token, k.get('users_id'), names),
                        'date': _fmt_ts(k.get('date_creation') or k.get('date')),
                        'private': False,
                        'html': _clean_html(
                            f"<b>{k.get('taskcategories_id') or 'Task'}</b><br>" + (k.get('content') or '')),
                    })
        finally:
            _close(token)
    return render_template('incidents_detail.html', ticket=ticket,
                           timeline=timeline, error=error)


@bp.route('/<int:ticket_id>/acknowledge', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def acknowledge(ticket_id):
    actor = _actor()

    def _run(token):
        ok, data = _api(token, 'PUT', f'Ticket/{ticket_id}', {'input': {'status': 2}})
        if not ok:
            return False, data
        _add_followup(token, ticket_id, f'Acknowledged from Report Portal by {actor}.')
        return True, f'Ticket {ticket_id} acknowledged.'

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_ACK', f'Ticket {ticket_id} acknowledged by {actor}')
    return jsonify(res)


@bp.route('/<int:ticket_id>/followup', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def followup(ticket_id):
    actor = _actor()
    content = (request.get_json(silent=True) or {}).get('content', '').strip()
    if not content:
        return jsonify({'success': False, 'message': 'Note text is required.'})
    if len(content) > 2000:
        return jsonify({'success': False, 'message': 'Note is too long (max 2000 characters).'})

    def _run(token):
        ok, data = _add_followup(token, ticket_id, f'{content}\n— {actor} (Report Portal)')
        return (True, f'Note added to ticket {ticket_id}.') if ok else (False, data)

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_NOTE', f'Note on ticket {ticket_id} by {actor}')
    return jsonify(res)


@bp.route('/users')
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def technician_search():
    """Autocomplete source for the assign dialog (GLPI users)."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    token = _session()
    if not token:
        return jsonify([])
    try:
        # searchText is a query param — call directly.
        r = requests.get(f'{GLPI_URL}/apirest.php/User/',
                         params={'searchText': q, 'range': '0-9'},
                         headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
                         timeout=12)
        out = []
        if r.status_code == 200:
            for u in r.json() or []:
                name = (u.get('realname') or '').strip()
                first = (u.get('firstname') or '').strip()
                login = u.get('name') or ''
                label = ' '.join(p for p in (name, first) if p) or login
                if label:
                    out.append({'id': u.get('id'), 'label': f'{label} ({login})'})
    finally:
        _close(token)
    return jsonify(out)


@bp.route('/<int:ticket_id>/assign', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def assign(ticket_id):
    actor = _actor()
    body = request.get_json(silent=True) or {}
    try:
        users_id = int(body.get('users_id', 0))
    except (TypeError, ValueError):
        users_id = 0
    if not users_id:
        return jsonify({'success': False, 'message': 'Select a technician to assign.'})

    def _run(token):
        ok, data = _api(token, 'POST', 'Ticket_User/', {'input': {
            'tickets_id': ticket_id, 'users_id': users_id, 'type': 2}})
        if not ok:
            return False, data
        _api(token, 'PUT', f'Ticket/{ticket_id}', {'input': {'status': 2}})
        _add_followup(token, ticket_id,
                      f'Assigned from Report Portal by {actor} (GLPI user id {users_id}).')
        return True, f'Ticket {ticket_id} assigned.'

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_ASSIGN', f'Ticket {ticket_id} assigned to GLPI user {users_id} by {actor}')
    return jsonify(res)


@bp.route('/<int:ticket_id>/solve', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def solve(ticket_id):
    actor = _actor()
    solution = (request.get_json(silent=True) or {}).get('solution', '').strip()
    if not solution:
        return jsonify({'success': False, 'message': 'A solution description is required.'})
    if len(solution) > 2000:
        return jsonify({'success': False, 'message': 'Solution is too long (max 2000 characters).'})

    def _run(token):
        # Canonical path: adding a solution flips the ticket to Solved.
        ok, data = _api(token, 'POST', 'ITILSolution/', {'input': {
            'itemtype': 'Ticket', 'items_id': ticket_id, 'content': f'{solution}\n— {actor} (Report Portal)'}})
        if ok:
            return True, f'Ticket {ticket_id} solved.'
        # Fallback for older GLPI: direct status change.
        ok2, data2 = _api(token, 'PUT', f'Ticket/{ticket_id}', {'input': {'status': 5}})
        if ok2:
            _add_followup(token, ticket_id, f'Marked solved from Report Portal by {actor}: {solution}')
            return True, f'Ticket {ticket_id} solved.'
        return False, data

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_SOLVE', f'Ticket {ticket_id} solved by {actor}')
    return jsonify(res)
