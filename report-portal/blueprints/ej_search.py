"""
blueprints/ej_search.py
Electronic Journal search routes — search UI and CSV export.
"""
import csv, io
import requests
from flask import Blueprint, render_template, request, Response
from db import get_db
from datetime import datetime, date

bp = Blueprint('ej_search', __name__)

import os
ES_HOST  = os.environ.get('ES_HOST', 'opensearch:9200')
ES_INDEX = os.environ.get('ES_INDEX', 'atm-ej-live-*,atm-electronic-journal')


def _parse_ej_log_line(src):
    """
    Filebeat ships raw log lines as plain text in src['message'].
    When structured fields are absent, parse the message pipe-delimited string.
    Two formats exist:
      4-field: ts | ATM-ID | EVENT_TYPE | SUB_TYPE | KV...
      5-field: ts | ATM-ID | TID | EVENT_TYPE | SUB_TYPE | KV...
    """
    if src.get('atm_id'):
        return src
    msg = src.get('message', '')
    if not msg or '|' not in msg:
        return src
    parts = [p.strip() for p in msg.split('|')]
    if len(parts) < 4:
        return src
    src['atm_id'] = parts[1]
    if parts[2].startswith('TID'):
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
        elif key == 'STATUS':   src['status'] = val
        elif key == 'AMOUNT':
            try:    src['amount'] = float(val)
            except: pass
        elif key == 'CURRENCY': src['currency'] = val
        elif key == 'AUTH':     src['auth_code'] = val
        elif key == 'CASSETTE': src['cassette_id'] = val
        elif key == 'TECH_CODE':src['error_code'] = val
        elif key == 'OPERATOR': src['operator_id'] = val
    return src


def _build_must(keyword, card, date_from, date_to, atm_id,
                event_type, status, min_amount, max_amount, auth_code):
    must = []
    if keyword:
        must.append({"match": {"message": {"query": keyword}}})
    if card:
        must.append({"wildcard": {"message": {"value": f"*{card}*"}}})
    if date_from:
        must.append({"range": {"@timestamp": {"gte": date_from}}})
    if date_to:
        must.append({"range": {"@timestamp": {"lte": f"{date_to}T23:59:59"}}})
    if atm_id:
        must.append({"bool": {"should": [
            {"term":         {"atm_id.keyword": atm_id}},
            {"match_phrase": {"message":        atm_id}},
        ], "minimum_should_match": 1}})
    if event_type:
        must.append({"bool": {"should": [
            {"term":         {"event_type.keyword": event_type}},
            {"match_phrase": {"message":            event_type}},
        ], "minimum_should_match": 1}})
    if status:
        must.append({"bool": {"should": [
            {"term":         {"status.keyword": status}},
            {"match_phrase": {"message":        status}},
        ], "minimum_should_match": 1}})
    if min_amount or max_amount:
        amt_range = {}
        if min_amount:
            try:    amt_range["gte"] = float(min_amount)
            except: pass
        if max_amount:
            try:    amt_range["lte"] = float(max_amount)
            except: pass
        if amt_range:
            must.append({"range": {"amount": amt_range}})
    if auth_code:
        must.append({"bool": {"should": [
            {"term":      {"auth_code.keyword": auth_code}},
            {"wildcard":  {"message":           {"value": f"*{auth_code}*"}}},
        ], "minimum_should_match": 1}})
    return must


def _fmt_ts(ts):
    if ts and 'T' in ts:
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    return ts


@bp.route('/ej-search', methods=['GET', 'POST'])
def ej_search():
    # Defaults
    keyword = card = date_from = atm_id = event_type = status = min_amount = max_amount = auth_code = ''
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
        date_from   = request.form.get('date_from', '')
        date_to     = request.form.get('date_to', date.today().isoformat())
        atm_id      = request.form.get('atm_id', '')
        event_type  = request.form.get('event_type', '')
        status      = request.form.get('status', '')
        min_amount  = request.form.get('min_amount', '').strip()
        max_amount  = request.form.get('max_amount', '').strip()
        auth_code   = request.form.get('auth_code', '').strip()
        page        = int(request.form.get('page', 1))

        must = _build_must(keyword, card, date_from, date_to, atm_id,
                           event_type, status, min_amount, max_amount, auth_code)

        if not must:
            error = "Please provide at least one search filter."
        else:
            try:
                resp = requests.get(
                    f"http://{ES_HOST}/{ES_INDEX}/_search",
                    json={
                        "query": {"bool": {"must": must}},
                        "size": per_page,
                        "from": (page - 1) * per_page,
                        "sort": [{"@timestamp": "desc"}],
                        "track_total_hits": True,
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    body  = resp.json()
                    total = body.get('hits', {}).get('total', {}).get('value', 0)
                    for hit in body.get('hits', {}).get('hits', []):
                        src = _parse_ej_log_line(hit['_source'])
                        results.append({
                            'timestamp':   _fmt_ts(src.get('@timestamp', '')),
                            'atm_id':      src.get('atm_id', ''),
                            'txn_type':    src.get('sub_type', src.get('event_type', '')),
                            'card':        src.get('card_masked', ''),
                            'status':      src.get('status', ''),
                            'amount':      src.get('amount', ''),
                            'message':     src.get('message', ''),
                            'terminal_id': src.get('terminal_id', ''),
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
            except requests.exceptions.ConnectionError:
                error = f"Cannot connect to OpenSearch at {ES_HOST}"
            except Exception as e:
                error = f"Search error: {e}"

        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1

    # ATM dropdown from database
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT atm_id, branch FROM atm_locations ORDER BY atm_id")
    db_atms = cur.fetchall()
    cur.close()
    conn.close()

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
def ej_search_csv():
    card        = request.args.get('card', '')
    date_from   = request.args.get('date_from', '')
    date_to     = request.args.get('date_to', date.today().isoformat())
    atm_id      = request.args.get('atm_id', '')
    keyword     = request.args.get('keyword', '')
    event_type  = request.args.get('event_type', '')
    status      = request.args.get('status', '')
    min_amount  = request.args.get('min_amount', '')
    max_amount  = request.args.get('max_amount', '')
    auth_code   = request.args.get('auth_code', '')

    must = _build_must(keyword, card, date_from, date_to, atm_id,
                       event_type, status, min_amount, max_amount, auth_code)
    rows = []
    if must:
        try:
            resp = requests.get(
                f"http://{ES_HOST}/{ES_INDEX}/_search",
                json={"query": {"bool": {"must": must}}, "size": 5000, "sort": [{"@timestamp": "desc"}]},
                timeout=30,
            )
            if resp.status_code == 200:
                for hit in resp.json().get('hits', {}).get('hits', []):
                    src = _parse_ej_log_line(hit['_source'])
                    rows.append([
                        _fmt_ts(src.get('@timestamp', '')),
                        src.get('atm_id', ''),
                        src.get('sub_type', src.get('event_type', '')),
                        src.get('card_masked', ''),
                        src.get('status', ''),
                        src.get('amount', ''),
                        src.get('message', ''),
                    ])
        except Exception:
            pass

    buf = io.StringIO()
    w   = csv.writer(buf)
    if not rows and not must:
        w.writerow(['ERROR: No search filters provided. Export cancelled.'])
    elif not rows:
        w.writerow(['ERROR: OpenSearch query failed or returned no data. Check that OpenSearch is running and try again.'])
    w.writerow(['Timestamp', 'ATM ID', 'Transaction Type', 'Card (masked)', 'Status', 'Amount', 'Raw Log'])
    w.writerows(rows)
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=ej_search_{date.today().isoformat()}.csv'}
    )
