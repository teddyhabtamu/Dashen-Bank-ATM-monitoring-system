"""
blueprints/admin.py
ATM Admin registration routes.
"""
from flask import Blueprint, render_template, request, redirect, jsonify, flash
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

    return render_template('admin_atm.html', atms=atms, atm_count=len(atms), fields=FIELDS)


@bp.route('/atm/save', methods=['POST'])
def atm_save():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    field_names = [f[0] for f in FIELDS]
    data = {}
    for f in field_names:
        v = request.form.get(f, '').strip()
        data[f] = v if v else None

    errors = _validate(data)
    if errors:
        if is_ajax:
            return jsonify({'error': True, 'errors': errors, 'message': 'Please fix the highlighted fields.'})
        flash('Validation failed. Please correct the highlighted fields.', 'error')
        return redirect('/admin/atm')

    conn = get_db()
    cur = conn.cursor()
    try:
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
    except Exception as e:
        conn.rollback()
        err_msg = str(e)
        if 'duplicate key' in err_msg.lower() or 'unique' in err_msg.lower():
            db_err = {'atm_id': 'This ATM ID already exists'}
        else:
            db_err = {'_general': f'Database error: {err_msg}'}
        if is_ajax:
            return jsonify({'error': True, 'errors': db_err, 'message': 'Database error. Please try again.'})
        flash(f'Database error: {err_msg}', 'error')
        return redirect('/admin/atm')
    finally:
        cur.close()
        conn.close()

    if is_ajax:
        return jsonify({'success': True, 'atm_id': data['atm_id']})

    return _guide_page(data)


@bp.route('/atm/delete', methods=['POST'])
def atm_delete():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    atm_id = request.form.get('atm_id', '').strip()
    if not atm_id:
        if is_ajax:
            return jsonify({'error': True, 'message': 'ATM ID is required.'})
        flash('ATM ID is required for deletion.', 'error')
        return redirect('/admin/atm')

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM atm_locations WHERE atm_id = %s", (atm_id,))
        if cur.rowcount == 0:
            if is_ajax:
                return jsonify({'error': True, 'message': f'ATM {atm_id} not found.'})
            flash(f'ATM {atm_id} not found.', 'error')
            return redirect('/admin/atm')
        conn.commit()
    except Exception as e:
        conn.rollback()
        if is_ajax:
            return jsonify({'error': True, 'message': f'Database error: {str(e)}'})
        flash(f'Database error: {str(e)}', 'error')
        return redirect('/admin/atm')
    finally:
        cur.close()
        conn.close()

    if is_ajax:
        return jsonify({'success': True, 'atm_id': atm_id})
    return redirect('/admin/atm')


def _validate(data):
    errors = {}
    required = ['atm_id', 'branch', 'district', 'city', 'region',
                'terminal_id', 'vendor', 'model', 'status']
    for field in required:
        if not data.get(field):
            errors[field] = 'This field is required'
    if data.get('atm_id') and len(data['atm_id']) < 3:
        errors['atm_id'] = 'ATM ID must be at least 3 characters'
    if data.get('latitude'):
        try:
            n = float(data['latitude'])
            if n < -90 or n > 90:
                errors['latitude'] = 'Must be between -90 and 90'
        except (ValueError, TypeError):
            errors['latitude'] = 'Must be a valid number'
    if data.get('longitude'):
        try:
            n = float(data['longitude'])
            if n < -180 or n > 180:
                errors['longitude'] = 'Must be between -180 and 180'
        except (ValueError, TypeError):
            errors['longitude'] = 'Must be a valid number'
    if data.get('install_date'):
        import re
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', data['install_date']):
            errors['install_date'] = 'Use YYYY-MM-DD format'
    if data.get('status') and data['status'] not in ('active', 'inactive'):
        errors['status'] = 'Must be active or inactive'
    return errors


def _guide_page(data):
    atm_id_val = data.get('atm_id') or 'ATM-XXX'
    branch_val = data.get('branch') or 'Branch Name'
    tid_val = data.get('terminal_id') or 'TIDXXX'

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

    return f"""<!DOCTYPE html><html><head><title>ATM Registered</title>
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
