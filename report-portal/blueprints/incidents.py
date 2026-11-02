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
import re
import time
from datetime import datetime, timezone, timedelta
from markupsafe import Markup
from flask import (Blueprint, render_template, request, jsonify, session,
                   Response)
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from audit import log_action
from db import get_db
from ticket_lifecycle import decide as _decide_status, status_label

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

# The board is paginated through GLPI's search engine. A page is 50 rows:
# the assignee column costs one Ticket_User call per row, so a 1,000-ticket
# list in one request would fan out into 1,000+ GLPI calls per page view.
TICKETS_PER_PAGE = 50
_search_cache = {}          # (status, page) -> (fetched_at, rows, total)
_counts_cache = {'ts': 0.0, 'data': None}

# Per-ticket detail cache (same TTL): the detail view fans out to 10+
# sequential GLPI calls (ticket, followups, solutions, tasks, user labels,
# per-message documents). Shared across users — entries hold no per-user
# state (unread/seen is computed separately per request).
_detail_cache = {}
# uid -> (fetched_at, image bytes, mime). GLPI's API serves the picture
# binary directly (`user/<id>/picture`), so no shared filesystem is needed.
# Empty bytes mean "this user has no picture" and are cached too, avoiding
# a repeated API call for every page view.
_avatar_cache = {}
_AVATAR_TTL = 300.0
# username -> (computed_at, unread_count). The sidebar polls this endpoint;
# a 20s cache stops three gunicorn workers and several tabs from repeating
# the same per-user seen-map query on every poll.
_unread_cache = {}
_UNREAD_TTL = 20.0


def _ensure_seen_table():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS ticket_seen (
                               ticket_id INT NOT NULL,
                               username VARCHAR(100) NOT NULL,
                               seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
                               PRIMARY KEY (ticket_id, username))""")
            conn.commit()
            cur.close()
    except Exception as e:
        logger.warning('ticket_seen init failed (non-fatal): %s', e)


def _seen_map(username, ticket_ids):
    """{ticket_id: seen_at} for one user. Never raises."""
    out = {}
    if not ticket_ids:
        return out
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT ticket_id, seen_at FROM ticket_seen WHERE username = %s",
                        (username,))
            for tid, ts in cur.fetchall():
                out[int(tid)] = ts
            cur.close()
    except Exception as e:
        logger.warning('ticket_seen read failed: %s', e)
    return out


def _mark_seen(username, ticket_id):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""INSERT INTO ticket_seen (ticket_id, username, seen_at)
                           VALUES (%s, %s, NOW())
                           ON CONFLICT (ticket_id, username)
                           DO UPDATE SET seen_at = NOW()""",
                        (int(ticket_id), username))
            conn.commit()
            cur.close()
        _unread_cache.pop(username, None)
    except Exception as e:
        logger.warning('ticket_seen write failed (non-fatal): %s', e)


def _unread_ids(username, raw_tickets):
    """Ticket ids that changed after this user last opened them.

    One definition for both the list's New tags and the sidebar count, so
    the number on the menu always matches the rows that carry the badge.
    Never raises.
    """
    rows = []
    for t in raw_tickets or []:
        try:
            tid = int(t.get('id'))
        except (TypeError, ValueError, AttributeError):
            continue
        rows.append((tid, _parse_utc(t.get('date_mod'))))
    if not rows:
        return set()
    seen = _seen_map(username, [tid for tid, _ in rows])
    out = set()
    for tid, mod in rows:
        if mod is None:
            continue
        if tid not in seen:
            out.add(tid)
            continue
        last = _as_utc(seen[tid])
        if last is not None and mod > last:
            out.add(tid)
    return out


def _parse_utc(ts):
    """GLPI 'YYYY-MM-DD HH:MM:SS' (UTC) → aware datetime or None."""
    try:
        return datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _as_utc(dt):
    if dt is None:
        return None
    if getattr(dt, 'tzinfo', None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
            params={
                'range': '0-99',
                'sort': 'date_mod',    # latest activity, not ticket title
                'order': 'DESC',
                'expand_dropdowns': 'true',
                'get_hateoas': '0',
            },
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            timeout=12,
        )
        # GLPI answers 206 Partial Content when a range is requested and the
        # instance holds more rows than that range covers. The body is still
        # the requested page of tickets, not an error.
        if r.status_code in (200, 206):
            return r.json()
        logger.warning('GLPI Ticket.list status=%s', r.status_code)
    except Exception as e:
        logger.warning('GLPI Ticket.list failed: %s', e)
    return None


_STATUS_VALUES = {'open': (1, 2, 3), 'pending': (4,), 'solved': (5, 6)}


def _status_criteria(status):
    """GLPI search criteria for one portal tab. Unknown status = all tickets.

    GLPI only offers the special values ``notold`` (which includes Pending)
    and ``old`` for status, so the portal's three-way split is expressed as
    explicit equals/OR pairs on search option 12 (status).
    """
    values = _STATUS_VALUES.get(status)
    if not values:
        return {}
    params = {}
    for i, value in enumerate(values):
        prefix = f'criteria[{i}]'
        if i:
            params[f'{prefix}[link]'] = 'OR'
        params[f'{prefix}[field]'] = '12'
        params[f'{prefix}[searchtype]'] = 'equals'
        params[f'{prefix}[value]'] = str(value)
    return params


def _search_ticket_ids(token, status, start, limit):
    """(ids, total) for one page of the filtered ticket list.

    The search engine reports the full match count in ``totalcount`` while
    returning only the requested range, so the board can paginate without
    downloading every ticket.
    """
    params = {
        'range': f'{start}-{start + limit - 1}',
        'sort': '19',             # date_mod: newest activity first
        'order': 'DESC',
        'forcedisplay[0]': '2',   # ticket id
    }
    params.update(_status_criteria(status))
    try:
        r = requests.get(
            f'{GLPI_URL}/apirest.php/search/Ticket',
            params=params,
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            timeout=12,
        )
        if r.status_code not in (200, 206):
            logger.warning('GLPI Ticket.search status=%s', r.status_code)
            return None, 0
        body = r.json()
    except Exception as e:
        logger.warning('GLPI Ticket.search failed: %s', e)
        return None, 0

    rows = body.get('data') if isinstance(body, dict) else body
    if isinstance(rows, dict):
        rows = list(rows.values())
    ids = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = row.get('2', row.get(2))
        if value is None:
            value = row.get('Ticket.id')
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    try:
        total = int((body or {}).get('totalcount') or 0) if isinstance(body, dict) else 0
    except (TypeError, ValueError):
        total = 0
    if total <= 0 and ids:
        total = start + len(ids)
    return ids, total


def _tickets_by_ids(token, ids):
    """Full ticket rows for one page, in the order the search returned them."""
    if not ids:
        return []
    params = {'expand_dropdowns': 'true', 'get_hateoas': '0'}
    for i, tid in enumerate(ids):
        params[f'items[{i}][itemtype]'] = 'Ticket'
        params[f'items[{i}][items_id]'] = str(tid)
    try:
        r = requests.get(
            f'{GLPI_URL}/apirest.php/getMultipleItems',
            params=params,
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            timeout=20,
        )
        if r.status_code != 200:
            logger.warning('GLPI getMultipleItems status=%s', r.status_code)
            return None
        rows = r.json()
    except Exception as e:
        logger.warning('GLPI getMultipleItems failed: %s', e)
        return None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return None
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            by_id[int(row.get('id'))] = row
        except (TypeError, ValueError):
            continue
    return [by_id[tid] for tid in ids if tid in by_id]


def _cached_ticket_page(status, page):
    """(rows, total) for one filtered page; stale data survives a hiccup."""
    key = (status, page)
    now = time.time()
    hit = _search_cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1], hit[2]
    token = _session()
    if not token:
        return (hit[1], hit[2]) if hit else (None, 0)
    try:
        start = (page - 1) * TICKETS_PER_PAGE
        ids, total = _search_ticket_ids(token, status, start, TICKETS_PER_PAGE)
        if ids is None:
            return (hit[1], hit[2]) if hit else (None, 0)
        rows = _tickets_by_ids(token, ids)
        if rows is None:
            return (hit[1], hit[2]) if hit else (None, 0)
        _search_cache[key] = (now, rows, total)
        return rows, total
    finally:
        _close(token)


def _ticket_counts():
    """Exact Open/Pending/Solved totals, cached for CACHE_TTL.

    A range of 0-0 asks GLPI for the count without downloading the rows.
    None means "cannot tell", so the caller falls back to page-local counts
    instead of showing a confident zero.
    """
    now = time.time()
    if _counts_cache['data'] is not None and now - _counts_cache['ts'] < CACHE_TTL:
        return _counts_cache['data']
    token = _session()
    if not token:
        return None
    try:
        counts = {}
        for key in _STATUS_VALUES:
            ids, total = _search_ticket_ids(token, key, 0, 1)
            if ids is None:
                return None
            counts[key] = total
    finally:
        _close(token)
    _counts_cache.update(ts=now, data=counts)
    return counts


def _invalidate_cache():
    _cache.update(ts=0.0, tickets=None)
    _search_cache.clear()
    _counts_cache.update(ts=0.0, data=None)
    _unread_cache.clear()


def _invalidate_detail(ticket_id):
    """Drop one ticket's detail entry (called after any mutation)."""
    try:
        _detail_cache.pop(int(ticket_id), None)
    except (TypeError, ValueError):
        pass


def _get_detail(ticket_id):
    """Fresh cached detail or None."""
    try:
        entry = _detail_cache.get(int(ticket_id))
    except (TypeError, ValueError):
        return None
    if entry and time.time() - entry['ts'] < CACHE_TTL:
        return entry
    return None


def _put_detail(ticket_id, ticket, timeline, ticket_files, contacts=None):
    try:
        _detail_cache[int(ticket_id)] = {
            'ts': time.time(), 'ticket': ticket,
            'timeline': timeline, 'ticket_files': ticket_files,
            'contacts': contacts or {},
        }
    except (TypeError, ValueError):
        pass


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
    ok, data, _used = _add_followup_ex(token, ticket_id, content)
    return ok, data


def _add_followup_ex(token, ticket_id, content):
    """Same, but also returns the itemtype that accepted the followup."""
    for item in ('ITILFollowup', 'TicketFollowup'):
        ok, data = _api(token, 'POST', f'{item}/', {'input': {
            'itemtype': 'Ticket', 'items_id': int(ticket_id), 'content': content}})
        if ok:
            return True, data, item
        if 'Invalid itemtype' in str(data) or '404' in str(data):
            continue
        # Real refusal (rights, validation) — don't mask it with the fallback.
        if item == 'ITILFollowup':
            ok2, data2 = _api(token, 'POST', 'TicketFollowup/', {'input': {
                'itemtype': 'Ticket', 'items_id': int(ticket_id), 'content': content}})
            return ok2, data2, 'TicketFollowup' if ok2 else item
        return ok, data, item
    return False, 'Followup endpoint not found', 'ITILFollowup'


# Reply attachments: executables never leave the portal.
_BLOCKED_EXT = {'exe', 'bat', 'cmd', 'sh', 'js', 'jar', 'msi', 'com', 'scr',
                'ps1', 'vbs', 'vbe', 'dll', 'sys', 'reg', 'lnk', 'apk'}
_MAX_REPLY_FILE = 10 * 1024 * 1024
_MAX_REPLY_FILES = 3


def _upload_followup_file(token, followup_item, followup_id, storage):
    """Upload one file to GLPI and link it to the followup. Returns (ok, msg)."""
    import json as _json
    fname = (storage.filename or '').split('/')[-1].split('\\')[-1].strip()
    if not fname:
        return False, 'empty filename'
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if ext in _BLOCKED_EXT:
        return False, f'{fname}: file type not allowed'
    data = storage.read(_MAX_REPLY_FILE + 1)
    if not data:
        return False, f'{fname}: empty file'
    if len(data) > _MAX_REPLY_FILE:
        return False, f'{fname}: over 10MB limit'
    try:
        r = requests.post(
            f'{GLPI_URL}/apirest.php/Document/',
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            files={'filename[0]': (fname, data, storage.mimetype or 'application/octet-stream')},
            data={'uploadManifest': _json.dumps(
                {'input': {'name': fname, '_filename': [fname]}})},
            timeout=60)
    except Exception as e:
        logger.warning('GLPI document upload failed: %s', e)
        return False, f'{fname}: upload failed'
    try:
        res = r.json()
    except ValueError:
        return False, f'{fname}: upload refused ({r.status_code})'
    doc_id = None
    if isinstance(res, list) and res and isinstance(res[0], dict):
        doc_id = res[0].get('id')
    elif isinstance(res, dict):
        doc_id = res.get('id')
    if not doc_id:
        logger.warning('GLPI document upload refused: %s', res)
        return False, f'{fname}: upload refused'
    ok, _ = _api(token, 'POST', 'Document_Item/', {'input': {
        'documents_id': int(doc_id), 'itemtype': followup_item,
        'items_id': int(followup_id)}})
    if not ok:
        return False, f'{fname}: uploaded but not linked'
    return True, fname


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
        _invalidate_detail(ticket_id)
        try:
            _mark_seen(session.get('username', ''), ticket_id)
        except Exception:
            pass
    return {'success': ok, 'message': message}


def _actor():
    try:
        return session.get('username', 'system')
    except Exception:
        return 'system'


_ALLOWED_TAGS = {'p', 'br', 'b', 'strong', 'i', 'em', 'u', 'ul', 'ol', 'li',
                 'a', 'blockquote', 'div', 'span', 'pre', 'code', 'hr', 'img'}

# Attachments proxy through the portal (never expose GLPI internals / sessions).
_MAX_ATTACH_BYTES = 15 * 1024 * 1024


def _docid_from_src(src):
    """Extract a GLPI document id from an embedded image URL, else None."""
    import re
    from urllib.parse import urlparse, parse_qs
    try:
        q = parse_qs(urlparse(src or '').query)
        for key in ('docid', 'id'):
            if key in q and str(q[key][0]).isdigit():
                return int(q[key][0])
    except Exception:
        pass
    return None


def _clean_html(html, ticket_id=None):
    """Strip everything but simple formatting (GLPI stores rich text).

    Embedded GLPI images are rewritten to the portal attachment proxy;
    anything else external is dropped (no leaking, no tracking pixels).
    """
    import re
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []

        def handle_starttag(self, tag, attrs):
            if tag not in _ALLOWED_TAGS:
                return
            attrs = dict(attrs)
            if tag == 'a':
                href = (attrs.get('href') or '').replace('"', '').strip()
                # Scheme allowlist — javascript:/data:/vbscript: become plain text.
                if not re.match(r'^(https?://|mailto:|#)', href, re.IGNORECASE):
                    return
                self.out.append(f'<a href="{href}" target="_blank" rel="noopener">')
            elif tag == 'br':
                self.out.append('<br>')
            elif tag == 'img':
                docid = _docid_from_src(attrs.get('src', ''))
                if docid and ticket_id:
                    self.out.append(
                        f'<img src="/incidents/{ticket_id}/attachment/{docid}" '
                        f'alt="attachment" loading="lazy" '
                        f'style="max-width:100%;border-radius:8px;margin-top:6px">')
            else:
                self.out.append(f'<{tag}>')

        def handle_endtag(self, tag):
            if tag in _ALLOWED_TAGS and tag not in ('br', 'img'):
                self.out.append(f'</{tag}>')

        def handle_data(self, data):
            self.out.append(
                data.replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))

    p = _P()
    try:
        p.feed(html or '')
    except Exception:
        return Markup(re.sub(r'<[^>]+>', '', html or ''))
    # Markup is safe because every tag and attribute above is allow-listed;
    # returning it here keeps the templates free of `|safe`.
    return Markup(''.join(p.out).strip())


def _is_portal_written(raw_content):
    """Did this timeline entry originate from this portal (either side of
    the glass)? Every message the portal writes carries the product name —
    '— actor (ATM Monitoring System)', 'via ATM Monitoring System',
    'from ATM Monitoring System by …'. Sides therefore mean TEAMS, not
    individuals: right/blue = our ops portal, left/white = vendor techs
    writing directly in GLPI (plus system entries).

    The old test demanded the exact parenthesized signature, so our own
    'Reassigned from … by …' and '(attachment … via …)' lines rendered as
    the other side. Bare-substring covers all portal templates; checked
    against RAW content (cleaned HTML may reformat spacing).
    """
    try:
        return 'ATM Monitoring System' in (raw_content or '')
    except Exception:
        return False


def _subitems(token, ticket_id, sub):
    ok, data = _api(token, 'GET', f'Ticket/{ticket_id}/{sub}/', None)
    if ok and isinstance(data, list):
        return data
    return []


def _subitems_of(token, itemtype, item_id, sub):
    """Sub-items of any parent (followups/solutions link THEIR OWN documents)."""
    ok, data = _api(token, 'GET', f'{itemtype}/{item_id}/{sub}/', None)
    if ok and isinstance(data, list):
        return data
    return []


def _linked_docs(token, itemtype, item_id, limit=10):
    """Documents attached to one ticket/followup/solution/task. Never raises."""
    docs = []
    try:
        for link in _subitems_of(token, itemtype, item_id, 'Document_Item')[:limit]:
            try:
                doc_id = int(link.get('documents_id') or 0)
            except (TypeError, ValueError):
                continue
            if not doc_id or any(d['id'] == doc_id for d in docs):
                continue
            ok, meta = _api(token, 'GET', f'Document/{doc_id}', None)
            if ok and isinstance(meta, dict):
                fname = meta.get('filename') or meta.get('name') or f'file-{doc_id}'
                docs.append({'id': doc_id, 'name': fname,
                             'mime': (meta.get('mime') or ''),
                             'size': meta.get('filesize')})
    except Exception as e:
        logger.warning('attachment listing failed for %s %s: %s', itemtype, item_id, e)
    return docs


def _ticket_item_ids(token, ticket_id):
    """All GLPI item ids belonging to a ticket (for download authorization)."""
    ids = {'Ticket': {int(ticket_id)}, 'ITILFollowup': set(),
           'TicketFollowup': set(), 'ITILSolution': set(), 'TicketTask': set()}
    try:
        for src in ('ITILFollowup', 'TicketFollowup'):
            for f in _subitems(token, ticket_id, src):
                try:
                    ids[src].add(int(f.get('id')))
                except (TypeError, ValueError):
                    pass
        for sub, key in (('ITILSolution', 'ITILSolution'), ('TicketTask', 'TicketTask')):
            for s in _subitems(token, ticket_id, sub):
                try:
                    ids[key].add(int(s.get('id')))
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning('item id listing failed for ticket %s: %s', ticket_id, e)
    return ids


def _fetch_avatar(token, uid, force=False):
    """Download one user picture through the GLPI API and cache the bytes.

    GLPI exposes `GET /apirest.php/user/<id>/picture` (binary; 204 when the
    user has no picture). This works against local and remote GLPI alike
    and needs no shared volume. Never raises; returns (bytes, mime) or None.
    """
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    now = time.time()
    hit = _avatar_cache.get(uid)
    if hit and not force and now - hit[0] < _AVATAR_TTL:
        return hit[1], hit[2]
    try:
        r = requests.get(
            f'{GLPI_URL}/apirest.php/user/{uid}/picture',
            headers={'App-Token': GLPI_APP_TOKEN, 'Session-Token': token},
            timeout=10,
        )
        if r.status_code == 204:
            _avatar_cache[uid] = (now, b'', '')
            return b'', ''
        if r.status_code != 200 or not r.content:
            logger.info('GLPI avatar uid=%s status=%s', uid, r.status_code)
            return None
        if len(r.content) > 5 * 1024 * 1024:
            logger.warning('GLPI avatar uid=%s too large (%s bytes)', uid, len(r.content))
            return None
        mime = (r.headers.get('Content-Type') or '').split(';')[0].strip()
        if not mime.startswith('image/'):
            logger.info('GLPI avatar uid=%s returned non-image %r', uid, mime)
            return None
        _avatar_cache[uid] = (now, r.content, mime)
        return r.content, mime
    except Exception as e:
        logger.warning('GLPI avatar fetch failed for %s: %s', uid, e)
        return None


def _avatar_for(uid):
    """Cached avatar bytes; one API session only on a cache miss."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    hit = _avatar_cache.get(uid)
    if hit and time.time() - hit[0] < _AVATAR_TTL:
        return hit[1], hit[2]
    token = _session()
    if not token:
        return None
    try:
        return _fetch_avatar(token, uid)
    finally:
        _close(token)


def _prefetch_avatar(token, uid):
    """Populate the avatar cache while a detail/list session is already open."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return
    hit = _avatar_cache.get(uid)
    if hit and time.time() - hit[0] < _AVATAR_TTL:
        return
    _fetch_avatar(token, uid)


def _user_contact(token, users_id, cache):
    """Contact card for the ticket thread popover: name + email + phones + picture.

    Cached per request in `cache` (keyed by user id). Anything missing
    stays '' — the template hides empty rows instead of showing blanks.
    Never raises.
    """
    try:
        uid = int(users_id or 0)
    except (TypeError, ValueError):
        return {'name': '—', 'email': '', 'phone': '', 'mobile': '', 'picture': ''}
    if uid in cache:
        return cache[uid]
    card = {'name': f'#{uid}', 'email': '', 'phone': '',
            'mobile': '', 'picture': ''}
    try:
        ok, data = _api(token, 'GET', f'User/{uid}', None)
        if ok and isinstance(data, dict):
            name = ' '.join(p for p in ((data.get('realname') or '').strip(),
                                        (data.get('firstname') or '').strip()) if p)
            card = {
                'name': name or (data.get('name') or f'#{uid}'),
                # Emails live on UserEmail sub-items, NOT on the User object
                # (GLPI supports several per user) — prefer the default one.
                'email': _user_default_email(token, uid),
                'phone': (data.get('phone') or '').strip(),
                'mobile': (data.get('mobile') or '').strip(),
                'picture': (data.get('picture') or '').strip(),
            }
            if card['picture']:
                _prefetch_avatar(token, uid)
    except Exception:
        pass
    cache[uid] = card
    return card


def _user_default_email(token, users_id):
    """Default address from the UserEmail sub-items (may be empty)."""
    try:
        rows = _subitems_of(token, 'User', users_id, 'UserEmail')
        if not isinstance(rows, list) or not rows:
            return ''
        for r in rows:
            try:
                if int(r.get('is_default') or 0) == 1 and r.get('email'):
                    return str(r.get('email')).strip()
            except (TypeError, ValueError):
                continue
        for r in rows:
            if r.get('email'):
                return str(r.get('email')).strip()
    except Exception:
        pass
    return ''


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


def _user_brief(token, users_id, cache):
    """{name, picture} for an assignee chip — one User call, cached.

    Separate from _user_label because the list needs the picture too,
    and from _user_contact because email/phone sub-item calls would be
    wasted on every ticket row.
    """
    try:
        uid = int(users_id or 0)
    except (TypeError, ValueError):
        return {'name': '—', 'picture': ''}
    if uid in cache:
        return cache[uid]
    info = {'name': f'#{uid}', 'picture': ''}
    ok, data = _api(token, 'GET', f'User/{uid}', None)
    if ok and isinstance(data, dict):
        name = ' '.join(p for p in ((data.get('realname') or '').strip(),
                                    (data.get('firstname') or '').strip()) if p)
        info = {'name': name or (data.get('name') or f'#{uid}'),
                'picture': (data.get('picture') or '').strip()}
        if info['picture']:
            _prefetch_avatar(token, uid)
    cache[uid] = info
    return info


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def board():
    status = request.args.get('status', '').strip()
    if status not in ('', 'open', 'pending', 'solved'):
        status = ''
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    error = None
    tickets = []
    counts = {'open': 0, 'pending': 0, 'solved': 0}

    # Exact totals first: they clamp page numbers before the range request
    # (GLPI refuses a start beyond the last row) and keep the KPI cards
    # honest across the whole instance, not just the visible page.
    exact_counts = _ticket_counts()
    counts_scoped = exact_counts is None
    if exact_counts:
        counts = exact_counts
        filter_total = {
            '': sum(exact_counts.values()),
            'open': exact_counts['open'],
            'pending': exact_counts['pending'],
            'solved': exact_counts['solved'],
        }[status]
        total_pages = max(1, -(-filter_total // TICKETS_PER_PAGE))
        page = min(page, total_pages)
    else:
        total_pages = None

    raw, total = _cached_ticket_page(status, page)
    if raw is None and page > 1:
        page = 1
        raw, total = _cached_ticket_page(status, page)
    if raw is None:
        error = ('Cannot reach the GLPI API — check GLPI_APP_TOKEN / GLPI_USER / '
                 'GLPI_API_PASSWORD and that the API client is enabled.')
        raw = []
        total = 0
    if total_pages is None:
        total_pages = max(1, -(-total // TICKETS_PER_PAGE))

    for t in raw:
        try:
            st = int(t.get('status', 0))
        except (TypeError, ValueError):
            st = 0
        if counts_scoped:
            if st in (1, 2, 3):
                counts['open'] += 1
            elif st == 4:
                counts['pending'] += 1
            elif st in (5, 6):
                counts['solved'] += 1
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
            'unread': False,
            'last_by': '',
            '_updater': t.get('users_id_lastupdater'),
        })

    # Unread = GLPI moved since this user last saw the ticket (detail open
    # or own action marks it seen, so only *others'* activity lights up).
    # Shared with /incidents/unread-count so the sidebar number always
    # equals the rows carrying the New badge.
    unread_count = 0
    try:
        me = session.get('username', '')
        _ensure_seen_table()
        unread_ids = _unread_ids(me, raw)
        unread_count = len(unread_ids)
        need_names = set()
        for x in tickets:
            try:
                x['unread'] = int(x['id']) in unread_ids
            except (TypeError, ValueError):
                x['unread'] = False
            if x['unread']:
                try:
                    uid = int(x.pop('_updater') or 0)
                except (TypeError, ValueError):
                    uid = 0
                if uid:
                    need_names.add(uid)
                    x['_uid'] = uid
            else:
                x.pop('_updater', None)
        if need_names:
            token = _session()
            if token:
                try:
                    names = {}
                    for uid in need_names:
                        _user_label(token, uid, names)
                    for x in tickets:
                        uid = x.pop('_uid', None)
                        if x.get('unread') and uid in names:
                            x['last_by'] = names[uid]
                finally:
                    _close(token)
    except Exception as e:
        logger.warning('unread computation failed (non-fatal): %s', e)

    # Assignee = Ticket_User type 2 (technician). One Ticket_User call per
    # ticket + one User call per distinct technician; anything failing
    # degrades to '—' rather than breaking the list.
    try:
        token = _session()
        if token:
            try:
                briefs = {}
                for x in tickets:
                    tech, tech_id, tech_pic = '—', None, ''
                    ok, actors = _api(token, 'GET', f"Ticket/{x['id']}/Ticket_User/", None)
                    if ok and isinstance(actors, list):
                        for a in actors:
                            try:
                                if int(a.get('type', 0)) == 2 and a.get('users_id'):
                                    tech_id = int(a.get('users_id'))
                                    info = _user_brief(token, tech_id, briefs)
                                    tech, tech_pic = info['name'], info['picture']
                                    break
                            except (TypeError, ValueError):
                                continue
                    x['assignee'] = tech
                    x['assignee_id'] = tech_id
                    x['assignee_pic'] = tech_pic
            finally:
                _close(token)
        else:
            for x in tickets:
                x['assignee'] = '—'
                x['assignee_id'] = None
                x['assignee_pic'] = ''
    except Exception as e:
        logger.warning('assignee computation failed (non-fatal): %s', e)
        for x in tickets:
            x.setdefault('assignee', '—')
            x.setdefault('assignee_id', None)
            x.setdefault('assignee_pic', '')

    return render_template('incidents.html', tickets=tickets, counts=counts,
                           f_status=status, error=error, page=page,
                           total=total, total_pages=total_pages,
                           per_page=TICKETS_PER_PAGE,
                           counts_scoped=counts_scoped,
                           unread_count=unread_count)


@bp.route('/unread-count')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def unread_count():
    """Sidebar notification count — same definition as the list's New tags."""
    me = session.get('username', '')
    now = time.time()
    hit = _unread_cache.get(me)
    if hit and now - hit[0] < _UNREAD_TTL:
        return jsonify({'count': hit[1]})
    raw = _cached_tickets()
    if raw is None:
        # Leave the existing badge alone instead of falsely clearing it.
        return jsonify({'count': None, 'error': 'glpi_unreachable'})
    try:
        _ensure_seen_table()
        count = len(_unread_ids(me, raw))
    except Exception as e:
        logger.warning('unread count failed (non-fatal): %s', e)
        return jsonify({'count': None, 'error': 'unread_unavailable'})
    _unread_cache[me] = (now, count)
    return jsonify({'count': count})


@bp.route('/avatar/<int:uid>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def avatar(uid):
    """Proxy a GLPI user picture through the API session.

    GLPI exposes the binary at /apirest.php/user/<id>/picture, so no
    shared volume or browser GLPI session is needed. A missing picture is
    a 404 and the template's initials remain visible underneath.
    """
    blob = _avatar_for(uid)
    if not blob or not blob[0]:
        return '', 404
    content, mime = blob
    resp = Response(content, mimetype=mime or 'image/jpeg')
    # Authenticated content: browser cache may keep it private, never shared.
    resp.headers['Cache-Control'] = 'private, max-age=86400'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@bp.route('/<int:ticket_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def detail(ticket_id):
    error = None
    ticket, timeline, ticket_files = None, [], []
    # ?fresh=1 bypasses the read cache. Needed because the cache is
    # per-process and we run 3 gunicorn workers: the POST invalidates only
    # its own worker, so the auto-reload GET can land on a sibling still
    # holding the pre-reply snapshot (message seemingly missing until the
    # next refresh). Fresh fetch still re-populates the cache.
    hit = None if request.args.get('fresh') == '1' else _get_detail(ticket_id)
    if hit is not None:
        ticket, timeline, ticket_files = hit['ticket'], hit['timeline'], hit['ticket_files']
        token = None
    else:
        token = _session()
    if ticket is None and token is None:
        error = ('Cannot reach the GLPI API — check GLPI_APP_TOKEN / GLPI_USER / '
                 'GLPI_API_PASSWORD and that the API client is enabled.')
    elif ticket is None:
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
                    'content': _clean_html(t.get('content') or '', ticket_id),
                }
                followup_rows = []
                for src in ('ITILFollowup', 'TicketFollowup'):
                    for f in _subitems(token, ticket_id, src):
                        html = _clean_html(f.get('content') or '', ticket_id)
                        followup_rows.append({
                            'kind': 'followup',
                            'fid': f.get('id'),
                            'fsrc': src,
                            'author': _user_label(token, f.get('users_id'), names),
                            'author_id': f.get('users_id'),
                            'date': _fmt_ts(f.get('date_creation') or f.get('date')),
                            'private': bool(int(f.get('is_private') or 0)),
                        'html': html,
                        'mine': _is_portal_written(f.get('content') or ''),
                        'files': [],
                    })
                timeline.extend(followup_rows)
                for s in _subitems(token, ticket_id, 'ITILSolution'):
                    timeline.append({
                        'kind': 'solution',
                        'sid': s.get('id'),
                        'author': _user_label(token, s.get('users_id'), names),
                        'author_id': s.get('users_id'),
                        'date': _fmt_ts(s.get('date_creation') or s.get('date')),
                        'private': False,
                        'html': _clean_html(s.get('content') or '', ticket_id),
                        'mine': _is_portal_written(_clean_html(s.get('content') or '', ticket_id)),
                        'files': [],
                    })
                for k in _subitems(token, ticket_id, 'TicketTask'):
                    timeline.append({
                        'kind': 'task',
                        'fid': None,
                        'author': _user_label(token, k.get('users_id'), names),
                        'author_id': k.get('users_id'),
                        'date': _fmt_ts(k.get('date_creation') or k.get('date')),
                        'private': False,
                        'html': _clean_html(
                            f"<b>{k.get('taskcategories_id') or 'Task'}</b><br>" + (k.get('content') or ''),
                            ticket_id),
                        'mine': _is_portal_written(k.get('content') or ''),
                        'files': [],
                    })
                # ITILFollowup + TicketFollowup endpoints serve the same rows
                # on some GLPI versions — collapse exact duplicates.
                seen, unique = set(), []
                for m in timeline:
                    key = (m['kind'], m['date'], m['author'], m['html'][:120])
                    if key not in seen:
                        seen.add(key)
                        unique.append(m)
                timeline = unique
                timeline.sort(key=lambda m: m['date'])
                # Attachments live on each message itself (followup/solution/task
                # ids — NOT the ticket id), so walk every entry's own links.
                for m in timeline:
                    try:
                        if m['kind'] == 'followup' and m.get('fid') is not None:
                            fid = int(m['fid'])
                            m['files'] = _linked_docs(token, m.get('fsrc') or 'ITILFollowup', fid)
                            if not m['files']:
                                alt = 'TicketFollowup' if (m.get('fsrc') or '') == 'ITILFollowup' else 'ITILFollowup'
                                m['files'] = _linked_docs(token, alt, fid)
                        elif m['kind'] == 'solution' and m.get('sid') is not None:
                            m['files'] = _linked_docs(token, 'ITILSolution', int(m['sid']))
                    except (TypeError, ValueError):
                        continue
                ticket_files = _linked_docs(token, 'Ticket', ticket_id)
                # Contact cards for the thread popover (one User call per
                # distinct author; empty fields stay '' and render hidden).
                _ccache, _seen_uids = {}, set()
                for m in timeline:
                    try:
                        _uid = int(m.get('author_id') or 0)
                    except (TypeError, ValueError):
                        continue
                    if _uid and _uid not in _seen_uids:
                        _seen_uids.add(_uid)
                        _user_contact(token, _uid, _ccache)
                ticket_contacts = dict(_ccache)
        finally:
            _close(token)
    if ticket is not None and hit is None:
        _put_detail(ticket_id, ticket, timeline, ticket_files,
                    ticket_contacts if 'ticket_contacts' in dir() else None)
    if ticket is not None:
        try:
            _mark_seen(session.get('username', ''), ticket_id)
        except Exception:
            pass
    contacts = (hit.get('contacts', {}) if hit
                else (ticket_contacts if 'ticket_contacts' in dir() else {}))
    # PII restraint: phone numbers to operator/admin only. Viewers get
    # names + email + picture. Stripped server-side — never rendered into
    # the page.
    if session.get('role', '') not in ('admin', 'operator'):
        contacts = {str(uid): {'name': c.get('name', '—'), 'email': c.get('email', ''),
                               'phone': '', 'mobile': '',
                               'picture': c.get('picture', '')}
                    for uid, c in contacts.items()}
    else:
        contacts = {str(uid): c for uid, c in contacts.items()}
    return render_template('incidents_detail.html', ticket=ticket,
                           timeline=timeline, ticket_files=ticket_files if ticket else [],
                           contacts=contacts, error=error)


def _safe_filename(raw, doc_id):
    """Strip path tricks, quotes and header newlines from a GLPI filename."""
    name = str(raw or '').split('/')[-1].split('\\')[-1].strip()
    name = re.sub(r'[\r\n";]', '', name)[:100].strip()
    return name or f'file-{doc_id}'


@bp.route('/<int:ticket_id>/attachment/<int:doc_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def attachment(ticket_id, doc_id):
    """Proxy a GLPI document (verifies it belongs to this ticket first)."""
    token = _session()
    if not token:
        return 'GLPI unreachable', 502
    try:
        # Authorize against every item of this ticket (ticket itself +
        # its followups, solutions, tasks) — never serve by bare doc id.
        allowed = set()
        for itemtype, ids in _ticket_item_ids(token, ticket_id).items():
            for item_id in ids:
                for link in _subitems_of(token, itemtype, item_id, 'Document_Item'):
                    try:
                        if int(link.get('documents_id') or 0) == doc_id:
                            allowed.add(doc_id)
                    except (TypeError, ValueError):
                        continue
        if doc_id not in allowed:
            return 'Not found', 404
        ok, meta = _api(token, 'GET', f'Document/{doc_id}', None)
        if not ok or not isinstance(meta, dict):
            return 'Not found', 404
        fname = meta.get('filename') or meta.get('name') or f'file-{doc_id}'
        mime = meta.get('mime') or 'application/octet-stream'
        try:
            r = requests.get(f'{GLPI_URL}/apirest.php/Document/{doc_id}',
                             headers={'App-Token': GLPI_APP_TOKEN,
                                      'Session-Token': token,
                                      'Accept': 'application/octet-stream'},
                             timeout=30, stream=True)
        except Exception as e:
            logger.warning('GLPI attachment download failed: %s', e)
            return 'GLPI unreachable', 502
        if r.status_code != 200:
            return 'Not found', 404
        try:
            declared = int(r.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > _MAX_ATTACH_BYTES:
            r.close()
            return 'File too large to preview', 413
        disp = 'inline' if (mime.startswith('image/') or mime == 'application/pdf') else 'attachment'
        fname = _safe_filename(fname, doc_id)

        def _stream():
            # Streamed, never buffered: the running cap only trips when the
            # server lied about Content-Length (truncates instead of OOMing).
            try:
                downloaded = 0
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > _MAX_ATTACH_BYTES:
                        break
                    yield chunk
            finally:
                r.close()

        return Response(_stream(), mimetype=mime, direct_passthrough=True,
                        headers={'Content-Disposition': f'{disp}; filename="{fname}"'})
    finally:
        _close(token)


@bp.route('/<int:ticket_id>/acknowledge', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def acknowledge(ticket_id):
    actor = _actor()

    def _run(token):
        ok, data = _api(token, 'PUT', f'Ticket/{ticket_id}', {'input': {'status': 2}})
        if not ok:
            return False, data
        # Make "(assigned)" true: whoever acknowledges takes the ticket, if
        # they exist as a GLPI user. Unresolvable actors keep status-only ack.
        assigned_note = ''
        try:
            import urllib.parse
            ok_u, users = _api(token, 'GET',
                               'User/?searchText=' + urllib.parse.quote(actor), None)
            match = None
            if ok_u and isinstance(users, list):
                exact = [u for u in users if (u.get('name') or '') == actor]
                match = exact[0] if exact else None
            if match and match.get('id'):
                ok_a, _ = _api(token, 'POST', 'Ticket_User/', {'input': {
                    'tickets_id': int(ticket_id),
                    'users_id': int(match['id']), 'type': 2}})
                if ok_a:
                    assigned_note = ' (self-assigned)'
        except Exception:
            pass
        _add_followup(token, ticket_id,
                      f'Acknowledged from ATM Monitoring System by {actor}{assigned_note}.')
        return True, f'Ticket {ticket_id} acknowledged{assigned_note}.'

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_ACK', f'Ticket {ticket_id} acknowledged by {actor}')
    return jsonify(res)


@bp.route('/<int:ticket_id>/followup', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def followup(ticket_id):
    actor = _actor()
    if request.files or request.form:
        content = (request.form.get('content') or '').strip()
        uploads = request.files.getlist('files')[:_MAX_REPLY_FILES]
    else:
        content = (request.get_json(silent=True) or {}).get('content', '').strip()
        uploads = []
    if not content and not uploads:
        return jsonify({'success': False, 'message': 'Type a reply or attach a file.'})
    if len(content) > 2000:
        return jsonify({'success': False, 'message': 'Note is too long (max 2000 characters).'})

    def _run(token):
        ok, data, used = _add_followup_ex(
            token, ticket_id, f'{content}\n— {actor} (ATM Monitoring System)' if content else
            f'(attachment from {actor} via ATM Monitoring System)')
        if not ok:
            return False, data
        fid = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            fid = data[0].get('id')
        elif isinstance(data, dict):
            fid = data.get('id')
        failed = []
        attached = 0
        if fid and uploads:
            for st in uploads:
                if not (st and st.filename):
                    continue
                ok_f, msg_f = _upload_followup_file(token, used, fid, st)
                if ok_f:
                    attached += 1
                else:
                    failed.append(msg_f)
        msg = f'Note added to ticket {ticket_id}.'
        if attached:
            msg = f'Note + {attached} file(s) added to ticket {ticket_id}.'
        elif uploads and not fid:
            failed.append('files: followup id unknown')
        if failed:
            msg += ' Issues: ' + '; '.join(failed)
        return True, msg

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
        # True reassignment: release existing technicians first so the ticket
        # never carries a stale assignee (e.g. someone gone unavailable).
        # History is untouched — GLPI keeps the full timeline regardless.
        ok, actors = _api(token, 'GET', f'Ticket/{ticket_id}/Ticket_User/', None)
        if ok and isinstance(actors, list):
            for a in actors:
                try:
                    if int(a.get('type', 0)) == 2 and a.get('id'):
                        if int(a.get('users_id', 0)) == users_id:
                            return True, f'Ticket {ticket_id} is already assigned to this technician.'
                        _api(token, 'DELETE', f"Ticket_User/{a['id']}", None)
                except (TypeError, ValueError):
                    continue
        ok, data = _api(token, 'POST', 'Ticket_User/', {'input': {
            'tickets_id': ticket_id, 'users_id': users_id, 'type': 2}})
        if not ok:
            return False, data
        _api(token, 'PUT', f'Ticket/{ticket_id}', {'input': {'status': 2}})
        _add_followup(token, ticket_id,
                      f'Reassigned from ATM Monitoring System by {actor} (GLPI user id {users_id}).')
        return True, f'Ticket {ticket_id} reassigned.'

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
            'itemtype': 'Ticket', 'items_id': ticket_id, 'content': f'{solution}\n— {actor} (ATM Monitoring System)'}})
        if ok:
            return True, f'Ticket {ticket_id} solved.'
        # Fallback for older GLPI: direct status change.
        ok2, data2 = _api(token, 'PUT', f'Ticket/{ticket_id}', {'input': {'status': 5}})
        if ok2:
            _add_followup(token, ticket_id, f'Marked solved from ATM Monitoring System by {actor}: {solution}')
            return True, f'Ticket {ticket_id} solved.'
        return False, data

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_SOLVE', f'Ticket {ticket_id} solved by {actor}')
    return jsonify(res)


def _ticket_status(token, ticket_id):
    """Current GLPI status, or None if the ticket cannot be read."""
    ok, data = _api(token, 'GET', f'Ticket/{ticket_id}', None)
    if not ok or not isinstance(data, dict):
        return None
    try:
        return int(data.get('status'))
    except (TypeError, ValueError):
        return None


def _has_technician(token, ticket_id):
    """True if the ticket has an assigned technician (Ticket_User type 2)."""
    ok, actors = _api(token, 'GET', f'Ticket/{ticket_id}/Ticket_User/', None)
    if not ok or not isinstance(actors, list):
        return False
    for a in actors:
        try:
            if int(a.get('type', 0)) == 2:
                return True
        except (TypeError, ValueError):
            continue
    return False


@bp.route('/<int:ticket_id>/close', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def close(ticket_id):
    """Solved -> Closed. Only from Solved: closing open work hides it."""
    actor = _actor()

    def _run(token):
        status = _ticket_status(token, ticket_id)
        if status is None:
            return False, f'Cannot read ticket {ticket_id} from GLPI.'
        ok, new_status, _code = _decide_status('close', status)
        if not ok:
            return False, ('Only a solved ticket can be closed '
                           f'(current status: {status_label(status)}).')
        if new_status is None:
            return True, f'Ticket {ticket_id} is already closed.'
        ok, data = _api(token, 'PUT', f'Ticket/{ticket_id}',
                        {'input': {'status': new_status}})
        if not ok:
            return False, data
        _add_followup(token, ticket_id,
                      f'Closed from ATM Monitoring System by {actor}.')
        return True, f'Ticket {ticket_id} closed.'

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_CLOSE', f'Ticket {ticket_id} closed by {actor}')
    return jsonify(res)


@bp.route('/<int:ticket_id>/reopen', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def reopen(ticket_id):
    """Solved/Closed -> open, or Pending -> open (resume)."""
    actor = _actor()
    reason = (request.get_json(silent=True) or {}).get('reason', '').strip()
    if len(reason) < 5:
        return jsonify({'success': False,
                        'message': 'Write why the ticket is being reopened (at least 5 characters).'})
    if len(reason) > 2000:
        return jsonify({'success': False, 'message': 'Reason is too long (max 2000 characters).'})

    def _run(token):
        status = _ticket_status(token, ticket_id)
        if status is None:
            return False, f'Cannot read ticket {ticket_id} from GLPI.'
        # Only ask GLPI for the assignee when a write is actually needed and
        # the target status depends on it.
        assigned = _has_technician(token, ticket_id)
        ok, new_status, code = _decide_status('reopen', status, assigned=assigned)
        if not ok:
            return False, f'Cannot reopen ticket {ticket_id} (status: {status_label(status)}).'
        if new_status is None:
            return True, f'Ticket {ticket_id} is already open.'
        ok, data = _api(token, 'PUT', f'Ticket/{ticket_id}',
                        {'input': {'status': new_status}})
        if not ok:
            return False, data
        verb = 'Resumed from pending' if code == 'resumed' else 'Reopened'
        _add_followup(token, ticket_id,
                      f'{verb} from ATM Monitoring System by {actor}: {reason}')
        return True, (f'Ticket {ticket_id} resumed.' if code == 'resumed'
                      else f'Ticket {ticket_id} reopened.')

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_REOPEN', f'Ticket {ticket_id} reopened by {actor}: {reason}')
    return jsonify(res)


@bp.route('/<int:ticket_id>/pending', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def pending(ticket_id):
    """Open -> Pending, with the reason recorded as a followup."""
    actor = _actor()
    reason = (request.get_json(silent=True) or {}).get('reason', '').strip()
    if len(reason) < 5:
        return jsonify({'success': False,
                        'message': 'Write what the ticket is waiting for (at least 5 characters).'})
    if len(reason) > 2000:
        return jsonify({'success': False, 'message': 'Reason is too long (max 2000 characters).'})

    def _run(token):
        status = _ticket_status(token, ticket_id)
        if status is None:
            return False, f'Cannot read ticket {ticket_id} from GLPI.'
        ok, new_status, _code = _decide_status('pending', status)
        if not ok:
            return False, ('Only an open ticket can be set to pending '
                           f'(current status: {status_label(status)}).')
        if new_status is None:
            return True, f'Ticket {ticket_id} is already pending.'
        ok, data = _api(token, 'PUT', f'Ticket/{ticket_id}',
                        {'input': {'status': new_status}})
        if not ok:
            return False, data
        _add_followup(token, ticket_id,
                      f'Set to pending from ATM Monitoring System by {actor}: {reason}')
        return True, f'Ticket {ticket_id} set to pending.'

    res = _mutate(ticket_id, _run)
    if res['success']:
        log_action('TICKET_PENDING', f'Ticket {ticket_id} set pending by {actor}: {reason}')
    return jsonify(res)
