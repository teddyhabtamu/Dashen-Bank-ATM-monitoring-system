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
from flask import Blueprint, render_template, request, jsonify, session, Response
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from audit import log_action
from db import get_db

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
    except Exception as e:
        logger.warning('ticket_seen write failed (non-fatal): %s', e)


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
                href = (attrs.get('href') or '#').replace('"', '')
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
                'unread': False,
                'last_by': '',
                '_mod': t.get('date_mod'),
                '_updater': t.get('users_id_lastupdater'),
            })

    # Unread = GLPI moved since this user last saw the ticket (detail open
    # or own action marks it seen, so only *others'* activity lights up).
    try:
        me = session.get('username', '')
        _ensure_seen_table()
        seen = _seen_map(me, [x['id'] for x in tickets])
        need_names = set()
        for x in tickets:
            mod = _parse_utc(x.pop('_mod'))
            if mod and (x['id'] not in seen or
                        (_as_utc(seen[x['id']]) is not None and mod > _as_utc(seen[x['id']]))):
                x['unread'] = True
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

    return render_template('incidents.html', tickets=tickets, counts=counts,
                           f_status=status, error=error)


@bp.route('/<int:ticket_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def detail(ticket_id):
    error = None
    ticket, timeline, ticket_files = None, [], []
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
                            'date': _fmt_ts(f.get('date_creation') or f.get('date')),
                            'private': bool(int(f.get('is_private') or 0)),
                            'html': html,
                            'mine': '(Report Portal)' in html,
                            'files': [],
                        })
                timeline.extend(followup_rows)
                for s in _subitems(token, ticket_id, 'ITILSolution'):
                    timeline.append({
                        'kind': 'solution',
                        'sid': s.get('id'),
                        'author': _user_label(token, s.get('users_id'), names),
                        'date': _fmt_ts(s.get('date_creation') or s.get('date')),
                        'private': False,
                        'html': _clean_html(s.get('content') or '', ticket_id),
                        'mine': False,
                        'files': [],
                    })
                for k in _subitems(token, ticket_id, 'TicketTask'):
                    timeline.append({
                        'kind': 'task',
                        'fid': None,
                        'author': _user_label(token, k.get('users_id'), names),
                        'date': _fmt_ts(k.get('date_creation') or k.get('date')),
                        'private': False,
                        'html': _clean_html(
                            f"<b>{k.get('taskcategories_id') or 'Task'}</b><br>" + (k.get('content') or ''),
                            ticket_id),
                        'mine': False,
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
        finally:
            _close(token)
    if ticket is not None:
        try:
            _mark_seen(session.get('username', ''), ticket_id)
        except Exception:
            pass
    return render_template('incidents_detail.html', ticket=ticket,
                           timeline=timeline, ticket_files=ticket_files if ticket else [],
                           error=error)


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
        body = b''.join(chunk for chunk in r.iter_content(65536)
                        if chunk)[:_MAX_ATTACH_BYTES + 1]
        if len(body) > _MAX_ATTACH_BYTES:
            return 'File too large to preview', 413
        disp = 'inline' if (mime.startswith('image/') or mime == 'application/pdf') else 'attachment'
        return Response(body, mimetype=mime,
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
