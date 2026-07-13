"""
blueprints/admin.py
ATM Admin registration routes.
"""
import csv, io, re, os
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, redirect, jsonify, flash, Response, current_app, abort
from werkzeug.security import generate_password_hash
from blueprints.auth import login_required, role_required, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER
from db import get_db
from audit import log_action

EAT = timezone(timedelta(hours=3))

def to_eat(dt, fmt='%Y-%m-%d %H:%M'):
    if not dt:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EAT).strftime(fmt)
    return str(dt)

bp = Blueprint('admin', __name__, url_prefix='/admin')

ROLES = [ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN]

# ─── Simulator metrics (for the visual ATM detail page) ────────────────────────
GATEWAY_IP = os.environ.get('GATEWAY_IP', '172.17.0.1')
# Legacy fallback for the 7 original ATMs (before sim_port was stored in the DB).
ATM_PORTS = {
    'ATM-001': 1161, 'ATM-002': 1162, 'ATM-003': 1163,
    'ATM-004': 1164, 'ATM-005': 1165,
    'GRG-001': 1166, 'GRG-002': 1167,
}
CASSETTE_MAX = 2500  # notes-per-cassette capacity used for fill %


def get_sim_port(atm_id):
    """Return the simulator port for an ATM (from atm_locations.sim_port)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT sim_port FROM atm_locations WHERE atm_id = %s", (atm_id,))
            except Exception:
                # sim_port column may not exist yet on a fresh/old DB
                return ATM_PORTS.get(atm_id)
            row = cur.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception:
        pass
    return ATM_PORTS.get(atm_id)


def assign_sim_port(atm_id, vendor):
    """Allocate a sim_port for a newly registered ATM (idempotent)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            base = 1166 if (vendor or '').upper() == 'GRG' else 1161
            cur.execute("SELECT sim_port FROM atm_locations WHERE atm_id = %s", (atm_id,))
            if cur.fetchone():
                return  # already has one
            cur.execute("SELECT COALESCE(MAX(sim_port), %s - 1) "
                        "FROM atm_locations WHERE vendor = %s AND sim_port IS NOT NULL",
                        (base, vendor))
            nxt = (cur.fetchone()[0] or (base - 1)) + 1
            cur.execute("UPDATE atm_locations SET sim_port = %s WHERE atm_id = %s", (nxt, atm_id))
            conn.commit()
    except Exception as e:
        print(f"[assign_sim_port] {atm_id}: {e}")


def fetch_metrics(atm_id):
    """Return the simulator /metrics JSON, or None if unreachable."""
    port = get_sim_port(atm_id)
    if not port:
        return None
    try:
        r = requests.get(f"http://{GATEWAY_IP}:{port}/metrics", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def build_cassettes(vendor, m):
    """Cash-cassette / module layout inside the ATM 'vault' with SVG geometry + fill level.
    Coordinates are expressed in the 320x540 viewBox used by the detail-page SVG."""
    # Vault interior area (matches the <rect> drawn in the template)
    VX, VY, VW, VH = 52, 364, 216, 150

    def slot(key, label, x, y, w, h):
        val = m.get(key)
        if val is None:
            return {'label': label, 'x': x, 'y': y, 'w': w, 'h': h,
                    'pct': None, 'fill_h': 0, 'fill_y': y + h, 'color': 'grey', 'notes': None}
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = 0
        pct = max(0, min(100, round(val / CASSETTE_MAX * 100)))
        fh = round(h * pct / 100)
        color = 'green' if pct > 40 else ('amber' if pct >= 15 else 'red')
        return {'label': label, 'x': x, 'y': y, 'w': w, 'h': h,
                'pct': pct, 'fill_h': fh, 'fill_y': y + h - fh, 'color': color, 'notes': val}

    out = []
    if vendor == 'NCR':  # 2 x 2 cassette grid
        keys = ['1.2.0', '1.3.0', '1.4.0', '1.5.0']
        labels = ['Cass 1', 'Cass 2', 'Cass 3', 'Cass 4']
        gap = 12
        cw = (VW - gap) / 2
        ch = (VH - gap) / 2
        for i, (k, lbl) in enumerate(zip(keys, labels)):
            r, c = divmod(i, 2)
            x = VX + c * (cw + gap)
            y = VY + r * (ch + gap)
            out.append(slot(k, lbl, x, y, cw, ch))
    else:  # GRG — three module bays side by side
        keys = ['2.1.0', '2.2.0', '2.3.0']
        labels = ['Module 1', 'Module 2', 'Module 3']
        gap = 14
        cw = (VW - 2 * gap) / 3
        ch = VH - 14
        for i, (k, lbl) in enumerate(zip(keys, labels)):
            x = VX + i * (cw + gap)
            y = VY + 8
            out.append(slot(k, lbl, x, y, cw, ch))
    return out


def build_hardware(vendor, m):
    """Hardware component status pills. ok=green, warn=amber, error=red."""
    if vendor == 'NCR':
        comps = [
            ('Card reader', 'ok' if m.get('2.1.0') == 1 else 'error'),
            ('Printer', 'ok' if m.get('3.1.0') == 1 else 'error'),
            ('Pin pad', 'ok'),
            ('Retract bin', 'ok' if (m.get('1.6.0') or 0) < 80 else 'warn'),
            ('Network', 'ok' if m.get('7.1.0') == 1 else 'error'),
            ('UPS', 'ok' if (m.get('6.3.0') or 0) > 20 else 'error'),
        ]
    else:  # GRG
        comps = [
            ('Card reader', 'ok' if m.get('3.1.0') == 1 else 'error'),
            ('Printer', 'ok' if m.get('4.1.0') == 1 else 'error'),
            ('Pin pad', 'ok'),
            ('Retract bin', 'ok' if (m.get('2.4.0') or 0) < 80 else 'warn'),
            ('Network', 'ok' if m.get('8.1.0') == 1 else 'error'),
            ('UPS', 'ok' if (m.get('7.2.0') or 0) > 20 else 'error'),
        ]
    return [{'name': n, 'status': s} for n, s in comps]


FIELDS = [
    ('atm_id',      'ATM ID',       'text',   20),
    ('branch',      'Branch',       'text',   100),
    ('district',    'District',     'text',   100),
    ('city',        'City',         'text',   100),
    ('region',      'Region',       'text',   100),
    ('latitude',    'Latitude',     'number', None),
    ('longitude',   'Longitude',    'number', None),
    ('terminal_id', 'Terminal ID',  'text',   20),
    ('vendor',      'Vendor',       'text',   50),
    ('model',       'Model',        'text',   50),
    ('install_date','Install Date', 'date',   None),
    ('status',      'Status',       'select', None),
]


@bp.route('/atm')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_list():
    search = request.args.get('search', '').strip()
    per_page = 50

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1

    with get_db() as conn:
        cur = conn.cursor()

        if search:
            like = f'%{search}%'
            cur.execute("""SELECT COUNT(*) FROM atm_locations
                           WHERE atm_id ILIKE %s OR branch ILIKE %s
                           OR district ILIKE %s OR city ILIKE %s OR region ILIKE %s""",
                        (like, like, like, like, like))
            total = cur.fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            cur.execute("""SELECT l.*, s.state, s.last_seen AT TIME ZONE 'Africa/Addis_Ababa' as last_seen, s.state_changed_at
                           FROM atm_locations l
                           LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id
                           WHERE l.atm_id ILIKE %s OR l.branch ILIKE %s
                           OR l.district ILIKE %s OR l.city ILIKE %s OR l.region ILIKE %s
                           ORDER BY l.atm_id LIMIT %s OFFSET %s""",
                        (like, like, like, like, like, per_page, (page - 1) * per_page))
        else:
            cur.execute("SELECT COUNT(*) FROM atm_locations")
            total = cur.fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            cur.execute("""SELECT l.*, s.state, s.last_seen AT TIME ZONE 'Africa/Addis_Ababa' as last_seen, s.state_changed_at
                           FROM atm_locations l
                           LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id
                           ORDER BY l.atm_id LIMIT %s OFFSET %s""",
                        (per_page, (page - 1) * per_page))

        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()

    atms = [dict(zip(cols, r)) for r in rows]

    # Summary stat row
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT
                (SELECT COUNT(*) FROM atm_locations) as registered,
                (SELECT COUNT(*) FROM atm_current_state WHERE state = 'IN_SERVICE') as in_service,
                (SELECT COUNT(*) FROM atm_locations WHERE vendor = 'NCR') as ncr,
                (SELECT COUNT(*) FROM atm_locations WHERE vendor = 'GRG') as grg""")
            s = cur.fetchone()
            cur.close()
        summary = {'registered': int(s[0] or 0), 'in_service': int(s[1] or 0),
                   'ncr': int(s[2] or 0), 'grg': int(s[3] or 0)}
    except Exception:
        summary = {'registered': total, 'in_service': 0, 'ncr': 0, 'grg': 0}

    return render_template('admin_atm.html', atms=atms, atm_count=total,
                           page=page, total_pages=total_pages,
                           search=search, fields=FIELDS, summary=summary)


@bp.route('/atm/<atm_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_detail(atm_id):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT l.*,
                    COALESCE(s.state, 'UNKNOWN') as current_state,
                    s.state_changed_at,
                    s.last_seen AT TIME ZONE 'Africa/Addis_Ababa' as last_seen
                FROM atm_locations l
                LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id
                WHERE l.atm_id = %s
            """, (atm_id,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            cur.close()
        if not row:
            abort(404)
        atm = dict(zip(cols, row))
        vendor = atm.get('vendor')

        metrics = fetch_metrics(atm_id)
        cassettes = build_cassettes(vendor, metrics) if metrics else []
        hardware = build_hardware(vendor, metrics) if metrics else []

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*),
                    ROUND(100.0 * SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0), 1),
                    COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL'
                        THEN amount ELSE 0 END), 0)
                FROM atm_transactions
                WHERE atm_id = %s AND recorded_at >= NOW() - INTERVAL '24 hours'
            """, (atm_id,))
            q = cur.fetchone()
            total_txns = int(q[0] or 0)
            success_rate = float(q[1] or 0)
            cash_today = float(q[2] or 0)
            cur.execute("""
                SELECT COUNT(*) FROM atm_transactions
                WHERE atm_id = %s AND status = 'ERROR'
                AND recorded_at >= NOW() - INTERVAL '24 hours'
            """, (atm_id,))
            active_faults = int(cur.fetchone()[0] or 0)
            cur.execute("""
                SELECT
                    TO_CHAR(recorded_at AT TIME ZONE 'Africa/Addis_Ababa', 'HH24:MI:SS'),
                    txn_type, card_masked, amount, status, auth_code, error_code
                FROM atm_transactions
                WHERE atm_id = %s
                ORDER BY recorded_at DESC
                LIMIT 20
            """, (atm_id,))
            txns = cur.fetchall()
            cur.close()

        return render_template('admin_atm_detail.html',
                               atm=atm, vendor=vendor, cassettes=cassettes,
                               hardware=hardware, metrics_online=metrics is not None,
                               total_txns=total_txns, success_rate=success_rate,
                               cash_today=cash_today, active_faults=active_faults,
                               txns=txns)
    except Exception as e:
        return f'<h1>Error</h1><pre>{e}</pre>', 500


@bp.route('/atm/get')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_get():
    atm_id = request.args.get('atm_id', '').strip()
    if not atm_id:
        return jsonify({'error': True, 'message': 'ATM ID is required'}), 400
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (atm_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        cur.close()
    if not row:
        return jsonify({'error': True, 'message': f'ATM {atm_id} not found'}), 404
    return jsonify(dict(zip(cols, row)))


@bp.route('/atm/save', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
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

    with get_db() as conn:
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
            # Auto-allocate a simulator port so the new ATM behaves like the others
            assign_sim_port(data['atm_id'], data['vendor'])
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

    if is_ajax:
        log_action('ATM_SAVE', f'ATM {data["atm_id"]} saved/updated')
        return jsonify({'success': True, 'atm_id': data['atm_id']})

    log_action('ATM_SAVE', f'ATM {data["atm_id"]} saved/updated')
    return _guide_page(data)


@bp.route('/atm/delete', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_delete():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    atm_id = request.form.get('atm_id', '').strip()
    if not atm_id:
        if is_ajax:
            return jsonify({'error': True, 'message': 'ATM ID is required.'})
        flash('ATM ID is required for deletion.', 'error')
        return redirect('/admin/atm')

    with get_db() as conn:
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

    if is_ajax:
        log_action('ATM_DELETE', f'ATM {atm_id} deleted')
        return jsonify({'success': True, 'atm_id': atm_id})
    log_action('ATM_DELETE', f'ATM {atm_id} deleted')
    return redirect('/admin/atm')


@bp.route('/atm/bulk-delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def atm_bulk_delete():
    data = request.get_json(silent=True)
    if not data or 'ids' not in data or not isinstance(data['ids'], list):
        return jsonify({'error': True, 'message': 'Please provide a list of ATM IDs.'})

    ids = [i.strip() for i in data['ids'] if i.strip()]
    if not ids:
        return jsonify({'error': True, 'message': 'No ATM IDs provided.'})

    with get_db() as conn:
        cur = conn.cursor()
        try:
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"DELETE FROM atm_locations WHERE atm_id IN ({placeholders})", ids)
            deleted = cur.rowcount
            conn.commit()
        except Exception as e:
            conn.rollback()
            return jsonify({'error': True, 'message': f'Database error: {str(e)}'})
        finally:
            cur.close()

    log_action('ATM_BULK_DELETE', f'{deleted} ATMs deleted: {",".join(ids)}')
    return jsonify({'success': True, 'deleted': deleted})


@bp.route('/atm/check', methods=['POST'])
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_check():
    data = request.get_json(silent=True)
    if not data or 'ids' not in data:
        return jsonify({'existing': []})
    ids = [i.strip() for i in data['ids'] if i.strip()]
    if not ids:
        return jsonify({'existing': []})
    with get_db() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(ids))
        cur.execute(f"SELECT atm_id FROM atm_locations WHERE atm_id IN ({placeholders})", ids)
        existing = [r[0] for r in cur.fetchall()]
        cur.close()
    return jsonify({'existing': existing})


@bp.route('/atm/csv')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_csv_export():
    ids_param = request.args.get('ids', '').strip()

    with get_db() as conn:
        cur = conn.cursor()
        if ids_param:
            ids = [i.strip() for i in ids_param.split(',') if i.strip()]
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"SELECT * FROM atm_locations WHERE atm_id IN ({placeholders}) ORDER BY atm_id", ids)
        else:
            cur.execute("SELECT * FROM atm_locations ORDER BY atm_id")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)

    log_action('EXPORT', f'ATM CSV export ({len(rows)} rows, filter={ids_param or "all"})')
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=atm_locations_{datetime.now().strftime("%Y%m%d")}.csv'}
    )


@bp.route('/atm/import', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_import():
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': True, 'message': 'Please select a CSV file to upload.'})

    try:
        content = file.stream.read().decode('utf-8-sig')
    except Exception:
        return jsonify({'error': True, 'message': 'Could not read file. Ensure it is a valid UTF-8 CSV.'})

    reader = csv.DictReader(io.StringIO(content))
    required = ['atm_id', 'branch', 'district', 'city', 'region',
                'terminal_id', 'vendor', 'model', 'status']
    missing_cols = [c for c in required if c not in reader.fieldnames]
    if missing_cols:
        return jsonify({'error': True, 'message': f'Missing required columns: {", ".join(missing_cols)}'})

    rows = []
    errors = []
    line = 1
    for row in reader:
        line += 1
        errs = _validate(row)
        if errs:
            errors.append({'line': line, 'atm_id': row.get('atm_id', '?'), 'errors': errs})
        else:
            rows.append(row)

    if errors and not rows:
        return jsonify({'error': True, 'message': f'All {len(errors)} row(s) have errors. No data imported.',
                        'import_errors': errors})

    imported = 0
    db_errors = []
    if rows:
        with get_db() as conn:
            cur = conn.cursor()
            for row in rows:
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
                        row['atm_id'], row.get('branch'), row.get('district'),
                        row.get('city'), row.get('region'), row.get('latitude'),
                        row.get('longitude'), row.get('terminal_id'),
                        row.get('vendor'), row.get('model'),
                        row.get('install_date'), row.get('status')
                    ))
                    imported += 1
                except Exception as e:
                    db_errors.append({'atm_id': row['atm_id'], 'error': str(e)})
            conn.commit()
            # Auto-allocate simulator ports so imported ATMs behave like the others
            for row in rows:
                assign_sim_port(row['atm_id'], row.get('vendor'))
            cur.close()

    log_action('ATM_IMPORT', f'Imported {imported}, skipped {len(errors)}, db_errors {len(db_errors)}')
    return jsonify({
        'success': True,
        'imported': imported,
        'skipped': len(errors),
        'db_errors': db_errors,
        'import_errors': errors
    })


def _validate(data):
    errors = {}
    required = ['atm_id', 'branch', 'district', 'city', 'region',
                'terminal_id', 'vendor', 'model', 'status']
    for field in required:
        if not data.get(field):
            errors[field] = 'This field is required'
    if data.get('atm_id') and len(data['atm_id']) < 3:
        errors['atm_id'] = 'ATM ID must be at least 3 characters'
    maxlens = {'atm_id': 20, 'branch': 100, 'district': 100, 'city': 100,
               'region': 100, 'terminal_id': 20, 'vendor': 50, 'model': 50}
    for field, maxlen in maxlens.items():
        val = data.get(field)
        if val and len(val) > maxlen:
            errors[field] = f'Max {maxlen} characters'
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


# ─── AUDIT LOG ───────────────────────────────────────────────

@bp.route('/audit')
@login_required
@role_required(ROLE_ADMIN)
def audit_log():
    page = max(1, int(request.args.get('page', 1)))
    per_page = 50
    search = request.args.get('search', '').strip()
    action_filter = request.args.get('action', '').strip()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            where_clauses = []
            params = []
            if search:
                where_clauses.append("(username ILIKE %s OR detail ILIKE %s)")
                like = f'%{search}%'
                params.extend([like, like])
            if action_filter:
                where_clauses.append("action = %s")
                params.append(action_filter)
            where_sql = ' AND '.join(where_clauses) if where_clauses else 'TRUE'

            cur.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where_sql}", params)
            total = cur.fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)

            cur.execute(f"""
                SELECT id, performed_at, username, action, detail, ip_address
                FROM audit_log WHERE {where_sql}
                ORDER BY performed_at DESC LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page])
            rows = cur.fetchall()

            cur.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
            actions = [r[0] for r in cur.fetchall()]
            cur.close()
        rows = [list(r) for r in rows]
        for r in rows:
            r[1] = to_eat(r[1], '%Y-%m-%d %H:%M')
    except Exception as e:
        rows = []
        total = 0
        total_pages = 1
        actions = []

    return render_template('admin_audit.html', logs=rows, total=total,
                           page=page, total_pages=total_pages, search=search,
                           action_filter=action_filter, actions=actions)


# ─── SCHEDULED REPORTS ─────────────────────────────────────────

@bp.route('/schedules')
@login_required
@role_required(ROLE_ADMIN)
def schedule_list():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, report_type, format, schedule, recipients, enabled, last_run FROM scheduled_reports ORDER BY id")
            rows = cur.fetchall()
            cur.close()
        rows = [list(r) for r in rows]
        for r in rows:
            r[7] = to_eat(r[7], '%Y-%m-%d %H:%M')
    except Exception:
        rows = []
    return render_template('admin_schedules.html', schedules=rows)


@bp.route('/schedules/save', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_save():
    name = request.form.get('name', '').strip()
    report_type = request.form.get('report_type', '').strip()
    fmt = request.form.get('format', 'pdf').strip()
    schedule = request.form.get('schedule', '').strip()
    recipients = request.form.get('recipients', '').strip()
    params = request.form.get('params', '').strip()
    schedule_id = request.form.get('id', '').strip()

    if not all([name, report_type, schedule, recipients]):
        flash('Name, report type, schedule, and recipients are required.', 'error')
        return redirect('/admin/schedules')

    try:
        with get_db() as conn:
            cur = conn.cursor()
            if schedule_id and schedule_id.isdigit():
                cur.execute("""
                    UPDATE scheduled_reports SET name=%s, report_type=%s, format=%s,
                        schedule=%s, recipients=%s, params=%s
                    WHERE id=%s
                """, (name, report_type, fmt, schedule, recipients, params or None, schedule_id))
            else:
                cur.execute("""
                    INSERT INTO scheduled_reports (name, report_type, format, schedule, recipients, params)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (name, report_type, fmt, schedule, recipients, params or None))
            conn.commit()
            cur.close()
        log_action('SCHEDULE_SAVE', f'Schedule "{name}" ({report_type}/{fmt}) saved')
        flash('Schedule saved.', 'success')
    except Exception as e:
        flash(f'Database error: {e}', 'error')

    return redirect('/admin/schedules')


@bp.route('/schedules/toggle', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_toggle():
    data = request.get_json(silent=True)
    sid = data.get('id') if data else None
    if not sid:
        return jsonify({'error': True, 'message': 'Missing id'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE scheduled_reports SET enabled = NOT enabled WHERE id = %s RETURNING enabled, name", (sid,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        if row:
            log_action('SCHEDULE_TOGGLE', f'Schedule "{row[1]}" {"enabled" if row[0] else "disabled"}')
            return jsonify({'success': True, 'enabled': row[0]})
        return jsonify({'error': True, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/schedules/delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_delete():
    data = request.get_json(silent=True)
    sid = data.get('id') if data else None
    if not sid:
        return jsonify({'error': True, 'message': 'Missing id'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM scheduled_reports WHERE id = %s RETURNING name", (sid,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        if row:
            log_action('SCHEDULE_DELETE', f'Schedule "{row[0]}" deleted')
            sched = current_app.config.get('SCHEDULER')
            if sched:
                try:
                    sched.remove_job(f'report_{sid}')
                except Exception:
                    pass
            return jsonify({'success': True})
        return jsonify({'error': True, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/schedules/get')
@login_required
@role_required(ROLE_ADMIN)
def schedule_get():
    sid = request.args.get('id', '').strip()
    if not sid or not sid.isdigit():
        return jsonify({'error': True, 'message': 'Missing id'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, report_type, format, schedule, recipients, params, enabled FROM scheduled_reports WHERE id = %s", (sid,))
            row = cur.fetchone()
            cur.close()
        if row:
            return jsonify({
                'id': row[0], 'name': row[1], 'report_type': row[2],
                'format': row[3], 'schedule': row[4], 'recipients': row[5],
                'params': row[6], 'enabled': row[7]
            })
        return jsonify({'error': True, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/users')
@login_required
@role_required(ROLE_ADMIN)
def user_list():
    search = request.args.get('search', '').strip()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if search:
                cur.execute("SELECT username, role, created_at, last_login FROM app_users WHERE username ILIKE %s ORDER BY created_at DESC", (f'%{search}%',))
            else:
                cur.execute("SELECT username, role, created_at, last_login FROM app_users ORDER BY created_at DESC")
            users = cur.fetchall()
            cur.close()
        users = [list(u) for u in users]
        for u in users:
            u[2] = to_eat(u[2], '%Y-%m-%d')
            u[3] = to_eat(u[3], '%Y-%m-%d %H:%M')
        return render_template('admin_users.html', users=users, roles=ROLES, search=search)
    except Exception as e:
        flash(str(e), 'error')
        return render_template('admin_users.html', users=[], roles=ROLES, search=search)


@bp.route('/users/create', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def user_create():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    role = request.form.get('role', ROLE_VIEWER).strip()
    if not username or len(username) < 3:
        flash('Username must be at least 3 characters', 'error')
        return redirect('/admin/users')
    if not password or len(password) < 4:
        flash('Password must be at least 4 characters', 'error')
        return redirect('/admin/users')
    if role not in ROLES:
        flash('Invalid role selected', 'error')
        return redirect('/admin/users')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT username FROM app_users WHERE username = %s", (username,))
            if cur.fetchone():
                cur.close()
                flash(f'User "{username}" already exists', 'error')
                return redirect('/admin/users')
            cur.execute(
                "INSERT INTO app_users (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, generate_password_hash(password), role)
            )
            conn.commit()
            cur.close()
        log_action('USER_CREATE', f'Created {role} user "{username}"')
        flash(f'User "{username}" created successfully', 'success')
        return redirect('/admin/users')
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect('/admin/users')


@bp.route('/users/edit', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def user_edit():
    username = request.form.get('username', '').strip()
    role = request.form.get('role', '').strip()
    password = request.form.get('password', '').strip()
    if not username:
        flash('No username provided', 'error')
        return redirect('/admin/users')
    if role not in ROLES:
        flash('Invalid role selected', 'error')
        return redirect('/admin/users')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if password:
                cur.execute("UPDATE app_users SET role = %s, password_hash = %s WHERE username = %s",
                            (role, generate_password_hash(password), username))
            else:
                cur.execute("UPDATE app_users SET role = %s WHERE username = %s",
                            (role, username))
            conn.commit()
            cur.close()
        log_action('USER_EDIT', f'Updated {username} (role={role})')
        flash(f'User "{username}" updated', 'success')
        return redirect('/admin/users')
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect('/admin/users')


@bp.route('/users/delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def user_delete():
    username = request.form.get('username', '').strip()
    if not username:
        flash('No username provided', 'error')
        return redirect('/admin/users')
    if username == 'admin':
        flash('Cannot delete the default admin user', 'error')
        return redirect('/admin/users')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM app_users WHERE username = %s", (username,))
            conn.commit()
            cur.close()
        log_action('USER_DELETE', f'Deleted user "{username}"')
        flash(f'User "{username}" deleted', 'success')
        return redirect('/admin/users')
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect('/admin/users')


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
