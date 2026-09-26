"""
blueprints/kpi.py
In-portal Management KPI — same queries as the Grafana Management KPI
dashboard (config/grafana/dashboards/*), rendered natively from Postgres
(Phase 2b of single-login UX). Every query is defensive: missing tables
yield empty sections, never a 500.
"""
import logging
from flask import Blueprint, render_template
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from db import get_db

logger = logging.getLogger(__name__)

bp = Blueprint('kpi', __name__, url_prefix='/kpi')


def _all(sql, params=()):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
            return rows
    except Exception as e:
        logger.warning('kpi query failed: %s', e)
        return []


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def dashboard():
    availability = _all("""SELECT d.day AS day,
        ROUND(100.0 * COALESCE(SUM(CASE WHEN s.state = 'IN_SERVICE'
          THEN EXTRACT(EPOCH FROM (LEAST(COALESCE(s.ended_at, NOW()), d.day + INTERVAL '1 day')
            - GREATEST(s.started_at, d.day))) ELSE 0 END), 0)
        / NULLIF((SELECT COUNT(*) FROM atm_locations WHERE status = 'active') * 86400, 0), 2) AS pct
      FROM (SELECT generate_series(DATE_TRUNC('day', NOW() - INTERVAL '7 days'),
        DATE_TRUNC('day', NOW()), INTERVAL '1 day') AS day) d
      LEFT JOIN atm_state_history s
        ON s.started_at < d.day + INTERVAL '1 day'
        AND (s.ended_at IS NULL OR s.ended_at > d.day)
      GROUP BY d.day ORDER BY d.day""")

    ranking = _all("""SELECT atm_id, branch, COUNT(*) AS total,
        ROUND(100.0 * SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) / COUNT(*), 1) AS success,
        COALESCE(SUM(CASE WHEN status = 'APPROVED' AND txn_type = 'WITHDRAWAL'
          THEN amount ELSE 0 END), 0) AS cash
      FROM atm_transactions
      WHERE recorded_at >= NOW() - INTERVAL '7 days'
      GROUP BY atm_id, branch ORDER BY total DESC""")

    errors = _all("""SELECT COALESCE(f.display_name, t.error_code, 'Unknown') AS label,
        COUNT(*) AS cnt,
        ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
      FROM atm_transactions t
      LEFT JOIN fault_type_map f ON t.error_code = f.raw_fault_code
      WHERE status = 'ERROR' AND error_code IS NOT NULL
      AND recorded_at >= NOW() - INTERVAL '7 days'
      GROUP BY COALESCE(f.display_name, t.error_code, 'Unknown')
      ORDER BY COUNT(*) DESC""")

    peak = _all("""SELECT EXTRACT(HOUR FROM recorded_at)::int AS h,
        TO_CHAR(recorded_at, 'Dy') AS day, COUNT(*) AS cnt
      FROM atm_transactions
      WHERE recorded_at >= NOW() - INTERVAL '7 days'
      GROUP BY 1, 2""")
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    heat = {d: [0] * 24 for d in days}
    peak_max = 0
    for r in peak:
        if r['day'] in heat and r['h'] is not None:
            heat[r['day']][int(r['h'])] = int(r['cnt'])
            peak_max = max(peak_max, int(r['cnt']))

    faults = _all("""SELECT l.atm_name AS name, l.branch AS branch, l.atm_id AS atm,
        COALESCE(f.display_name, t.error_code) AS fault, COUNT(*) AS cnt
      FROM atm_transactions t
      JOIN atm_locations l ON t.atm_id = l.atm_id
      LEFT JOIN fault_type_map f ON t.error_code = f.raw_fault_code
      WHERE t.status = 'ERROR' AND t.recorded_at >= NOW() - INTERVAL '7 days'
      GROUP BY l.atm_name, l.branch, l.atm_id, COALESCE(f.display_name, t.error_code)
      ORDER BY COUNT(*) DESC LIMIT 100""")

    chart = {
        'avail_labels': [str(r['day'])[:10] for r in availability],
        'avail': [float(r['pct']) if r['pct'] is not None else 0.0 for r in availability],
        'err_labels': [r['label'] for r in errors],
        'err_data': [int(r['cnt']) for r in errors],
        'fault_labels': [r['label'] for r in errors],
        'fault_data': [int(r['cnt']) for r in errors],
    }

    return render_template('kpi.html', availability=availability, ranking=ranking,
                           errors=errors, heat=heat, days=days, peak_max=peak_max,
                           faults=faults, chart=chart)
