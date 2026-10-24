from flask import Blueprint, request, jsonify, send_file, abort
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from audit import log_action
from report_dates import parse_report_window
import openpyxl, io, os, csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from db import get_db
from helpers import (xl_header, xl_style_row, xl_style_data_rows, xl_autosize,
                     xl_send, xl_finalize,
                     xl_build_sheet, xl_append_section, csv_send, pdf_send, mktable,
                     _pdf_header_block, _period_slug, LOGO_PATH)
import livecache
from datetime import datetime, timedelta, timezone
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                Paragraph, Spacer, HRFlowable, PageBreak, Image)
from reportlab.lib.enums import TA_CENTER

bp = Blueprint('report', __name__)
EAT = timezone(timedelta(hours=3))


def _window():
    try:
        return parse_report_window(
            request.args.get('start'),
            request.args.get('end'),
            request.args.get('days'),
            end_now=request.args.get('end_now') in ('1', 'true', 'yes'),
        )
    except ValueError as exc:
        abort(400, description=str(exc))


# Display units for headers. Applied in Python AFTER fetch — '%' must never
# appear inside a SQL string (psycopg2 mistakes it for a placeholder).
_DISPLAY_UNITS = {
    'Success Rate': 'Success Rate (%)',
    'Uptime': 'Uptime (%)',
    'Coverage': 'Coverage (%)',
}

_VENDOR_SQL = """
    SELECT COALESCE(l.vendor,'Unknown'), COUNT(DISTINCT t.atm_id), COUNT(*),
        SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END),
        SUM(CASE WHEN t.status='DECLINED' THEN 1 ELSE 0 END),
        SUM(CASE WHEN t.status='ERROR' THEN 1 ELSE 0 END),
        ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2),
        COALESCE(SUM(CASE WHEN t.status='APPROVED' AND t.txn_type='WITHDRAWAL' THEN t.amount ELSE 0 END),0)
    FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id
    WHERE t.recorded_at >= %s
      AND t.recorded_at < %s
      AND l.status = 'active'
    GROUP BY l.vendor ORDER BY 3 DESC
"""

# Availability from state history (shared by the availability report and the
# complete report §6). Coverage starts at each ATM's first retained state, so
# a range older than the history reports coverage instead of false downtime.
_AVAIL_SQL = """
    WITH bounds AS (
        SELECT %s::timestamp AS start_ts, %s::timestamp AS end_ts
    ),
    coverage AS (
        SELECT atm_id, MIN(started_at) AS first_state
        FROM atm_state_history
        GROUP BY atm_id
    ),
    eff AS (
        SELECT l.atm_id, l.branch,
            LEAST((SELECT end_ts FROM bounds),
                  GREATEST((SELECT start_ts FROM bounds),
                           COALESCE(c.first_state, (SELECT end_ts FROM bounds)))) AS eff_start,
            (SELECT end_ts FROM bounds) AS eff_end
        FROM atm_locations l
        LEFT JOIN coverage c USING (atm_id)
        WHERE l.status = 'active'{atm_filter}
    ),
    segs AS (
        SELECT e.atm_id, e.branch, h.state,
            EXTRACT(EPOCH FROM (
                COALESCE(LEAST(h.ended_at, e.eff_end), e.eff_end)
                - GREATEST(h.started_at, e.eff_start)
            )) / 3600.0 AS hours
        FROM eff e
        JOIN atm_state_history h ON h.atm_id = e.atm_id
        WHERE h.started_at < e.eff_end
          AND (h.ended_at IS NULL OR h.ended_at > e.eff_start)
    ),
    agg AS (
        SELECT atm_id,
            COALESCE(SUM(hours) FILTER (WHERE state = 'IN_SERVICE'), 0) AS up_hours
        FROM segs GROUP BY atm_id
    ),
    reasons AS (
        SELECT DISTINCT ON (atm_id) atm_id, state AS top_reason
        FROM segs WHERE state <> 'IN_SERVICE' ORDER BY atm_id, hours DESC
    ),
    calc AS (
        SELECT
            e.atm_id,
            e.branch,
            ROUND(COALESCE(a.up_hours, 0), 1) AS active_hours,
            ROUND(EXTRACT(EPOCH FROM (e.eff_end - e.eff_start)) / 3600.0, 1) AS expected_hours,
            ROUND(100.0 * COALESCE(a.up_hours, 0)
                / NULLIF(EXTRACT(EPOCH FROM (e.eff_end - e.eff_start)) / 3600.0, 0), 2) AS uptime_pct,
            ROUND(100.0 * EXTRACT(EPOCH FROM (e.eff_end - e.eff_start)) / 3600.0
                / NULLIF(EXTRACT(EPOCH FROM ((SELECT end_ts FROM bounds) - (SELECT start_ts FROM bounds))) / 3600.0, 0), 1) AS coverage_pct,
            r.top_reason
        FROM eff e
        LEFT JOIN agg a USING (atm_id)
        LEFT JOIN reasons r USING (atm_id)
    )
    SELECT
        atm_id as "ATM_ID",
        branch as "Owner Branch",
        active_hours as "Active (hrs)",
        expected_hours as "Expected (hrs)",
        ROUND(expected_hours - active_hours, 1) as "Downtime (hrs)",
        uptime_pct as "Uptime",
        CASE
            WHEN expected_hours <= 0 THEN 'No data'
            WHEN uptime_pct >= 99.9 THEN 'Excellent'
            WHEN uptime_pct >= 99.0 THEN 'Good'
            WHEN uptime_pct >= 95.0 THEN 'Acceptable'
            ELSE 'Below Target'
        END as "SLA Status",
        COALESCE(top_reason,
            CASE WHEN expected_hours <= 0 THEN 'No data'
                 WHEN active_hours >= expected_hours THEN 'None'
                 ELSE 'No data' END) as "Top Downtime Reason",
        coverage_pct as "Coverage"
    FROM calc ORDER BY uptime_pct ASC NULLS LAST
"""


def _avail_sql(atm='all'):
    return _AVAIL_SQL.format(
        atm_filter=" AND l.atm_id = %s" if atm != 'all' else "")


@bp.route('/api/atms')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def api_atms():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id, branch FROM atm_locations ORDER BY atm_id")
            rows = cur.fetchall(); cur.close()
        return jsonify([{'id': r[0], 'branch': r[1]} for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/stats')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def api_stats():
    try:
        def _run():
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*),
                        ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1),
                    COALESCE(SUM(CASE WHEN t.status='APPROVED' AND t.txn_type='WITHDRAWAL'
                        AND t.recorded_at >= date_trunc('day', NOW() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'
                        THEN t.amount ELSE 0 END),0),
                        (SELECT COUNT(*) FROM atm_locations WHERE status = 'active')
                    FROM atm_transactions_visible t
                    JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= NOW() - INTERVAL '24 hours'
                      AND l.status = 'active'
                """)
                r = cur.fetchone(); cur.close()
            # The window is 24 hours, and the tile reads "Transactions (24h)".
            # A 7-day window was correct arithmetic and useless as a headline:
            # with a constant-rate stream, rows enter the window and fall out of
            # it at the same rate, so the number never moves. Measured on UAT:
            # 60 arriving and 60 leaving per 10 minutes, dead level. It read as
            # frozen, and a monitoring KPI that appears frozen trains people to
            # ignore it. 24h tracks what the ATM is doing now, and matches the
            # "Cash Dispensped Today" tile beside it.
            return {'total_txns': int(r[0] or 0), 'success_rate': float(r[1]) if r[1] is not None else 0.0, 'cash_today': float(r[2] or 0), 'atm_count': int(r[3] or 0)}
        # Shared across viewers (12s TTL under the 15s frontend poll).
        return jsonify(livecache.get_or_compute('api_stats', 12, _run))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _fetch_query(sql, params=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows


@bp.route('/api/charts')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def api_charts():
    try:
        TXN_VOL = """
            SELECT DATE(recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa') as day, COUNT(*) as cnt
            FROM atm_transactions_visible
            WHERE recorded_at >= NOW() - INTERVAL '7 days'
            GROUP BY DATE(recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa')
            ORDER BY day
        """
        SR = """
            SELECT DATE(recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa') as day,
                ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as rate
            FROM atm_transactions_visible
            WHERE recorded_at >= NOW() - INTERVAL '7 days'
            GROUP BY DATE(recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa')
            ORDER BY day
        """
        # The chart must agree with the ATM list, which overlays live Zabbix
        # state on the persisted state. Reading only atm_current_state made a
        # GRG that was live HARDWARE_FAULT appear as stale UNREACHABLE in the
        # doughnut. Fetch the fleet, then overlay live state below.
        STATUS_ATMS = """
            SELECT l.atm_id, COALESCE(l.vendor, 'NCR') as vendor,
                   COALESCE(s.state, 'UNKNOWN') as state
            FROM atm_locations l
            LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id
            WHERE LOWER(COALESCE(l.status, 'active')) = 'active'
        """
        VENDOR = """
            SELECT COALESCE(l.vendor, 'Unknown') as vendor,
                ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as rate
            FROM atm_transactions_visible t
            JOIN atm_locations l ON t.atm_id = l.atm_id
            WHERE t.recorded_at >= NOW() - INTERVAL '7 days'
              AND l.status = 'active'
            GROUP BY l.vendor
            ORDER BY 2 DESC
        """
        CASH = """
            SELECT DATE(recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa') as day,
                COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL' THEN amount ELSE 0 END),0) as cash
            FROM atm_transactions_visible
            WHERE recorded_at >= NOW() - INTERVAL '7 days'
            GROUP BY DATE(recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa')
            ORDER BY day
        """

        with ThreadPoolExecutor(max_workers=5) as ex:
            f_txn = ex.submit(_fetch_query, TXN_VOL)
            f_sr = ex.submit(_fetch_query, SR)
            f_status = ex.submit(_fetch_query, STATUS_ATMS)
            f_vendor = ex.submit(_fetch_query, VENDOR)
            f_cash = ex.submit(_fetch_query, CASH)

        txn_rows = f_txn.result()
        sr_rows = f_sr.result()
        status_atms = f_status.result()
        vendor_rows = f_vendor.result()
        cash_rows = f_cash.result()

        def _status_payload():
            live = {}
            try:
                from blueprints.admin import batch_fetch_live_states
                live = batch_fetch_live_states(
                    [{'atm_id': r[0], 'vendor': r[1]} for r in status_atms])
            except Exception as e:
                print(f"[CHARTS] live status overlay failed: {e!r}")
            counts = {}
            for atm_id, _vendor, stored in status_atms:
                hit = live.get(atm_id)
                state = hit[0] if hit else (stored or 'UNKNOWN')
                counts[state] = counts.get(state, 0) + 1
            rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            return {'labels': [f'{s} ({n})' for s, n in rows],
                    'data': [n for _s, n in rows]}

        # Shared across viewers (25s TTL under the 30s chart poll).
        return jsonify(livecache.get_or_compute('api_charts', 25, lambda: {
            'txn_volume': {
                'labels': [str(r[0]) for r in txn_rows],
                'data': [int(r[1]) for r in txn_rows],
            },
            'success_rate': {
                'labels': [str(r[0]) for r in sr_rows],
                'data': [float(r[1]) for r in sr_rows],
            },
            'atm_status': _status_payload(),
            'vendor_comparison': {
                'labels': [str(r[0]) for r in vendor_rows],
                'data': [float(r[1]) for r in vendor_rows],
            },
            'cash_trend': {
                'labels': [str(r[0]) for r in cash_rows],
                'data': [float(r[1]) for r in cash_rows],
            },
        }))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── TRANSACTION SUMMARY ────────────────────────────────────────────────────────

@bp.route('/report/transaction/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_transaction(fmt):
    win = _window()
    atm = request.args.get('atm', 'all')

    with get_db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT
                t.atm_id                                                      AS "ATM_ID",
                l.branch                                                      AS "Owner Branch",
                COUNT(*)                                                      AS "Total Txns",
                SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)         AS "Approved",
                SUM(CASE WHEN t.status='DECLINED' THEN 1 ELSE 0 END)         AS "Declined",
                SUM(CASE WHEN t.status='ERROR'    THEN 1 ELSE 0 END)         AS "Errors",
                ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)
                    /NULLIF(COUNT(*),0),2)                                    AS "Success Rate",
                COALESCE(SUM(CASE WHEN t.status='APPROVED' AND txn_type='WITHDRAWAL'
                    THEN amount ELSE 0 END),0)                               AS "Cash Dispensed ETB"
            FROM atm_transactions_visible t
            JOIN atm_locations l ON t.atm_id = l.atm_id
            WHERE t.recorded_at >= %s
              AND t.recorded_at < %s
              AND l.status = 'active'
        """
        params = [win.start_utc, win.end_utc]
        if atm != 'all':
            sql += " AND t.atm_id = %s"
            params.append(atm)
        sql += " GROUP BY t.atm_id, l.branch ORDER BY t.atm_id"

        cur.execute(sql, params)
        rows = cur.fetchall(); headers = [d[0] for d in cur.description]
        cur.close()

    # NOTE: '%' must never appear inside the SQL above (psycopg2 parses it
    # as a placeholder) — pretty display units are applied here instead.
    headers = ['S.N'] + [_DISPLAY_UNITS.get(h, h) for h in headers]
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]

    title = 'Transaction Summary Report'
    subtitle = 'How many transactions each ATM handled, and how many succeeded.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Transactions', title, win.days, atm, headers, rows,
                       subtitle=subtitle, period=win.label)
        log_action('EXPORT', f'Transaction report (excel) - period={win.label}, atm={atm}')
        return xl_send(wb, 'Transactions', period=win.label)
    elif fmt == 'pdf':
        total = sum(r[3] for r in rows)
        approved = sum(r[4] for r in rows)
        declined = sum(r[5] for r in rows)
        errors_cnt = sum(r[6] for r in rows)
        cash = sum(r[8] for r in rows)
        rate = round(approved / total * 100, 1) if total else 0
        kpis = [
            ('Total Transactions', f'{total:,}'),
            ('Success Rate', f'{rate}%'),
            ('Declined / Errors', f'{declined + errors_cnt:,}'),
            ('Cash Dispensed', f'ETB {cash:,.0f}'),
        ]
        log_action('EXPORT', f'Transaction report (pdf) - period={win.label}, atm={atm}')
        return pdf_send(title, headers, rows, win.days, atm, 'Transactions',
                        kpis=kpis, subtitle=subtitle, period=win.label)
    else:
        log_action('EXPORT', f'Transaction report (csv) - period={win.label}, atm={atm}')
        return csv_send(headers, rows, 'Transactions', title=title, days=win.days,
                        atm=atm, subtitle=subtitle, period=win.label)


# ─── CASH LEVEL ─────────────────────────────────────────────────────────────────

@bp.route('/report/cash/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_cash(fmt):
    win = _window()
    atm = request.args.get('atm', 'all')

    with get_db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT
                t.atm_id                             AS "ATM_ID",
                l.branch                             AS "Owner Branch",
                COALESCE(SUM(amount),0)              AS "Total Dispensed ETB",
                COALESCE(ROUND(AVG(amount),2),0)     AS "Avg Withdrawal ETB",
                COUNT(*)                             AS "Withdrawal Count",
                COALESCE(MAX(amount),0)              AS "Largest Withdrawal ETB"
            FROM atm_transactions_visible t
            JOIN atm_locations l ON t.atm_id = l.atm_id
            WHERE t.status='APPROVED' AND t.txn_type='WITHDRAWAL'
            AND t.recorded_at >= %s
            AND t.recorded_at < %s
              AND l.status = 'active'
        """
        params = [win.start_utc, win.end_utc]
        if atm != 'all':
            sql += " AND t.atm_id = %s"
            params.append(atm)
        sql += " GROUP BY t.atm_id, l.branch ORDER BY 3 DESC"

        cur.execute(sql, params)
        rows = cur.fetchall(); headers = [d[0] for d in cur.description]
        cur.close()

    headers = ['S.N'] + headers
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]

    title = 'Cash Level & Dispensing Report'
    subtitle = 'Cash dispensed per ATM from approved withdrawals.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Cash', title, win.days, atm, headers, rows,
                       subtitle=subtitle, period=win.label)
        log_action('EXPORT', f'Cash report (excel) - period={win.label}, atm={atm}')
        return xl_send(wb, 'Cash', period=win.label)
    elif fmt == 'pdf':
        total_dispensed = sum(r[3] for r in rows)
        total_withdrawals = sum(r[5] for r in rows)
        avg_withdrawal = round(total_dispensed / total_withdrawals, 0) if total_withdrawals else 0
        largest = max((r[6] for r in rows), default=0)
        kpis = [
            ('Total Dispensed', f'ETB {total_dispensed:,.0f}'),
            ('Total Withdrawals', f'{total_withdrawals:,}'),
            ('Avg Withdrawal', f'ETB {avg_withdrawal:,.0f}'),
            ('Largest Withdrawal', f'ETB {largest:,.0f}'),
        ]
        log_action('EXPORT', f'Cash report (pdf) - period={win.label}, atm={atm}')
        return pdf_send(title, headers, rows, win.days, atm, 'Cash', kpis=kpis,
                        subtitle=subtitle, period=win.label)
    else:
        log_action('EXPORT', f'Cash report (csv) - period={win.label}, atm={atm}')
        return csv_send(headers, rows, 'Cash', title=title, days=win.days, atm=atm,
                        subtitle=subtitle, period=win.label)


# In-memory cache of error_code -> human label, seeded from the DB
# fault_type_map (vendor-agnostic) so every surface (dashboards, reports,
# detail page) shows the same readable fault name. Falls back to a small
# built-in map if the table is unreachable.
_FAULT_LABELS = {
    '3A7F': 'Dispenser',
    'B2C1': 'Card Reader',
    '44AA': 'Network',
    '9E3D': 'Dispenser',
    'FF01': 'Receipt Printer',
}


def _load_fault_labels():
    """Populate _FAULT_LABELS from fault_type_map (one query, cached)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT raw_fault_code, display_name "
                        "FROM fault_type_map")
            for code, label in cur.fetchall():
                if code and label:
                    _FAULT_LABELS[code] = label
            cur.close()
    except Exception:
        pass


def _describe_error(code):
    if not _FAULT_LABELS or '9E3D' not in _FAULT_LABELS:
        _load_fault_labels()
    return _FAULT_LABELS.get(code, code or 'Unknown')

# ─── ERROR & INCIDENT ────────────────────────────────────────────────────────────

@bp.route('/report/error/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_error(fmt):
    win = _window()
    atm = request.args.get('atm', 'all')

    with get_db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT
                t.atm_id      AS "ATM_ID",
                l.branch      AS "Owner Branch",
                t.error_code  AS "Error Code",
                COUNT(*)      AS "Occurrences",
                TO_CHAR(MIN(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') AS "First Seen",
                TO_CHAR(MAX(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') AS "Last Seen"
            FROM atm_transactions_visible t
            JOIN atm_locations l ON t.atm_id = l.atm_id
            WHERE t.status='ERROR' AND t.error_code IS NOT NULL
            AND t.recorded_at >= %s
            AND t.recorded_at < %s
              AND l.status = 'active'
        """
        params = [win.start_utc, win.end_utc]
        if atm != 'all':
            sql += " AND t.atm_id = %s"
            params.append(atm)
        sql += " GROUP BY t.atm_id, l.branch, t.error_code ORDER BY 4 DESC"

        cur.execute(sql, params)
        rows = cur.fetchall(); headers = [d[0] for d in cur.description]
        cur.close()

    headers = ['S.N'] + headers
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]

    # Augment rows with error-code description
    desc_ndx = headers.index('Error Code') + 1
    headers.insert(desc_ndx, 'Description')
    rows = [list(r) for r in rows]
    for r in rows:
        r.insert(desc_ndx, _describe_error(r[desc_ndx - 1] if desc_ndx - 1 < len(r) else ''))

    title = 'Error & Incident Report'
    subtitle = 'Which faults occurred, how often, and where.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Errors', title, win.days, atm, headers, rows,
                       subtitle=subtitle, period=win.label)
        log_action('EXPORT', f'Error report (excel) - period={win.label}, atm={atm}')
        return xl_send(wb, 'Errors', period=win.label)
    elif fmt == 'pdf':
        total_occ = sum(r[5] for r in rows)
        unique_codes = len({r[3] for r in rows})
        unique_atms = len({r[1] for r in rows})
        code_counts = Counter(r[3] for r in rows)
        most_common = code_counts.most_common(1)[0][0] if code_counts else 'N/A'
        kpis = [
            ('Total Errors', f'{total_occ:,}'),
            ('Unique Error Codes', str(unique_codes)),
            ('ATMs Affected', str(unique_atms)),
            ('Most Common', most_common),
        ]
        log_action('EXPORT', f'Error report (pdf) - period={win.label}, atm={atm}')
        return pdf_send(title, headers, rows, win.days, atm, 'Errors', kpis=kpis,
                        subtitle=subtitle, period=win.label)
    else:
        log_action('EXPORT', f'Error report (csv) - period={win.label}, atm={atm}')
        return csv_send(headers, rows, 'Errors', title=title, days=win.days, atm=atm,
                        subtitle=subtitle, period=win.label)


# ─── ATM PERFORMANCE ────────────────────────────────────────────────────────────

@bp.route('/report/performance/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_performance(fmt):
    win = _window()

    with get_db() as conn:
        cur = conn.cursor()
        sql = """
            WITH stats AS (
                SELECT t.atm_id, l.branch,
                    COUNT(*) AS total,
                    ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)
                        /NULLIF(COUNT(*),0),2) AS rate,
                    ROUND(COUNT(*)/%s::numeric,1) AS daily
                FROM atm_transactions_visible t
                JOIN atm_locations l ON t.atm_id = l.atm_id
                WHERE t.recorded_at >= %s
                  AND t.recorded_at < %s
                  AND l.status = 'active'
                GROUP BY t.atm_id, l.branch
            ),
            peaks AS (
                SELECT DISTINCT ON (t.atm_id) t.atm_id,
                    EXTRACT(HOUR FROM t.recorded_at) AS hr, COUNT(*) AS cnt
                FROM atm_transactions_visible t
                JOIN atm_locations l ON t.atm_id = l.atm_id
                WHERE t.recorded_at >= %s
                  AND t.recorded_at < %s
                  AND l.status = 'active'
                GROUP BY t.atm_id, EXTRACT(HOUR FROM t.recorded_at)
                ORDER BY t.atm_id, cnt DESC
            )
            SELECT s.atm_id AS "ATM_ID", s.branch AS "Owner Branch",
                s.total AS "Total Transactions",
                s.rate  AS "Success Rate",
                s.daily AS "Avg Daily Txns",
                CONCAT(p.hr::int,':00-',(p.hr::int+1),':00') AS "Peak Hour",
                RANK() OVER (ORDER BY s.total DESC) AS "Rank"
            FROM stats s JOIN peaks p ON s.atm_id=p.atm_id
            ORDER BY s.total DESC
        """
        cur.execute(sql, (win.days, win.start_utc, win.end_utc,
                          win.start_utc, win.end_utc))
        rows = cur.fetchall(); headers = [d[0] for d in cur.description]
        cur.close()

    headers = ['S.N'] + [_DISPLAY_UNITS.get(h, h) for h in headers]
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]

    title = 'ATM Performance Report'
    subtitle = 'ATMs ranked by volume, with peak hours.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Performance', title, win.days, 'all', headers, rows,
                       subtitle=subtitle, period=win.label)
        log_action('EXPORT', f'Performance report (excel) - period={win.label}')
        return xl_send(wb, 'Performance', period=win.label)
    elif fmt == 'pdf':
        total_txns = sum(r[3] for r in rows)
        avg_rate = round(sum(r[4] for r in rows) / len(rows), 1) if rows else 0
        top_atm = rows[0][1] if rows else 'N/A'
        atm_count = len(rows)
        kpis = [
            ('Total Transactions', f'{total_txns:,}'),
            ('Avg Success Rate', f'{avg_rate}%'),
            ('ATMs Ranked', str(atm_count)),
            ('Top Performer', str(top_atm)),
        ]
        log_action('EXPORT', f'Performance report (pdf) - period={win.label}')
        return pdf_send(title, headers, rows, win.days, 'all', 'Performance',
                        kpis=kpis, subtitle=subtitle, period=win.label)
    else:
        log_action('EXPORT', f'Performance report (csv) - period={win.label}')
        return csv_send(headers, rows, 'Performance', title=title, days=win.days,
                        atm='all', subtitle=subtitle, period=win.label)


# ─── VENDOR PERFORMANCE ───────────────────────────────────────────────────────────

@bp.route('/report/vendor/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_vendor(fmt):
    win = _window()

    with get_db() as conn:
        cur = conn.cursor()
        sql = """
            SELECT
                COALESCE(l.vendor, 'Unknown')                                 AS "Vendor",
                COUNT(DISTINCT t.atm_id)                                       AS "ATMs",
                COUNT(*)                                                       AS "Total Txns",
                SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)          AS "Approved",
                SUM(CASE WHEN t.status='DECLINED' THEN 1 ELSE 0 END)          AS "Declined",
                SUM(CASE WHEN t.status='ERROR'    THEN 1 ELSE 0 END)          AS "Errors",
                ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)
                    /NULLIF(COUNT(*),0),2)                                     AS "Success Rate",
                ROUND(COUNT(*)/NULLIF(COUNT(DISTINCT t.atm_id),0)/%s::numeric,1)
                                                                              AS "Avg Daily Txns/ATM",
                COALESCE(SUM(CASE WHEN t.status='APPROVED' AND t.txn_type='WITHDRAWAL'
                    THEN t.amount ELSE 0 END),0)                              AS "Cash Dispensed ETB"
            FROM atm_transactions_visible t
            JOIN atm_locations l ON t.atm_id = l.atm_id
            WHERE t.recorded_at >= %s
              AND t.recorded_at < %s
            GROUP BY l.vendor
            ORDER BY 3 DESC
        """
        cur.execute(sql, (win.days, win.start_utc, win.end_utc))
        rows = cur.fetchall(); headers = [d[0] for d in cur.description]
        cur.close()

    headers = ['S.N'] + [_DISPLAY_UNITS.get(h, h) for h in headers]
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]

    title = 'Vendor Performance Report'
    subtitle = 'NCR vs GRG head-to-head on volume and success rate.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Vendor', title, win.days, 'all', headers, rows,
                       subtitle=subtitle, period=win.label)
        log_action('EXPORT', f'Vendor report (excel) - period={win.label}')
        return xl_send(wb, 'Vendor', period=win.label)
    elif fmt == 'pdf':
        total_txns = sum(r[3] for r in rows)
        approved = sum(r[4] for r in rows)
        rate = round(approved / total_txns * 100, 1) if total_txns else 0
        cash = sum(r[9] for r in rows)
        best = max(rows, key=lambda r: (r[7] or 0))[1] if rows else 'N/A'
        kpis = [
            ('Vendors', str(len(rows))),
            ('Total Transactions', f'{total_txns:,}'),
            ('Overall Success Rate', f'{rate}%'),
            ('Top Vendor (by success)', str(best)),
        ]
        log_action('EXPORT', f'Vendor report (pdf) - period={win.label}')
        return pdf_send(title, headers, rows, win.days, 'all', 'Vendor', kpis=kpis,
                        subtitle=subtitle, period=win.label)
    else:
        log_action('EXPORT', f'Vendor report (csv) - period={win.label}')
        return csv_send(headers, rows, 'Vendor', title=title, days=win.days, atm='all',
                        subtitle=subtitle, period=win.label)


# ─── ATM AVAILABILITY ───────────────────────────────────────────────────────────

@bp.route('/report/availability/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_availability(fmt):
    win = _window()
    atm = request.args.get('atm', 'all')

    with get_db() as conn:
        cur = conn.cursor()
        # True availability from state history (BRD 6.1): integrate each ATM's
        # state segments over the window. Coverage starts at the ATM's first
        # retained state so an older range cannot be reported as 100% downtime.
        params = [win.start_utc, win.end_utc]
        if atm != 'all':
            params.append(atm)
        cur.execute(_avail_sql(atm), tuple(params))
        rows = cur.fetchall(); headers = [d[0] for d in cur.description]
        cur.close()

    headers = ['S.N'] + [_DISPLAY_UNITS.get(h, h) for h in headers]
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]

    title = 'ATM Availability Report'
    subtitle = 'Which ATMs were down and for how long — worst first.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet('Availability')
        xl_header(ws, title, win.days, atm, ncols=len(headers),
                  subtitle=subtitle, period=win.label)
        ws.append(headers)
        xl_style_row(ws, ws.max_row, len(headers))
        data_start = ws.max_row + 1
        for row in rows:
            ws.append(list(row))
        xl_style_data_rows(ws, data_start, len(headers))
        colors_map = {
            'Excellent': 'C6EFCE',
            'Good': 'C6EFCE',
            'Acceptable': 'FFEB9C',
            'Below Target': 'FFC7CE'
        }
        sla_col = headers.index('SLA Status') + 1
        for idx, row in enumerate(rows):
            sla_val = str(row[7] or '') if row else ''
            fill_color = colors_map.get(sla_val, 'FFFFFF')
            cell = ws.cell(row=data_start + idx, column=sla_col)
            cell.fill = openpyxl.styles.PatternFill('solid', fgColor=fill_color)
        xl_autosize(ws)
        xl_finalize(ws, data_start - 1, len(headers))
        log_action('EXPORT', f'Availability report (excel) - period={win.label}, atm={atm}')
        return xl_send(wb, 'Availability', period=win.label)
    elif fmt == 'pdf':
        uptimes = [r[6] for r in rows if r[6] is not None]
        avg_uptime = round(sum(uptimes) / len(uptimes), 2) if uptimes else 0
        excellent = sum(1 for r in rows if r[7] == 'Excellent')
        below_target = sum(1 for r in rows if r[7] == 'Below Target')
        kpis = [
            ('Avg Uptime', f'{avg_uptime}%'),
            ('Excellent SLA', str(excellent)),
            ('Below Target', str(below_target)),
            ('ATMs Monitored', str(len(rows))),
        ]
        log_action('EXPORT', f'Availability report (pdf) - period={win.label}, atm={atm}')
        return pdf_send(title, headers, rows, win.days, atm, 'Availability',
                        kpis=kpis, subtitle=subtitle, period=win.label)
    else:
        log_action('EXPORT', f'Availability report (csv) - period={win.label}, atm={atm}')
        return csv_send(headers, rows, 'Availability', title=title, days=win.days,
                        atm=atm, subtitle=subtitle, period=win.label)


# ─── DAILY ATM OUTAGE REPORT ─────────────────────────────────────────────────
# Mirrors the ATM team's manual template (Daily ATM Report .xlsx): one row per
# down ATM, same column order. Location + outage days are automatic;
# Causes / Action Taken / Call ID stay blank for the team to type, exactly as
# they do today (only GLPI-linked tickets could prefill those — follow-up).
def _glpi_outage_links(atm_ids):
    """Map atm_id -> (ticket_id, cause, action) from the latest open GLPI
    tickets mentioning the ATM. One session, followups fetched only for
    matches. Never raises — any failure means the report falls back to
    suspected causes and blank action/Call ID."""
    import re
    out = {}
    wanted = {a for a in atm_ids if a}
    if not wanted:
        return out
    try:
        from blueprints.incidents import _session, _close, _tickets, _subitems
        token = _session()
        if not token:
            return out
        try:
            tickets = _tickets(token) or []

            def _text(html):
                t = re.sub(r'<[^>]+>', ' ', html or '')
                return re.sub(r'\s+', ' ', t).strip()

            for t in tickets:
                try:
                    if int(t.get('status', 0)) == 6:
                        continue  # Closed
                except (TypeError, ValueError):
                    continue
                tid = t.get('id')
                blob = f"{t.get('name', '')} {t.get('content', '')}"
                hit = next((a for a in wanted if a in blob), None)
                if not hit:
                    continue
                title = _text(t.get('name')) or f'Ticket #{tid}'
                action = ''
                for f in _subitems(token, tid, 'ITILFollowup'):
                    c = _text(f.get('content'))
                    if c:
                        action = c
                for s in _subitems(token, tid, 'ITILSolution'):
                    c = _text(s.get('content'))
                    if c:
                        action = c
                if len(action) > 300:
                    action = action[:297] + '...'
                out[hit] = (tid, title, action)
                wanted.discard(hit)
                if not wanted:
                    break
        finally:
            _close(token)
    except Exception:
        pass
    return out
@bp.route('/report/outage/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_outage(fmt):
    atm = request.args.get('atm', 'all')

    with get_db() as conn:
        cur = conn.cursor()
        # Down ATMs + cause evidence in one pass:
        #  e = latest ERROR txn (7d) mapped through fault_type_map,
        #  w = latest approved withdrawal (cash-out inference needs its age).
        sql = """
            SELECT l.atm_id, l.atm_name, l.district,
                ROUND(EXTRACT(EPOCH FROM (NOW() - s.state_changed_at)) / 86400.0, 1),
                l.branch,
                TRIM(COALESCE(l.vendor, '') || ' ' || COALESCE(l.model, '')),
                l.ip_address,
                s.state, s.last_seen,
                e.error_code, f.display_name,
                EXTRACT(EPOCH FROM (NOW() - e.recorded_at)) / 3600.0,
                EXTRACT(EPOCH FROM (NOW() - w.recorded_at)) / 3600.0
            FROM atm_current_state s
            JOIN atm_locations l ON l.atm_id = s.atm_id
            LEFT JOIN LATERAL (
                SELECT t.error_code, t.recorded_at FROM atm_transactions_visible t
                WHERE t.atm_id = s.atm_id AND t.status = 'ERROR'
                  AND t.error_code IS NOT NULL
                  AND t.recorded_at >= NOW() - INTERVAL '7 days'
                ORDER BY t.recorded_at DESC LIMIT 1
            ) e ON true
            LEFT JOIN fault_type_map f
                ON f.raw_fault_code = e.error_code AND f.vendor = l.vendor
            LEFT JOIN LATERAL (
                SELECT t.recorded_at FROM atm_transactions_visible t
                WHERE t.atm_id = s.atm_id AND t.status = 'APPROVED'
                  AND t.txn_type = 'WITHDRAWAL'
                ORDER BY t.recorded_at DESC LIMIT 1
            ) w ON true
            WHERE l.status = 'active' AND s.state <> 'IN_SERVICE'
        """ + (" AND s.atm_id = %s" if atm != 'all' else "") + """
            ORDER BY 4 DESC
        """
        cur.execute(sql, (atm,) if atm != 'all' else ())
        db_rows = cur.fetchall()
        cur.close()

    def _suspect(r):
        # r: (..., state, last_seen, err_code, display, err_age_h, wd_age_h)
        state, seen, code, display, err_age, wd_age = r[7], r[8], r[9], r[10], r[11], r[12]
        if state == 'UNREACHABLE':
            return 'Suspected Network/Power outage (host unreachable)'
        if code and display:
            return f'Suspected {display} (last fault {code})'
        if code:
            return f'Suspected fault {code} (unmapped code)'
        if state == 'OUT_OF_SERVICE':
            seen_age = (datetime.now(seen.tzinfo) - seen).total_seconds() / 3600.0 \
                if seen else None
            if seen_age is not None and seen_age < 2 \
                    and (wd_age is None or wd_age > 48):
                return 'Suspected Cash Out (silent, reachable, no faults)'
        return ''

    headers = ['S.N', 'ATM_ID', 'ATM_Name', 'District', 'Causes for failure',
               'Action Taken', 'Outage in days', 'Owner branch', 'Type of ATM',
               'IP Address', 'Call ID']
    rows = [[i + 1, r[0], r[1], r[2], _suspect(r), '', r[3], r[4], r[5], r[6], '-']
            for i, r in enumerate(db_rows)]

    # Where an open GLPI ticket names the ATM, its title/followups replace the
    # guess: confirmed cause + real action + ticket id as Call ID.
    links = _glpi_outage_links([r[0] for r in db_rows])
    for row in rows:
        link = links.get(row[1])
        if link:
            tid, cause, action = link
            row[4] = cause
            row[5] = action
            row[10] = tid

    title = 'Daily ATM Outage Report'
    subtitle = 'Down ATMs right now — location and outage days filled, causes left for the team.'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet('Daily Outage')
        xl_header(ws, title, 1, atm, ncols=len(headers),
                  subtitle=subtitle, period='Current snapshot')
        ws.append(headers)
        xl_style_row(ws, ws.max_row, len(headers))
        # Match the team's template: periwinkle header, dark text.
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=ws.max_row, column=col)
            cell.fill = openpyxl.styles.PatternFill('solid', fgColor='95B3D7')
            cell.font = openpyxl.styles.Font(bold=True, color='0F172A', size=10)
        data_start = ws.max_row + 1
        for row in rows:
            ws.append(list(row))
        xl_style_data_rows(ws, data_start, len(headers))
        xl_autosize(ws)
        xl_finalize(ws, data_start - 1, len(headers))
        log_action('EXPORT', f'Outage report (excel) - atm={atm}')
        return xl_send(wb, 'Daily_Outage')
    elif fmt == 'pdf':
        longest = max((r[6] for r in rows), default=0)
        avg = round(sum(r[6] for r in rows) / len(rows), 1) if rows else 0
        kpis = [
            ('Down ATMs', str(len(rows))),
            ('Longest Outage', f'{longest}d'),
            ('Avg Outage', f'{avg}d'),
            ('Generated', datetime.now(EAT).strftime('%Y-%m-%d')),
        ]
        log_action('EXPORT', f'Outage report (pdf) - atm={atm}')
        return pdf_send(title, headers, rows, 1, atm, 'Daily_Outage', kpis=kpis,
                        subtitle=subtitle, period='Current snapshot')
    else:
        log_action('EXPORT', f'Outage report (csv) - atm={atm}')
        return csv_send(headers, rows, 'Daily_Outage', title=title, days=1,
                        atm=atm, subtitle=subtitle, period='Current snapshot')


# ─── COMPLETE MANAGEMENT REPORT ─────────────────────────────────────────────────

@bp.route('/report/full/<fmt>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def report_full(fmt):
    win = _window()
    atm = request.args.get('atm', 'all')
    days = win.days
    with get_db() as conn:

        if fmt == 'excel':
            wb = openpyxl.Workbook(); ws = wb.active
            ws.title = 'Complete Report'
            xl_header(ws, 'Complete Management Report', days, atm,
                      subtitle='All six reports in one file — the numbers behind the morning meeting.',
                      period=win.label)

            # 1. Transactions
            cur = conn.cursor()
            sql1 = """
                SELECT t.atm_id, l.branch, COUNT(*),
                    SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN t.status='DECLINED' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN t.status='ERROR'    THEN 1 ELSE 0 END),
                    ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2),
                    COALESCE(SUM(CASE WHEN t.status='APPROVED' AND t.txn_type='WITHDRAWAL' THEN t.amount ELSE 0 END),0)
                FROM atm_transactions_visible t
                JOIN atm_locations l ON t.atm_id = l.atm_id
                WHERE t.recorded_at >= %s AND t.recorded_at < %s
                  AND l.status = 'active'
            """
            p1 = [win.start_utc, win.end_utc]
            if atm != 'all': sql1 += " AND t.atm_id = %s"; p1.append(atm)
            sql1 += " GROUP BY t.atm_id, l.branch ORDER BY t.atm_id"
            cur.execute(sql1, p1)
            h1 = ['ATM_ID', 'Owner Branch', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB']
            xl_append_section(ws, '1. Transaction Summary', h1, cur.fetchall())
            cur.close()

            # 2. Cash
            cur = conn.cursor()
            sql2 = """
                SELECT t.atm_id, l.branch, COALESCE(SUM(t.amount),0),
                    COALESCE(ROUND(AVG(t.amount),2),0), COUNT(*), COALESCE(MAX(t.amount),0)
                FROM atm_transactions_visible t
                JOIN atm_locations l ON t.atm_id = l.atm_id
                WHERE t.status='APPROVED' AND t.txn_type='WITHDRAWAL'
                AND t.recorded_at >= %s AND t.recorded_at < %s
                  AND l.status = 'active'
            """
            p2 = [win.start_utc, win.end_utc]
            if atm != 'all': sql2 += " AND t.atm_id = %s"; p2.append(atm)
            sql2 += " GROUP BY t.atm_id, l.branch ORDER BY 3 DESC"
            cur.execute(sql2, p2)
            h2 = ['ATM_ID', 'Owner Branch', 'Total Dispensed ETB', 'Avg Withdrawal ETB', 'Withdrawal Count', 'Largest Withdrawal ETB']
            xl_append_section(ws, '2. Cash Level & Dispensing', h2, cur.fetchall())
            cur.close()

            # 3. Errors
            cur = conn.cursor()
            sql3 = """
                SELECT t.atm_id, l.branch, t.error_code, COUNT(*),
                    TO_CHAR(MIN(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI'),
                    TO_CHAR(MAX(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI')
                FROM atm_transactions_visible t
                JOIN atm_locations l ON t.atm_id = l.atm_id
                WHERE t.status='ERROR' AND t.error_code IS NOT NULL
                AND t.recorded_at >= %s AND t.recorded_at < %s
                  AND l.status = 'active'
            """
            p3 = [win.start_utc, win.end_utc]
            if atm != 'all': sql3 += " AND t.atm_id = %s"; p3.append(atm)
            sql3 += " GROUP BY t.atm_id, l.branch, t.error_code ORDER BY 4 DESC"
            cur.execute(sql3, p3)
            rows3 = cur.fetchall()
            h3 = ['ATM_ID', 'Owner Branch', 'Error Code', 'Description', 'Occurrences', 'First Seen', 'Last Seen']
            rows3_desc = [list(r) for r in rows3]
            for r in rows3_desc:
                r.insert(3, _describe_error(r[2]))
            xl_append_section(ws, '3. Error & Incident Log', h3, rows3_desc)
            cur.close()

            # 4. Performance
            cur = conn.cursor()
            sql4 = """
                WITH stats AS (
                    SELECT t.atm_id, l.branch, COUNT(*) AS total,
                        ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) AS rate,
                        ROUND(COUNT(*)/%s::numeric,1) AS daily
                    FROM atm_transactions_visible t
                    JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= %s AND t.recorded_at < %s
                      AND l.status = 'active'
                    GROUP BY t.atm_id, l.branch
                ),
                peaks AS (
                    SELECT DISTINCT ON (t.atm_id) t.atm_id,
                        EXTRACT(HOUR FROM t.recorded_at) AS hr, COUNT(*) AS cnt
                    FROM atm_transactions_visible t
                    JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= %s AND t.recorded_at < %s
                      AND l.status = 'active'
                    GROUP BY t.atm_id, EXTRACT(HOUR FROM t.recorded_at)
                    ORDER BY t.atm_id, cnt DESC
                )
                SELECT s.atm_id, s.branch, s.total, s.rate, s.daily,
                    CONCAT(p.hr::int,':00-',(p.hr::int+1),':00'),
                    RANK() OVER (ORDER BY s.total DESC)
                FROM stats s JOIN peaks p ON s.atm_id=p.atm_id ORDER BY s.total DESC
            """
            cur.execute(sql4, (win.days, win.start_utc, win.end_utc,
                                      win.start_utc, win.end_utc))
            h4 = ['ATM_ID', 'Owner Branch', 'Total Transactions', 'Success Rate %', 'Avg Daily Txns', 'Peak Hour', 'Rank']
            xl_append_section(ws, '4. ATM Performance Metrics', h4, cur.fetchall())
            cur.close()

            # 5. Vendor Performance
            cur = conn.cursor()
            cur.execute(_VENDOR_SQL, (win.start_utc, win.end_utc))
            h5 = ['Vendor', 'ATMs', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB']
            xl_append_section(ws, '5. Vendor Performance', h5, cur.fetchall())
            cur.close()

            # 6. Availability
            cur = conn.cursor()
            cur.execute(_avail_sql(), (win.start_utc, win.end_utc))
            h6 = ['ATM_ID', 'Owner Branch', 'Active Hours', 'Expected Hours', 'Downtime Hours', 'Uptime %', 'SLA Status', 'Top Downtime Reason', 'Coverage %']
            xl_append_section(ws, '6. Availability', h6, cur.fetchall())
            cur.close()

            ws.freeze_panes = 'A6'
            xl_autosize(ws)

            log_action('EXPORT', f'Complete report (excel) - period={win.label}, atm={atm}')
            return xl_send(wb, 'Complete_Report', period=win.label)

        elif fmt == 'csv':
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['DASHEN BANK S.C. — ATM MONITORING SYSTEM'])
            w.writerow(['COMPLETE MANAGEMENT REPORT'])
            w.writerow([f'Period: {win.label}', f'Generated: {datetime.now(EAT).strftime("%Y-%m-%d %H:%M")} EAT'])
            w.writerow([])

            sections = [
                ('1. TRANSACTION SUMMARY',
                 "SELECT t.atm_id, l.branch, COUNT(*), SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END), SUM(CASE WHEN t.status='DECLINED' THEN 1 ELSE 0 END), SUM(CASE WHEN t.status='ERROR' THEN 1 ELSE 0 END), ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2), COALESCE(SUM(CASE WHEN t.status='APPROVED' AND t.txn_type='WITHDRAWAL' THEN t.amount ELSE 0 END),0) FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id WHERE t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active' GROUP BY t.atm_id, l.branch ORDER BY t.atm_id",
                 [win.start_utc, win.end_utc],
                 ['ATM', 'Branch', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB']),
                ('2. CASH LEVEL & DISPENSING',
                 "SELECT t.atm_id, l.branch, COALESCE(SUM(t.amount),0), COALESCE(ROUND(AVG(t.amount),2),0), COUNT(*), COALESCE(MAX(t.amount),0) FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id WHERE t.status='APPROVED' AND t.txn_type='WITHDRAWAL' AND t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active' GROUP BY t.atm_id, l.branch ORDER BY 3 DESC",
                 [win.start_utc, win.end_utc],
                 ['ATM', 'Branch', 'Total Dispensed ETB', 'Avg Withdrawal ETB', 'Withdrawal Count', 'Largest Withdrawal ETB']),
                ('3. ERROR & INCIDENT LOG',
                 "SELECT t.atm_id, l.branch, t.error_code, COUNT(*), TO_CHAR(MIN(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI'), TO_CHAR(MAX(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id WHERE t.status='ERROR' AND t.error_code IS NOT NULL AND t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active' GROUP BY t.atm_id, l.branch, t.error_code ORDER BY 4 DESC",
                 [win.start_utc, win.end_utc],
                 ['ATM', 'Branch', 'Error Code', 'Occurrences', 'First Seen', 'Last Seen']),
            ]

            for sec_title, sql, params, headers in sections:
                cur = conn.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall()
                cur.close()
                if 'ERROR' in sec_title:
                    headers = headers[:3] + ['Description'] + headers[3:]
                    rows = [list(r) for r in rows]
                    for r in rows:
                        r.insert(3, _describe_error(r[2]))
                w.writerow([sec_title])
                w.writerow(headers)
                w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows])
                w.writerow([])

            cur = conn.cursor()
            sql4 = """
                WITH stats AS (
                    SELECT t.atm_id, l.branch, COUNT(*) AS total,
                        ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) AS rate,
                        ROUND(COUNT(*)/%s::numeric,1) AS daily
                    FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active'
                    GROUP BY t.atm_id, l.branch
                ),
                peaks AS (
                    SELECT DISTINCT ON (t.atm_id) t.atm_id, EXTRACT(HOUR FROM t.recorded_at) AS hr, COUNT(*) AS cnt
                    FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active'
                    GROUP BY t.atm_id, EXTRACT(HOUR FROM t.recorded_at) ORDER BY t.atm_id, cnt DESC
                )
                SELECT s.atm_id, s.branch, s.total, s.rate, s.daily,
                    CONCAT(p.hr::int,':00-',(p.hr::int+1),':00'), RANK() OVER (ORDER BY s.total DESC)
                FROM stats s JOIN peaks p ON s.atm_id=p.atm_id ORDER BY s.total DESC
            """
            cur.execute(sql4, (win.days, win.start_utc, win.end_utc,
                                      win.start_utc, win.end_utc))
            rows4 = cur.fetchall(); cur.close()
            w.writerow(['4. ATM PERFORMANCE METRICS'])
            w.writerow(['ATM', 'Branch', 'Total Transactions', 'Success Rate %', 'Avg Daily Txns', 'Peak Hour', 'Rank'])
            w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows4])
            w.writerow([])

            cur = conn.cursor()
            cur.execute(_VENDOR_SQL, [win.start_utc, win.end_utc])
            rows5 = cur.fetchall(); cur.close()
            w.writerow(['5. VENDOR PERFORMANCE'])
            w.writerow(['Vendor', 'ATMs', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB'])
            w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows5])
            w.writerow([])
            cur = conn.cursor()
            cur.execute(_avail_sql(), [win.start_utc, win.end_utc])
            rows6 = cur.fetchall(); cur.close()
            w.writerow(['6. AVAILABILITY'])
            w.writerow(['ATM', 'Branch', 'Active Hours', 'Expected Hours', 'Downtime Hours', 'Uptime %', 'SLA Status', 'Top Downtime Reason', 'Coverage %'])
            w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows6])
            w.writerow([])
            w.writerow(['Dashen Bank ATM Monitoring System | Confidential Management Report'])

            log_action('EXPORT', f'Complete report (csv) - period={win.label}, atm={atm}')
            buf.seek(0)
            return send_file(
                io.BytesIO(buf.getvalue().encode('utf-8-sig')),
                mimetype='text/csv', as_attachment=True,
                download_name=f'Dashen_ATM_Complete_Report_{datetime.now(EAT).strftime("%Y%m%d")}{_period_slug(win.label)}.csv'
            )

        else:
            buf = io.BytesIO()
            doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                    rightMargin=1.5 * cm, leftMargin=1.5 * cm,
                                    topMargin=2 * cm, bottomMargin=1.5 * cm)
            style_bank = ParagraphStyle('_h1', fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=4)
            style_sys  = ParagraphStyle('_h2', fontSize=11, leading=14, alignment=TA_CENTER, spaceAfter=8)
            style_full = ParagraphStyle('_h3', fontSize=14, leading=18, alignment=TA_CENTER, spaceAfter=8)
            style_meta = ParagraphStyle('_h4', fontSize=9, leading=12, alignment=TA_CENTER)
            style_sec  = ParagraphStyle('_h_sec', fontSize=12, leading=15, spaceBefore=12, spaceAfter=8)

            story = []
            if os.path.exists(LOGO_PATH):
                img = Image(LOGO_PATH, width=6 * cm, height=1.8 * cm, kind='proportional')
                img.hAlign = 'CENTER'
                story.append(img)
                story.append(Spacer(1, 0.5 * cm))

            story += [
                Paragraph('<b><font color="#012169">DASHEN BANK S.C.</font></b>', style_bank),
                Paragraph('<font color="#273274">ATM MONITORING SYSTEM</font>', style_sys),
                HRFlowable(width='60%', thickness=2.5, color=colors.HexColor('#FDD79A'), spaceAfter=12),
                Paragraph('<b><font color="#273274">COMPLETE MANAGEMENT REPORT</font></b>', style_full),
                Paragraph(f'<font color="#64748B">Period: {win.label}  |  Generated: {datetime.now(EAT).strftime("%Y-%m-%d %H:%M")} EAT</font>', style_meta),
                Spacer(1, 1.2 * cm)
            ]

            cur = conn.cursor()

            # 1. Transaction Summary
            s1_sql = "SELECT t.atm_id, l.branch, COUNT(*), SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END), SUM(CASE WHEN t.status='DECLINED' THEN 1 ELSE 0 END), SUM(CASE WHEN t.status='ERROR' THEN 1 ELSE 0 END), ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2), COALESCE(SUM(CASE WHEN t.status='APPROVED' AND t.txn_type='WITHDRAWAL' THEN t.amount ELSE 0 END),0) FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id WHERE t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active' GROUP BY t.atm_id, l.branch ORDER BY t.atm_id"
            cur.execute(s1_sql, [win.start_utc, win.end_utc])
            story += [Paragraph('<b><font color="#012169">1. Transaction Summary</font></b>', style_sec),
                      mktable(['ATM', 'Branch', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB'], cur.fetchall()),
                      Spacer(1, 0.8 * cm)]

            # 2. Cash Level
            s2_sql = "SELECT t.atm_id, l.branch, COALESCE(SUM(t.amount),0), COALESCE(ROUND(AVG(t.amount),2),0), COUNT(*), COALESCE(MAX(t.amount),0) FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id WHERE t.status='APPROVED' AND t.txn_type='WITHDRAWAL' AND t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active' GROUP BY t.atm_id, l.branch ORDER BY 3 DESC"
            cur.execute(s2_sql, [win.start_utc, win.end_utc])
            story += [Paragraph('<b><font color="#012169">2. Cash Level & Dispensing</font></b>', style_sec),
                      mktable(['ATM', 'Branch', 'Total Dispensed ETB', 'Avg Withdrawal ETB', 'Withdrawal Count', 'Largest Withdrawal ETB'], cur.fetchall()),
                      Spacer(1, 0.8 * cm)]

            # 3. Error & Incident
            s3_sql = "SELECT t.atm_id, l.branch, t.error_code, COUNT(*), TO_CHAR(MIN(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI'), TO_CHAR(MAX(t.recorded_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id WHERE t.status='ERROR' AND t.error_code IS NOT NULL AND t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active' GROUP BY t.atm_id, l.branch, t.error_code ORDER BY 4 DESC"
            cur.execute(s3_sql, [win.start_utc, win.end_utc])
            res3 = cur.fetchall()
            if res3:
                res3_desc = [list(r) for r in res3]
                for r in res3_desc:
                    r.insert(3, _describe_error(r[2]))
                story += [Paragraph('<b><font color="#012169">3. Error & Incident Log</font></b>', style_sec),
                          mktable(['ATM', 'Branch', 'Error Code', 'Description', 'Occurrences', 'First Seen', 'Last Seen'], res3_desc),
                          Spacer(1, 0.8 * cm)]

            # 4. Performance Metrics
            s4_sql = """
                WITH stats AS (
                    SELECT t.atm_id, l.branch, COUNT(*) AS total,
                        ROUND(100.0*SUM(CASE WHEN t.status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) AS rate,
                        ROUND(COUNT(*)/%s::numeric,1) AS daily
                    FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active'
                    GROUP BY t.atm_id, l.branch
                ),
                peaks AS (
                    SELECT DISTINCT ON (t.atm_id) t.atm_id, EXTRACT(HOUR FROM t.recorded_at) AS hr, COUNT(*) AS cnt
                    FROM atm_transactions_visible t JOIN atm_locations l ON t.atm_id = l.atm_id
                    WHERE t.recorded_at >= %s AND t.recorded_at < %s AND l.status = 'active'
                    GROUP BY t.atm_id, EXTRACT(HOUR FROM t.recorded_at) ORDER BY t.atm_id, cnt DESC
                )
                SELECT s.atm_id, s.branch, s.total, s.rate, s.daily,
                    CONCAT(p.hr::int,':00-',(p.hr::int+1),':00'), RANK() OVER (ORDER BY s.total DESC)
                FROM stats s JOIN peaks p ON s.atm_id=p.atm_id ORDER BY s.total DESC
            """
            cur.execute(s4_sql, (win.days, win.start_utc, win.end_utc, win.start_utc, win.end_utc))
            res4 = cur.fetchall()
            if res4:
                story += [PageBreak(),
                          Paragraph('<b><font color="#012169">4. ATM Performance Metrics</font></b>', style_sec),
                          mktable(['ATM', 'Branch', 'Total Transactions', 'Success Rate %', 'Avg Daily Txns', 'Peak Hour', 'Rank'], res4),
                          Spacer(1, 0.8 * cm)]

            # 5. Vendor Performance
            cur.execute(_VENDOR_SQL, [win.start_utc, win.end_utc])
            res5 = cur.fetchall()
            if res5:
                story += [Paragraph('<b><font color="#012169">5. Vendor Performance</font></b>', style_sec),
                          mktable(['Vendor', 'ATMs', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB'], res5),
                          Spacer(1, 0.8 * cm)]

            # 6. Availability
            cur.execute(_avail_sql(), [win.start_utc, win.end_utc])
            res6 = cur.fetchall()
            if res6:
                story += [Paragraph('<b><font color="#012169">6. Availability</font></b>', style_sec),
                          mktable(['ATM', 'Branch', 'Active Hours', 'Expected Hours', 'Downtime Hours', 'Uptime %', 'SLA Status', 'Top Downtime Reason', 'Coverage %'], res6),
                          Spacer(1, 0.8 * cm)]

            story.append(Paragraph(
                f'<font size="8" color="#64748B">Dashen Bank ATM Monitoring System  |  Confidential Management Report  |  {datetime.now(EAT).strftime("%Y-%m-%d")}</font>',
                style_meta
            ))

            cur.close()
            log_action('EXPORT', f'Complete report (pdf) - period={win.label}, atm={atm}')
            doc.build(story); buf.seek(0)
            return send_file(buf, mimetype='application/pdf', as_attachment=True,
                             download_name=f'Dashen_ATM_Complete_Report_{datetime.now(EAT).strftime("%Y%m%d")}{_period_slug(win.label)}.pdf')



