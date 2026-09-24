"""
blueprints/operations.py
In-portal Operations dashboards — Transaction Monitoring + KPI essentials
rendered natively from Postgres (Phase 2 of single-login UX).

Same queries Grafana runs today (config/grafana/dashboards/*), so the
numbers match what admins see in Grafana. Every query is defensive:
a missing table yields zeros, never a 500.
"""
import logging
from flask import Blueprint, render_template
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from db import get_db

logger = logging.getLogger(__name__)

bp = Blueprint('operations', __name__, url_prefix='/operations')


def _one(sql, params=()):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            cur.close()
            return row
    except Exception as e:
        logger.warning('operations query failed: %s', e)
        return None


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
        logger.warning('operations query failed: %s', e)
        return []


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def dashboard():
    # ── Stat row ──
    cash = _one("""SELECT COALESCE(SUM(amount), 0) FROM atm_transactions
                   WHERE status = 'APPROVED' AND txn_type = 'WITHDRAWAL'
                   AND recorded_at >= CURRENT_DATE""")
    cash_today = float(cash[0]) if cash and cash[0] is not None else 0.0

    rate = _one("""SELECT ROUND(100.0 * SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END)
                   / NULLIF(COUNT(*), 0), 2) FROM atm_transactions
                   WHERE recorded_at >= NOW() - INTERVAL '1 hour'""")
    success_rate = float(rate[0]) if rate and rate[0] is not None else None

    unacked = _one("""SELECT COUNT(*) FROM atm_anomalies
                      WHERE acknowledged = FALSE
                      AND detected_at >= NOW() - INTERVAL '60 minutes'""")
    unacked_count = int(unacked[0]) if unacked else 0

    # ── Fleet overview (health per ATM, last 1h) ──
    fleet = _all("""SELECT l.atm_id, l.branch, l.city, l.vendor,
                           COALESCE(t.total, 0) AS txns,
                           COALESCE(t.approved, 0) AS approved,
                           COALESCE(t.success_rate, 0) AS success_rate,
                           COALESCE(t.cash_dispensed, 0) AS cash,
                           CASE WHEN t.atm_id IS NULL THEN 'No Data'
                                WHEN t.success_rate >= 95 THEN 'Healthy'
                                WHEN t.success_rate >= 80 THEN 'Degraded'
                                ELSE 'Critical' END AS health
                    FROM atm_locations l LEFT JOIN (
                      SELECT atm_id, COUNT(*) AS total,
                        SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                        ROUND(100.0 * SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END)
                          / NULLIF(COUNT(*), 0), 1) AS success_rate,
                        SUM(CASE WHEN status = 'APPROVED' AND txn_type = 'WITHDRAWAL'
                          THEN amount ELSE 0 END) AS cash_dispensed
                      FROM atm_transactions
                      WHERE recorded_at >= NOW() - INTERVAL '1 hour'
                      GROUP BY atm_id) t ON t.atm_id = l.atm_id
                    ORDER BY l.atm_id""")

    # ── Live feed + 24h summary ──
    feed = _all("""SELECT recorded_at, atm_id, branch, txn_type, card_masked,
                          amount, status FROM atm_transactions
                   ORDER BY recorded_at DESC LIMIT 50""")
    summary = _all("""SELECT atm_id, COUNT(*) AS total,
                        SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                        SUM(CASE WHEN status = 'DECLINED' THEN 1 ELSE 0 END) AS declined,
                        SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS errors
                      FROM atm_transactions
                      WHERE recorded_at >= NOW() - INTERVAL '24 hours'
                      GROUP BY atm_id ORDER BY atm_id""")

    # ── Per-hour chart data (last 12h) ──
    hourly = _all("""SELECT date_trunc('hour', recorded_at) AS h,
                        SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                        SUM(CASE WHEN status != 'APPROVED' THEN 1 ELSE 0 END) AS failed
                     FROM atm_transactions
                     WHERE recorded_at >= NOW() - INTERVAL '12 hours'
                     GROUP BY 1 ORDER BY 1""")
    chart = {
        'labels': [str(r['h'])[11:16] for r in hourly],
        'approved': [int(r['approved'] or 0) for r in hourly],
        'failed': [int(r['failed'] or 0) for r in hourly],
    }

    for r in feed:
        if r.get('recorded_at') is not None:
            r['recorded_at'] = str(r['recorded_at'])[:19]

    return render_template('operations.html',
                           cash_today=cash_today, success_rate=success_rate,
                           unacked=unacked_count, fleet=fleet, feed=feed,
                           summary=summary, chart=chart)
