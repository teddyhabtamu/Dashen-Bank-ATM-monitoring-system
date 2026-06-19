#!/usr/bin/env python3
import os, csv, io
import requests
from flask import Flask, request, redirect, url_for, Response
from routes import bp as report_bp
from static_html import PORTAL_HTML, ADMIN_HTML, EJ_SEARCH_HTML
from db import get_db
from datetime import datetime, date, timedelta

app = Flask(__name__)
app.register_blueprint(report_bp)

# Configurable via .env — defaults work for local dev
GRAFANA_URL        = os.environ.get('GRAFANA_URL', 'http://localhost:3002')
REPORT_PORTAL_PORT = os.environ.get('REPORT_PORTAL_PORT', '8888')
ES_HOST            = os.environ.get('ES_HOST', 'elasticsearch:9200')
ES_INDEX           = os.environ.get('ES_INDEX', '.ds-atm-ej-live-*,atm-electronic-journal')


@app.route('/')
def index():
    html = PORTAL_HTML\
        .replace('{{GRAFANA_URL}}', GRAFANA_URL)\
        .replace('{{REPORT_PORTAL_PORT}}', REPORT_PORTAL_PORT)
    return html


@app.route('/admin/atm')
def admin_atm_list():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM atm_locations ORDER BY atm_id")
    cols = [d[0] for d in cur.description]
    atms = cur.fetchall()
    cur.close()
    conn.close()

    rows_html = ""
    for r in atms:
        d = dict(zip(cols, r))
        status = d.get('status')
        status_badge = f'<span class="badge {status}">{status or "unknown"}</span>' if status else '<span class="badge active">active</span>'
        rows_html += f"""<tr>
          <td>{d['atm_id']}</td>
          <td>{d['branch'] or '—'}</td>
          <td>{d['district'] or '—'}</td>
          <td>{d['city'] or '—'}</td>
          <td>{d['region'] or '—'}</td>
          <td>{d['latitude'] or '—'}</td>
          <td>{d['longitude'] or '—'}</td>
          <td>{d['terminal_id'] or '—'}</td>
          <td>{d['vendor'] or '—'}</td>
          <td>{d['model'] or '—'}</td>
          <td>{d['install_date'] or '—'}</td>
          <td>{status_badge}</td>
          <td class="actions"><a class="edit" href="?edit={d['atm_id']}">Edit</a></td>
        </tr>"""

    if not rows_html:
        rows_html = '<tr><td colspan="13" class="empty">No ATMs registered yet.</td></tr>'

    edit_mode = request.args.get('edit')
    edit_data = {}
    if edit_mode:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (edit_mode,))
        row = cur.fetchone()
        if row:
            edit_data = dict(zip([d[0] for d in cur.description], row))
        cur.close()
        conn.close()

    fields = [
        ('atm_id', 'ATM ID', 'text'),
        ('branch', 'Branch', 'text'),
        ('district', 'District', 'text'),
        ('city', 'City', 'text'),
        ('region', 'Region', 'text'),
        ('latitude', 'Latitude', 'number'),
        ('longitude', 'Longitude', 'number'),
        ('terminal_id', 'Terminal ID', 'text'),
        ('vendor', 'Vendor', 'text'),
        ('model', 'Model', 'text'),
        ('install_date', 'Install Date', 'date'),
        ('status', 'Status', 'select'),
    ]
    form_fields = ""
    for name, label, typ in fields:
        val = edit_data.get(name, '')
        if typ == 'select':
            opts = ''.join(f'<option value="{s}"{" selected" if val==s else ""}>{s}</option>' for s in ['active', 'inactive'])
            form_fields += f'<div class="form-group"><label>{label}</label><select name="{name}">{opts}</select></div>'
        elif typ == 'date':
            form_fields += f'<div class="form-group"><label>{label}</label><input type="text" name="{name}" class="flatpickr-date" placeholder="YYYY-MM-DD" value="{val if val is not None else ""}"></div>'
        else:
            form_fields += f'<div class="form-group"><label>{label}</label><input type="{typ}" name="{name}" value="{val if val is not None else ""}"{" step=any" if typ=="number" else ""}></div>'

    edit_note = ""
    if edit_mode:
        edit_note = f'<h3 style="padding:24px 24px 0;font-size:14px;color:#012169">Editing ATM <code>{edit_mode}</code></h3>'

    return ADMIN_HTML\
        .replace('{{ADMIN_FORM_TITLE}}', 'Edit ATM' if edit_mode else 'Add New ATM')\
        .replace('{{ADMIN_FORM_EDIT_NOTE}}', edit_note)\
        .replace('{{ADMIN_FORM_FIELDS}}', form_fields)\
        .replace('{{ADMIN_ATM_COUNT}}', str(len(atms)))\
        .replace('{{ADMIN_TABLE_ROWS}}', rows_html)


@app.route('/admin/atm/save', methods=['POST'])
def admin_atm_save():
    fields = ['atm_id', 'branch', 'district', 'city', 'region',
              'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
              'install_date', 'status']

    data = {}
    for f in fields:
        v = request.form.get(f, '').strip()
        data[f] = v if v else None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO atm_locations
            (atm_id, branch, district, city, region, latitude, longitude,
             terminal_id, vendor, model, install_date, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (atm_id) DO UPDATE SET
            branch      = EXCLUDED.branch,
            district    = EXCLUDED.district,
            city        = EXCLUDED.city,
            region      = EXCLUDED.region,
            latitude    = EXCLUDED.latitude,
            longitude   = EXCLUDED.longitude,
            terminal_id = EXCLUDED.terminal_id,
            vendor      = EXCLUDED.vendor,
            model       = EXCLUDED.model,
            install_date = EXCLUDED.install_date,
            status      = EXCLUDED.status
    """, (
        data['atm_id'], data['branch'], data['district'], data['city'],
        data['region'], data['latitude'], data['longitude'],
        data['terminal_id'], data['vendor'], data['model'],
        data['install_date'], data['status']
    ))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('admin_atm_list'))


@app.route('/ej-search', methods=['GET', 'POST'])
def ej_search():
    card = ''
    date_from = ''
    date_to = date.today().isoformat()
    atm_id = ''
    page = 1
    per_page = 50
    results = []
    total = 0
    error = None

    if request.method == 'POST':
        card = request.form.get('card', '').strip()
        date_from = request.form.get('date_from', '')
        date_to = request.form.get('date_to', date.today().isoformat())
        atm_id = request.form.get('atm_id', '')
        page = int(request.form.get('page', 1))

        must = []
        if card and len(card) == 4 and card.isdigit():
            must.append({"wildcard": {"card_masked": {"value": f"*{card}"}}})
        if date_from:
            must.append({"range": {"@timestamp": {"gte": date_from}}})
        if date_to:
            must.append({"range": {"@timestamp": {"lte": f"{date_to}T23:59:59"}}})
        if atm_id:
            must.append({"term": {"atm_id": atm_id}})

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
                    body = resp.json()
                    total = body.get('hits', {}).get('total', {}).get('value', 0)
                    hits = body.get('hits', {}).get('hits', [])
                    for hit in hits:
                        src = hit['_source']
                        ts = src.get('@timestamp', '')
                        if ts and 'T' in ts:
                            try:
                                ts = datetime.fromisoformat(ts.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
                            except Exception:
                                pass
                        results.append({
                            'timestamp': ts,
                            'atm_id': src.get('atm_id', ''),
                            'txn_type': src.get('sub_type', src.get('event_type', '')),
                            'card': src.get('card_masked', ''),
                            'status': src.get('status', ''),
                            'amount': src.get('amount', ''),
                        })
                else:
                    error = f"Elasticsearch returned status {resp.status_code}"
            except requests.exceptions.ConnectionError:
                error = f"Cannot connect to Elasticsearch at {ES_HOST}"
            except Exception as e:
                error = f"Search error: {e}"

    # Build ATM dropdown options
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT atm_id, branch FROM atm_locations ORDER BY atm_id")
    db_atms = cur.fetchall()
    cur.close()
    conn.close()
    atm_opts = '<option value="">All ATMs</option>'
    for a_id, a_branch in db_atms:
        sel = ' selected' if a_id == atm_id else ''
        label = f'{a_id} — {a_branch}' if a_branch else a_id
        atm_opts += f'<option value="{a_id}"{sel}>{label}</option>'

    # Build results table + pagination
    results_section = ""
    if request.method == 'POST':
        if error:
            results_section = f'<div class="card"><div class="card-header"><h2>Error</h2></div><p style="padding:24px;color:#DC2626">{error}</p></div>'
        else:
            rows = ""
            for r in results:
                status_badge = f'<span class="badge {r["status"]}">{r["status"] or "—"}</span>'
                amt = f'ETB {r["amount"]:,.2f}' if r.get('amount') and r['amount'] not in (None, '', 0) else '—'
                rows += f"""<tr>
                    <td>{r['timestamp']}</td>
                    <td>{r['atm_id']}</td>
                    <td>{r['txn_type']}</td>
                    <td>{r['card'] or '—'}</td>
                    <td>{status_badge}</td>
                    <td style="text-align:right">{amt}</td>
                </tr>"""
            if not rows:
                rows = '<tr><td colspan="6" class="empty">No results found.</td></tr>'

            qs = f"card={card}&date_from={date_from}&date_to={date_to}&atm_id={atm_id}"
            total_pages = max(1, (total + per_page - 1) // per_page) if total else 1

            pag = ""
            if total_pages > 1:
                pag += '<div style="display:flex;align-items:center;justify-content:center;gap:6px;padding:14px 24px;border-top:1px solid #EEF1F6">'
                if page > 1:
                    pag += f'<button type="submit" name="page" value="{page-1}" class="btn btn-outline" style="padding:6px 12px;font-size:11px">&larr; Prev</button>'
                start = max(1, page - 2)
                end = min(total_pages, page + 2)
                for p in range(start, end + 1):
                    active = ' style="background:#012169;color:#fff;border-color:#012169"' if p == page else ''
                    pag += f'<button type="submit" name="page" value="{p}" class="btn btn-outline" style="padding:6px 10px;font-size:11px;min-width:32px"{active}>{p}</button>'
                if page < total_pages:
                    pag += f'<button type="submit" name="page" value="{page+1}" class="btn btn-outline" style="padding:6px 12px;font-size:11px">Next &rarr;</button>'
                pag += '</div>'

            results_section = f"""<div class="card">
                <div class="card-header">
                    <h2>Results ({total})</h2>
                    <a href="/ej-search/csv?{qs}" class="btn btn-gold"><i data-lucide="download" style="width:14px;height:14px"></i> Export to CSV</a>
                </div>
                <div style="overflow-x:auto">
                <table>
                    <thead><tr>
                        <th>Timestamp</th><th>ATM ID</th><th>Transaction Type</th><th>Card (masked)</th><th>Status</th><th style="text-align:right">Amount</th>
                    </tr></thead>
                    <tbody>{rows}</tbody>
                </table>
                </div>
                {pag}
            </div>"""

    return EJ_SEARCH_HTML\
        .replace('{{EJ_CARD}}', card)\
        .replace('{{EJ_DATE_FROM}}', date_from)\
        .replace('{{EJ_DATE_TO}}', date_to)\
        .replace('{{EJ_ATM_OPTIONS}}', atm_opts)\
        .replace('{{EJ_RESULTS_SECTION}}', results_section)


@app.route('/ej-search/csv')
def ej_search_csv():
    card = request.args.get('card', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', date.today().isoformat())
    atm_id = request.args.get('atm_id', '')

    must = []
    if card and len(card) == 4 and card.isdigit():
        must.append({"wildcard": {"card_masked": {"value": f"*{card}"}}})
    if date_from:
        must.append({"range": {"@timestamp": {"gte": date_from}}})
    if date_to:
        must.append({"range": {"@timestamp": {"lte": f"{date_to}T23:59:59"}}})
    if atm_id:
        must.append({"term": {"atm_id": atm_id}})

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
                    src = hit['_source']
                    ts = src.get('@timestamp', '')
                    if ts and 'T' in ts:
                        try:
                            ts = datetime.fromisoformat(ts.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
                        except Exception:
                            pass
                    rows.append([
                        ts,
                        src.get('atm_id', ''),
                        src.get('sub_type', src.get('event_type', '')),
                        src.get('card_masked', ''),
                        src.get('status', ''),
                        src.get('amount', ''),
                    ])
        except Exception:
            pass

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Timestamp', 'ATM ID', 'Transaction Type', 'Card (masked)', 'Status', 'Amount'])
    w.writerows(rows)
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=ej_search_{date.today().isoformat()}.csv'}
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(REPORT_PORTAL_PORT), debug=True)
