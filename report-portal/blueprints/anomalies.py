"""
blueprints/anomalies.py
Alert management and acknowledgement routes.
"""
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, session, jsonify
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
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

bp = Blueprint('anomalies', __name__, url_prefix='/admin')

SEVERITIES = ['HIGH', 'MEDIUM', 'LOW']
ANOMALY_TYPES = ['VELOCITY', 'FAILURE_SPIKE', 'LARGE_TXN', 'RAPID_SEQ', 'OFFHOURS_SPIKE']

ANOMALY_LABELS = {
    'VELOCITY': 'Velocity Abuse',
    'FAILURE_SPIKE': 'Failure Spike',
    'LARGE_TXN': 'Large Transaction',
    'RAPID_SEQ': 'Rapid Sequential',
    'OFFHOURS_SPIKE': 'Off-Hours Spike',
}

SEVERITY_COLORS = {
    'HIGH': {'bg': '#FEF2F2', 'fg': '#DC2626'},
    'MEDIUM': {'bg': '#FFFBEB', 'fg': '#D97706'},
    'LOW': {'bg': '#EFF6FF', 'fg': '#2563EB'},
}

TYPE_COLORS = {
    'VELOCITY': {'bg': '#FEF2F2', 'fg': '#DC2626'},
    'FAILURE_SPIKE': {'bg': '#FFF7ED', 'fg': '#C2410C'},
    'LARGE_TXN': {'bg': '#FFFBEB', 'fg': '#D97706'},
    'RAPID_SEQ': {'bg': '#F5F3FF', 'fg': '#7C3AED'},
    'OFFHOURS_SPIKE': {'bg': '#EFF6FF', 'fg': '#2563EB'},
}


@bp.route('/anomalies')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def anomaly_list():
    page = max(1, int(request.args.get('page', 1)))
    per_page = 50

    severity = request.args.get('severity', '').strip()
    anomaly_type = request.args.get('type', '').strip()
    atm_search = request.args.get('atm_id', '').strip()
    acknowledged = request.args.get('acknowledged', 'no').strip()
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()

    open_count = 0
    ack_count = 0
    try:
        with get_db() as conn:
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM atm_anomalies WHERE acknowledged = FALSE")
            open_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM atm_anomalies WHERE acknowledged = TRUE")
            ack_count = cur.fetchone()[0]

            cur.execute("SELECT atm_id, branch FROM atm_locations ORDER BY atm_id")
            all_atms = [{'id': r[0], 'branch': r[1]} for r in cur.fetchall()]

            where_clauses = []
            params = []

            if severity and severity in SEVERITIES:
                where_clauses.append("severity = %s")
                params.append(severity)
            if anomaly_type and anomaly_type in ANOMALY_TYPES:
                where_clauses.append("anomaly_type = %s")
                params.append(anomaly_type)
            if atm_search:
                where_clauses.append("atm_id ILIKE %s")
                params.append(f'%{atm_search}%')
            if acknowledged == 'yes':
                where_clauses.append("acknowledged = TRUE")
            elif acknowledged == 'no':
                where_clauses.append("acknowledged = FALSE")
            if date_from:
                where_clauses.append("detected_at >= %s::timestamp")
                params.append(date_from)
            if date_to:
                where_clauses.append("detected_at <= %s::timestamp + interval '1 day'")
                params.append(date_to)

            where_sql = ' AND '.join(where_clauses) if where_clauses else 'TRUE'

            cur.execute(f"SELECT COUNT(*) FROM atm_anomalies WHERE {where_sql}", params)
            total = cur.fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)

            cur.execute(f"""
                SELECT id, detected_at, atm_id, branch, anomaly_type, severity,
                       card_masked, detail, txn_count, amount,
                       acknowledged, acknowledged_at, acknowledged_by
                FROM atm_anomalies WHERE {where_sql}
                ORDER BY detected_at DESC LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page])
            rows = cur.fetchall()
            cur.close()

        rows = [list(r) for r in rows]
        for r in rows:
            r[1] = to_eat(r[1], '%Y-%m-%d %H:%M')
            r[11] = to_eat(r[11], '%Y-%m-%d %H:%M')
    except Exception as e:
        rows = []
        total = 0
        total_pages = 1
        all_atms = []

    anomalies = [{
        'id': r[0],
        'detected_at': r[1],
        'atm_id': r[2],
        'branch': r[3],
        'anomaly_type': r[4],
        'severity': r[5],
        'card_masked': r[6],
        'detail': r[7],
        'txn_count': r[8],
        'amount': r[9],
        'acknowledged': r[10],
        'acknowledged_at': r[11],
        'acknowledged_by': r[12],
    } for r in rows]

    return render_template('admin_anomalies.html',
                           anomalies=anomalies,
                           all_atms=all_atms,
                           open_count=open_count,
                           ack_count=ack_count,
                           total=total,
                           page=page,
                           total_pages=total_pages,
                           severities=SEVERITIES,
                           anomaly_types=ANOMALY_TYPES,
                           anomaly_labels=ANOMALY_LABELS,
                           severity_colors=SEVERITY_COLORS,
                           type_colors=TYPE_COLORS,
                           filters={
                               'severity': severity,
                               'type': anomaly_type,
                               'atm_id': atm_search,
                               'acknowledged': acknowledged,
                               'from': date_from,
                               'to': date_to,
                           })


@bp.route('/anomalies/acknowledge', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def anomaly_acknowledge():
    data = request.get_json(silent=True)
    anomaly_id = data.get('id') if data else None
    if not anomaly_id:
        return jsonify({'error': True, 'message': 'Missing anomaly ID.'}), 400

    username = session.get('username', 'system')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE atm_anomalies
                SET acknowledged = TRUE,
                    acknowledged_at = NOW(),
                    acknowledged_by = %s
                WHERE id = %s AND acknowledged = FALSE
                RETURNING id, atm_id, anomaly_type
            """, (username, anomaly_id))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        if row:
            log_action('ANOMALY_ACK',
                       f'Anomaly #{row[0]} ({row[2]}) at {row[1]} acknowledged by {username}')
            return jsonify({'success': True, 'id': row[0]})
        return jsonify({'error': True, 'message': 'Anomaly not found or already acknowledged.'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/anomalies/bulk-acknowledge', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def anomaly_bulk_acknowledge():
    data = request.get_json(silent=True)
    if not data or 'ids' not in data or not isinstance(data['ids'], list):
        return jsonify({'error': True, 'message': 'Please provide a list of anomaly IDs.'})

    ids = [i for i in data['ids'] if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]
    if not ids:
        return jsonify({'error': True, 'message': 'No valid anomaly IDs provided.'})

    username = session.get('username', 'system')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"""
                UPDATE atm_anomalies
                SET acknowledged = TRUE,
                    acknowledged_at = NOW(),
                    acknowledged_by = %s
                WHERE id IN ({placeholders}) AND acknowledged = FALSE
            """, [username] + ids)
            updated = cur.rowcount
            conn.commit()
            cur.close()

        log_action('ANOMALY_BULK_ACK',
                   f'{updated} anomalies acknowledged by {username}: {",".join(str(i) for i in ids)}')
        return jsonify({'success': True, 'updated': updated})
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/anomalies/unacknowledge', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def anomaly_unacknowledge():
    data = request.get_json(silent=True)
    anomaly_id = data.get('id') if data else None
    if not anomaly_id:
        return jsonify({'error': True, 'message': 'Missing anomaly ID.'}), 400

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE atm_anomalies
                SET acknowledged = FALSE,
                    acknowledged_at = NULL,
                    acknowledged_by = NULL
                WHERE id = %s AND acknowledged = TRUE
                RETURNING id, atm_id, anomaly_type
            """, (anomaly_id,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        if row:
            log_action('ANOMALY_UNACK',
                       f'Anomaly #{row[0]} ({row[2]}) at {row[1]} unacknowledged')
            return jsonify({'success': True, 'id': row[0]})
        return jsonify({'error': True, 'message': 'Anomaly not found or not acknowledged.'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500
