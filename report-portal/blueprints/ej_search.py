"""
blueprints/ej_search.py
Electronic Journal search routes — search UI and CSV export.
"""
import csv, io, os, re, logging
import requests
from flask import Blueprint, render_template, request, Response
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from db import get_db
from audit import log_action
from ej_text import humanize_ndc_text
from net_url import service_url
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger(__name__)

bp = Blueprint('ej_search', __name__)

ES_HOST  = os.environ.get('ES_HOST', 'opensearch:9200')
ES_INDEX = os.environ.get('ES_INDEX', 'atm-ej-live-*,atm-electronic-journal')

_TID_CACHE = {'at': 0.0, 'map': {}}
_HOST_CACHE = {'at': 0.0, 'map': {}}


def _terminal_map(ttl=300):
    """atm_id -> terminal_id from the registry, cached.

    The journal never carries the TID (proven: every NDC record lacks it),
    so the portal fills it from atm_locations instead of showing blank.
    Fail-closed (empty map) — a DB hiccup must not break search.
    """
    import time
    now = time.time()
    if now - _TID_CACHE['at'] < ttl:
        return _TID_CACHE['map']
    mapping = {}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id, terminal_id FROM atm_locations")
            mapping = {a: (t or '') for a, t in cur.fetchall()}
            cur.close()
    except Exception as e:
        logger.warning('terminal map refresh failed: %s', e)
    _TID_CACHE.update(at=now, map=mapping)
    return mapping


def _hostname_map(ttl=300):
    """ej_hostname -> atm_id, cached. Used when the pipeline did not
    populate atm.id (GRG documents were arriving with only the filename)."""
    import time
    now = time.time()
    if now - _HOST_CACHE['at'] < ttl:
        return _HOST_CACHE['map']
    mapping = {}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT ej_hostname, atm_id FROM atm_locations "
                        "WHERE COALESCE(ej_hostname,'') <> ''")
            mapping = {str(h).strip().upper(): a for h, a in cur.fetchall()}
            cur.close()
    except Exception as e:
        logger.warning('hostname map refresh failed: %s', e)
    _HOST_CACHE.update(at=now, map=mapping)
    return mapping


_GRG_TS = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}')
_GRG_MARKERS = ('TRANSACTION START', 'TRANSACTION END', 'CARD INSERTED',
                'RFCARD INSERTED', 'PIN ENTERED', 'DISPENSE', 'WITHDRAWAL',
                'BALANCE', 'FAULT', 'CASSETTE', 'CABINET DOOR')


def _atm_from_source(src):
    """Best-effort ATM identity for reachable raw EJ docs.

    The OpenSearch pipeline only maps a couple of test hostnames today; the
    canonical pulled filename is still a reliable identity because it is
    <date>_<HOST>_EJDATA.log. Use the registry, never the filename as an ATM id.
    """
    atm = src.get('atm') if isinstance(src.get('atm'), dict) else {}
    if atm.get('id'):
        return atm.get('id')
    candidates = []
    log = src.get('log') if isinstance(src.get('log'), dict) else {}
    fileinfo = log.get('file') if isinstance(log.get('file'), dict) else {}
    if fileinfo.get('path'):
        candidates.append(fileinfo['path'])
    if atm.get('host'):
        candidates.append(atm['host'])
    for value in candidates:
        match = re.search(r'([0-9]{4}-[0-9]{2}-[0-9]{2})_([^/]+)_EJDATA\.log$',
                          str(value))
        if match:
            mapped = _hostname_map().get(match.group(2).upper())
            if mapped:
                return mapped
    return None


def _raw_event_fields(src):
    """Give unparsed NCR/GRG verbose events a displayable shape."""
    msg = src.get('message') or ''
    if not msg:
        return src
    upper = msg.upper()
    stamp = _GRG_TS.match(msg)
    existing = src.get('event_type')
    if stamp:
        src.setdefault('event_time', stamp.group(0))
    if any(marker in upper for marker in _GRG_MARKERS):
        wanted = 'TXN' if any(
            k in upper for k in ('TRANSACTION', 'CARD INSERTED', 'PIN ENTERED',
                                 'DISPENSE', 'WITHDRAWAL')) else 'STATE'
        if not existing or (wanted == 'TXN' and existing == 'STATE'):
            src['event_type'] = wanted
    elif stamp and not existing:
        src['event_type'] = 'STATE'
    match = re.search(r'\b(APPROVED|DECLINED|TIMEOUT|ERROR)\b', upper)
    if match:
        src.setdefault('status', match.group(1))
    return src


def _scrub_pan(text):
    """Mask PAN-like digit runs in free text (first6/last4, C12).

    EJ raw messages may carry a full PAN; card columns arriving from older
    pipeline generations may too. A 12–19 digit run becomes
    123456******1234 so bulk exports can't leak full card numbers."""
    if not text:
        return text
    def _mask(m):
        d = m.group(0)
        if len(d) < 12:
            return d
        return d[:6] + '******' + d[-4:]
    return re.sub(r'\d{12,19}', _mask, str(text))


def _audit_filters(card, auth_code):
    """Presence markers for the audit log — never the values (C12)."""
    return ('set' if card else 'unset'), ('set' if auth_code else 'unset')


def _parse_ej_log_line(src):
    """
    Normalize a hit's _source into the flat display fields used by routes.

    Handles all three schema generations:
      1. Nested (current):  atm.id, terminal.id, transaction.*, card.masked,
                            fault.*, event.category  (atm_ej_parser v2)
      2. Flat pipeline:     atm_id, terminal_id, sub_type, status, amount ...
                            (atm_ej_parser v1 — older daily indices)
      3. Raw message only:  pipe-delimited line parsed client-side
    """
    # ── Generation 1: nested schema ──
    atm_obj = src.get('atm') if isinstance(src.get('atm'), dict) else {}
    if not atm_obj.get('id'):
        derived_atm = _atm_from_source(src)
        if derived_atm:
            src['atm_id'] = derived_atm
    if atm_obj.get('id'):
        txn   = src.get('transaction') if isinstance(src.get('transaction'), dict) else {}
        card  = src.get('card')        if isinstance(src.get('card'), dict) else {}
        fault = src.get('fault')       if isinstance(src.get('fault'), dict) else {}
        ev    = src.get('event')       if isinstance(src.get('event'), dict) else {}
        term  = src.get('terminal')    if isinstance(src.get('terminal'), dict) else {}
        src['atm_id']      = atm_obj.get('id')
        src['terminal_id'] = term.get('id') or \
            _terminal_map().get(atm_obj.get('id'), '')
        src['event_type']  = ev.get('category')
        src['sub_type']    = txn.get('type')
        comp = src.get('component') if isinstance(src.get('component'), dict) else {}
        if comp.get('name') and not src['sub_type']:
            src['sub_type'] = comp.get('name')
        src['seq']         = txn.get('sequence')
        src['card_masked'] = card.get('masked')
        src['status']      = txn.get('status')
        try:
            src['amount'] = float(txn.get('amount')) \
                if txn.get('amount') not in (None, '') else None
        except (TypeError, ValueError):
            src['amount'] = None
        src['currency']    = txn.get('currency')
        src['auth_code']   = txn.get('auth_code')
        src['error_code']  = fault.get('code')
        return _raw_event_fields(src)

    # ── Generation 2: flat pipeline fields already present ──
    if src.get('atm_id'):
        return _raw_event_fields(src)

    # ── Generation 3: parse raw message ──
    msg = src.get('message', '')
    if not msg:
        return src
    if '|' not in msg:
        return _raw_event_fields(src)
    parts = [p.strip() for p in msg.split('|')]
    if len(parts) < 4:
        return _raw_event_fields(src)
    is_grg = any(p.upper().startswith('VENDOR=') for p in parts)
    src['atm_id'] = parts[1]
    if is_grg:
        # GRG: ts | ATM | TID | VENDOR=GRG | TXN_CODE=.. (or EVENT=FAULT) | KV...
        src['terminal_id'] = parts[2]
        src['event_type']  = 'TXN'        # overridden below if EVENT=FAULT
        kv_start = 3
    elif parts[2].startswith('TID'):
        src['terminal_id'] = parts[2]
        src['event_type']  = parts[3]
        src['sub_type']    = parts[4]
        kv_start = 5
    else:
        src['event_type']  = parts[2]
        src['sub_type']    = parts[3]
        kv_start = 4
    for part in parts[kv_start:]:
        if '=' not in part:
            continue
        key, _, val = part.partition('=')
        key = key.strip().upper()
        val = val.strip()
        if   key == 'CARD':     src['card_masked'] = val
        elif key == 'ACCT_NO':  src['card_masked'] = val
        elif key == 'STATUS':   src['status'] = val
        elif key == 'RESP_CODE':src['status'] = val
        elif key == 'TXN_CODE': src['sub_type'] = val
        elif key == 'EVENT':    src['event_type'] = val
        elif key == 'VENDOR':   src['vendor'] = val
        elif key == 'FAULT_CODE': src['error_code'] = val
        elif key == 'SEQ':      src['seq'] = val
        elif key == 'AMOUNT':
            try:    src['amount'] = float(val)
            except: pass
        elif key == 'CURRENCY': src['currency'] = val
        elif key == 'AUTH':     src['auth_code'] = val
        elif key == 'CASSETTE': src['cassette_id'] = val
        elif key == 'TECH_CODE':src['error_code'] = val
        elif key == 'OPERATOR': src['operator_id'] = val
    return src


def _is_junk_hit(src):
    """Drop non-EJ noise (empty datagrams, binary garbage on :514).

    Stray UDP packets to Filebeat :514 index as docs with blank or binary
    messages; they render as all-dash rows. A displayable EJ row needs an
    ATM identity or a pipe-delimited line, so anything with neither is
    skipped. Real NCR/GRG journal lines always contain '|'.
    """
    if src.get('atm_id'):
        return False
    msg = src.get('message') or ''
    if '|' in msg:
        return False
    if _GRG_TS.match(msg) or '[020t' in msg:
        return False
    if any(marker in msg.upper() for marker in _GRG_MARKERS):
        return False
    return True


def _clean_contains(value, limit=64):
    """Strip ES wildcard specials from a contains-search input.

    A bare `*` (or `***`) would otherwise compile to a leading-wildcard
    full-index scan; stripping specials turns it into an empty filter
    (clause skipped) instead of a cluster-wide DoS.
    """
    cleaned = re.sub(r'[*?%]', '', str(value or '')).strip()
    return cleaned[:limit]


def _parse_amount(value):
    """Return (number|None, error|None) — invalid amounts must surface,
    never silently unfilter a search the user believes is bounded."""
    if not value:
        return None, None
    try:
        return float(value), None
    except (ValueError, TypeError):
        return None, f'Invalid amount {value!r}: enter a number.'


def _valid_ej_date(value):
    """YYYY-MM-DD or None if malformed (surfaces instead of an OS 400)."""
    try:
        date.fromisoformat((value or '').strip())
        return True
    except (ValueError, TypeError):
        return False


def _build_must(keyword, card, date_from, date_to, atm_id,
                event_type, status, min_amount, max_amount, auth_code):
    """
    Build the bool.must list. Every field filter targets BOTH schema
    generations (nested v2 fields and flat v1 fields) plus a raw-message
    fallback, because atm-ej-live-* spans daily indices written before
    and after the structured-schema cutover.

    Returns (must, amount_error).
    """
    must = []
    amount_error = None
    if keyword:
        must.append({"match": {"message": {"query": keyword}}})
    card = _clean_contains(card)
    if card:
        card_val = f"*{card}*"
        must.append({"bool": {"should": [
            {"wildcard": {"card.masked": {"value": card_val}}},
            {"wildcard": {"card_masked": {"value": card_val}}},
            {"wildcard": {"message":     {"value": card_val}}},
        ], "minimum_should_match": 1}})
    if date_from:
        must.append({"range": {"@timestamp": {"gte": date_from}}})
    if date_to:
        must.append({"range": {"@timestamp": {"lte": f"{date_to}T23:59:59"}}})
    if atm_id:
        must.append({"bool": {"should": [
            {"term":         {"atm.id": atm_id}},
            {"term":         {"atm_id": atm_id}},
            {"match_phrase": {"message": atm_id}},
        ], "minimum_should_match": 1}})
    if event_type:
        must.append({"bool": {"should": [
            {"term":         {"event.category": event_type}},
            {"term":         {"event_type":      event_type}},
            {"match_phrase": {"message":         event_type}},
        ], "minimum_should_match": 1}})
    if status:
        must.append({"bool": {"should": [
            {"term":         {"transaction.status": status}},
            {"term":         {"status.keyword":     status}},
            {"match_phrase": {"message":            status}},
        ], "minimum_should_match": 1}})
    if min_amount or max_amount:
        amt_range = {}
        lo, lo_err = _parse_amount(min_amount)
        hi, hi_err = _parse_amount(max_amount)
        amount_error = lo_err or hi_err
        if lo is not None:
            amt_range["gte"] = lo
        if hi is not None:
            amt_range["lte"] = hi
        if amt_range:
            must.append({"bool": {"should": [
                {"range": {"transaction.amount": amt_range}},
                {"range": {"amount":             amt_range}},
            ], "minimum_should_match": 1}})
    auth_code = _clean_contains(auth_code)
    if auth_code:
        must.append({"bool": {"should": [
            {"term":      {"transaction.auth_code": auth_code}},
            {"term":      {"auth_code.keyword":     auth_code}},
            {"wildcard":  {"message":               {"value": f"*{auth_code}*"}}},
        ], "minimum_should_match": 1}})
    return must, amount_error


EAT = timezone(timedelta(hours=3))

def _display_ts(src):
    """Prefer the journal's own clock over filebeat's ingest time."""
    raw = src.get('event_time')
    if raw:
        match = re.match(r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})',
                         str(raw))
        if match:
            return f'{match.group(1)} {match.group(2)}'
    return _fmt_ts(src.get('@timestamp', ''))


def _fmt_ts(ts):
    if ts and 'T' in ts:
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(EAT).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    return ts


@bp.route('/ej-search', methods=['GET', 'POST'])
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def ej_search():
    # Defaults
    keyword = card = atm_id = event_type = status = min_amount = max_amount = auth_code = ''
    date_from = (date.today() - timedelta(days=30)).isoformat()
    date_to   = date.today().isoformat()
    page      = 1
    per_page  = 50
    results   = []
    total     = 0
    total_pages = 1
    error     = None
    searched  = False

    if request.method == 'POST':
        searched    = True
        keyword     = request.form.get('keyword', '').strip()
        card        = request.form.get('card', '').strip()
        date_from   = request.form.get('date_from', (date.today() - timedelta(days=30)).isoformat()) or (date.today() - timedelta(days=30)).isoformat()
        date_to     = request.form.get('date_to', date.today().isoformat()) or date.today().isoformat()
        atm_id      = request.form.get('atm_id', '')
        event_type  = request.form.get('event_type', '')
        status      = request.form.get('status', '')
        min_amount  = request.form.get('min_amount', '').strip()
        max_amount  = request.form.get('max_amount', '').strip()
        auth_code   = request.form.get('auth_code', '').strip()
        try:
            page = max(1, int(request.form.get('page', 1)))
        except (ValueError, TypeError):
            page = 1

        must, amount_error = _build_must(keyword, card, date_from, date_to, atm_id,
                                           event_type, status, min_amount, max_amount, auth_code)

        if not _valid_ej_date(date_from) or not _valid_ej_date(date_to):
            error = 'Invalid date(s): use YYYY-MM-DD.'
        elif amount_error:
            error = amount_error
        elif not must:
            error = "Please provide at least one search filter."
        else:
            skipped = 0
            try:
                resp = requests.get(
                    service_url(ES_HOST,
                                f"/{ES_INDEX}/_search?ignore_unavailable=true",
                                'ES_SCHEME'),
                    json={
                        "query": {"bool": {"must": must}},
                        "size": per_page,
                        "from": (page - 1) * per_page,
                        "sort": [{"@timestamp": "desc"}],
                        "track_total_hits": True,
                    },
                    timeout=20,
                )
                if resp.status_code == 200:
                    body  = resp.json()
                    total = body.get('hits', {}).get('total', {}).get('value', 0)
                    skipped = 0
                    for hit in body.get('hits', {}).get('hits', []):
                        src = _parse_ej_log_line(hit['_source'])
                        if _is_junk_hit(src):
                            skipped += 1
                            continue
                        results.append({
                            'timestamp':   _display_ts(src),
                            'atm_id':      src.get('atm_id', ''),
                            'txn_type':    src.get('sub_type', src.get('event_type', '')),
                            'card':        src.get('card_masked', ''),
                            'status':      src.get('status', ''),
                            'amount':      src.get('amount', ''),
                            'message':     src.get('message', ''),
                            'human':       humanize_ndc_text(src.get('message', '')),                            'terminal_id': src.get('terminal_id', ''),
                            'auth_code':   src.get('auth_code', ''),
                            'error_code':  src.get('error_code', ''),
                            'cassette_id': src.get('cassette_id', ''),
                            'operator_id': src.get('operator_id', ''),
                            'event_type':  src.get('event_type', ''),
                            'sub_type':    src.get('sub_type', ''),
                            'currency':    src.get('currency', ''),
                        })
                else:
                    error = f"OpenSearch returned status {resp.status_code}"
                    logger.warning('OS search failed: status=%s query=%s', resp.status_code, keyword or '(no keyword)')
                # Junk datagrams are hidden from the listing, so the header
                # count must exclude them too (else "N Results" + empty table).
                if not error:
                    total = max(0, total - skipped)
            except requests.exceptions.ConnectionError:
                error = f"Cannot connect to OpenSearch at {ES_HOST}"
                logger.error('OS connection error: host=%s', ES_HOST)
            except Exception as e:
                error = f"Search error: {e}"
                logger.exception('OS search exception')

        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1

        # Audit log for EJ search (filter values never logged — C12)
        if searched and not error:
            _card, _auth = _audit_filters(card, auth_code)
            log_action('EJ_SEARCH', f'keyword={keyword}, card={_card}, atm={atm_id}, event={event_type}, status={status}, amount={min_amount}-{max_amount}, auth={_auth}, results={total}')

    # ATM dropdown from database
    db_atms = []
    try:
        with get_db() as conn:
            cur  = conn.cursor()
            cur.execute("SELECT atm_id, branch FROM atm_locations ORDER BY atm_id")
            db_atms = cur.fetchall()
            cur.close()
    except Exception as e:
        logger.warning('Failed to load ATMs for EJ search: %s', e)
        error = f'Failed to load ATM list: {e}' if not error else error

    return render_template(
        'ej_search.html',
        searched=searched,
        keyword=keyword, card=card,
        date_from=date_from, date_to=date_to,
        atm_id=atm_id, event_type=event_type, status=status,
        min_amount=min_amount, max_amount=max_amount, auth_code=auth_code,
        db_atms=db_atms,
        results=list(enumerate(results)),   # pass (idx, row) pairs for template
        total=total,
        page=page,
        total_pages=total_pages,
        error=error,
    )


@bp.route('/ej-search/csv')
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def ej_search_csv():
    card        = request.args.get('card', '')
    date_from   = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat()) or (date.today() - timedelta(days=30)).isoformat()
    date_to     = request.args.get('date_to', date.today().isoformat()) or date.today().isoformat()
    atm_id      = request.args.get('atm_id', '')
    keyword     = request.args.get('keyword', '')
    event_type  = request.args.get('event_type', '')
    status      = request.args.get('status', '')
    min_amount  = request.args.get('min_amount', '')
    max_amount  = request.args.get('max_amount', '')
    auth_code   = request.args.get('auth_code', '')

    selected = request.args.get('selected', '').strip()
    selected_set = set()
    if selected:
        for s in selected.split(','):
            s = s.strip()
            if s.isdigit():
                selected_set.add(int(s))

    must, amount_error = _build_must(keyword, card, date_from, date_to, atm_id,
                                       event_type, status, min_amount, max_amount, auth_code)
    date_error = None
    if not _valid_ej_date(date_from) or not _valid_ej_date(date_to):
        date_error = 'Invalid date(s): use YYYY-MM-DD.'
    rows = []
    if amount_error or date_error:
        rows = []
    elif must:
        try:
            resp = requests.get(
                service_url(ES_HOST,
                            f"/{ES_INDEX}/_search?ignore_unavailable=true",
                            'ES_SCHEME'),
                json={"query": {"bool": {"must": must}}, "size": 5000, "sort": [{"@timestamp": "desc"}]},
                timeout=30,
            )
            if resp.status_code == 200:
                for idx, hit in enumerate(resp.json().get('hits', {}).get('hits', [])):
                    if selected_set and idx not in selected_set:
                        continue
                    src = _parse_ej_log_line(hit['_source'])
                    if _is_junk_hit(src):
                        continue
                    rows.append([
                        _display_ts(src),
                        src.get('atm_id', ''),
                        src.get('sub_type', src.get('event_type', '')),
                        _scrub_pan(src.get('card_masked', '')),
                        src.get('status', ''),
                        src.get('amount', ''),
                        _scrub_pan(src.get('message', '')),
                    ])
        except Exception:
            pass

    buf = io.StringIO()
    w   = csv.writer(buf)
    if amount_error:
        w.writerow([f'ERROR: {amount_error} Export cancelled — fix the amount and retry.'])
    elif date_error:
        w.writerow([f'ERROR: {date_error} Export cancelled.'])
    elif not rows and not must:
        w.writerow(['ERROR: No search filters provided. Export cancelled.'])
    elif not rows:
        w.writerow(['ERROR: OpenSearch query failed or returned no data. Check that OpenSearch is running and try again.'])
    w.writerow(['Timestamp', 'ATM ID', 'Transaction Type', 'Card (masked)', 'Status', 'Amount', 'Raw Log'])
    w.writerows(rows)
    buf.seek(0)

    # Audit log for EJ CSV export (filter values never logged — C12)
    _card, _auth = _audit_filters(card, auth_code)
    log_action('EJ_EXPORT_CSV', f'keyword={keyword}, card={_card}, atm={atm_id}, event={event_type}, status={status}, amount={min_amount}-{max_amount}, auth={_auth}, rows={len(rows)}')

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=ej_search_{date.today().isoformat()}.csv'}
    )
