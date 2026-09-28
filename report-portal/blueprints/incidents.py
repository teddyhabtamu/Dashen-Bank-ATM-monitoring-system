"""
blueprints/incidents.py
In-portal Incident Management — GLPI ticket list without leaving the
portal (Phase 3 of single-login UX).

Reads via the GLPI legacy REST API with the app token + service account
(GLPI_APP_TOKEN / GLPI_USER / GLPI_API_PASSWORD). Read-only: acknowledge,
assign and solve still happen in GLPI itself for now.
"""
import logging
import requests
import os
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN

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
                'priority': PRIORITY_NAMES.get(pr, str(pr) if pr else '—'),
                'priority_n': pr,
                'category': t.get('itilcategories_id') or '—',
                'requester': t.get('users_id_recipient') or '—',
                'created': _fmt_ts(t.get('date_creation')),
                'updated': _fmt_ts(t.get('date_mod')),
            })

    return render_template('incidents.html', tickets=tickets, counts=counts,
                           f_status=status, error=error)
