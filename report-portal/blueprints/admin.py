"""
blueprints/admin.py
ATM Admin registration routes.
"""
from flask import Blueprint, render_template, request, redirect
from db import get_db

bp = Blueprint('admin', __name__, url_prefix='/admin')

FIELDS = [
    ('atm_id',      'ATM ID',       'text'),
    ('branch',      'Branch',       'text'),
    ('district',    'District',     'text'),
    ('city',        'City',         'text'),
    ('region',      'Region',       'text'),
    ('latitude',    'Latitude',     'number'),
    ('longitude',   'Longitude',    'number'),
    ('terminal_id', 'Terminal ID',  'text'),
    ('vendor',      'Vendor',       'text'),
    ('model',       'Model',        'text'),
    ('install_date','Install Date', 'date'),
    ('status',      'Status',       'select'),
]


@bp.route('/atm')
def atm_list():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM atm_locations ORDER BY atm_id")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cur.close()
    conn.close()

    atms = [dict(zip(cols, r)) for r in rows]

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

    return render_template(
        'admin_atm.html',
        atms=atms,
        atm_count=len(atms),
        fields=FIELDS,
        edit_data=edit_data,
        edit_mode=edit_mode,
        edit_note=bool(edit_mode),
        form_title='Edit ATM' if edit_mode else 'Add New ATM',
    )


@bp.route('/atm/save', methods=['POST'])
def atm_save():
    field_names = [f[0] for f in FIELDS]
    data = {}
    for f in field_names:
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
            branch       = EXCLUDED.branch,
            district     = EXCLUDED.district,
            city         = EXCLUDED.city,
            region       = EXCLUDED.region,
            latitude     = EXCLUDED.latitude,
            longitude    = EXCLUDED.longitude,
            terminal_id  = EXCLUDED.terminal_id,
            vendor       = EXCLUDED.vendor,
            model        = EXCLUDED.model,
            install_date = EXCLUDED.install_date,
            status       = EXCLUDED.status
    """, (
        data['atm_id'], data['branch'], data['district'], data['city'],
        data['region'], data['latitude'], data['longitude'],
        data['terminal_id'], data['vendor'], data['model'],
        data['install_date'], data['status']
    ))
    conn.commit()
    cur.close()
    conn.close()

    atm_id_val = data['atm_id'] or 'ATM-XXX'
    branch_val = data['branch'] or 'Branch Name'
    tid_val    = data['terminal_id'] or 'TIDXXX'

    compose_snippet = f"""  {atm_id_val.lower()}-ej:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: {atm_id_val.lower()}-ej
    command: python3 ej_log_generator.py
    environment:
      ATM_ID: "{atm_id_val}"
      ATM_TERMINAL_ID: "{tid_val}"
      ATM_BRANCH: "{branch_val}"
      EJ_LOG_PATH: "/var/log/atm-ej/{atm_id_val}.log"
    volumes:
      - ./ej-logs:/var/log/atm-ej
    restart: unless-stopped"""

    guide = f"""<!DOCTYPE html><html><head><title>ATM Registered</title>
<style>
  body {{font-family:sans-serif;background:#f5f7fa;padding:40px;}}
  .card {{background:#fff;border-radius:12px;padding:32px;max-width:800px;margin:0 auto;box-shadow:0 2px 12px rgba(0,0,0,.08);}}
  h1 {{color:#012169;margin-top:0;}}
  h2 {{color:#374151;font-size:16px;margin-top:28px;}}
  pre {{background:#1e293b;color:#e2e8f0;padding:20px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.6;}}
  .badge-ok {{display:inline-block;background:#d1fae5;color:#065f46;padding:4px 10px;border-radius:20px;font-size:13px;font-weight:600;}}
  .badge-warn {{display:inline-block;background:#fef3c7;color:#92400e;padding:4px 10px;border-radius:20px;font-size:13px;font-weight:600;}}
  .btn {{display:inline-block;margin-top:24px;background:#012169;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;}}
  ul {{line-height:2;}}
</style></head><body>
<div class="card">
  <h1>&#10003; {atm_id_val} Registered Successfully</h1>
  <p>The ATM has been saved to the database and will appear in the Grafana map, Report Portal dropdowns, and EJ Search filter immediately.</p>

  <h2>What happens automatically <span class="badge-ok">&#10003; No action needed</span></h2>
  <ul>
    <li><strong>Filebeat</strong> — already watches <code>ej-logs/ATM-*.log</code> with a wildcard.<br>
        The moment a file named <code>ej-logs/{atm_id_val}.log</code> exists, Filebeat ships it to OpenSearch automatically.</li>
    <li><strong>EJ Search</strong> — will show {atm_id_val} data as soon as logs are indexed.</li>
    <li><strong>ATM dropdown</strong> — already shows {atm_id_val} in all search filters.</li>
  </ul>

  <h2>What you need to do next <span class="badge-warn">&#9888; Action required</span></h2>

  <p><strong>Option A — Simulated ATM (for demo/testing):</strong><br>
  Add this service to <code>docker-compose.yml</code> before the <code>volumes:</code> section, then run the two commands below:</p>
  <pre>{compose_snippet}</pre>
  <pre>docker compose build {atm_id_val.lower()}-ej
docker compose up -d {atm_id_val.lower()}-ej</pre>

  <p><strong>Option B — Real ATM (production):</strong><br>
  Configure the ATM's EJ log delivery (agent, SFTP, or shared folder) to write files into <code>ej-logs/{atm_id_val}.log</code>.
  Filebeat will detect and ship the file automatically — no further configuration needed.</p>

  <a href="/admin/atm" class="btn">&#8592; Back to ATM Management</a>
</div></body></html>"""

    return guide
