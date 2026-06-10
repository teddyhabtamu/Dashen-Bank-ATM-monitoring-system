from flask import Blueprint, request, jsonify, send_file
import openpyxl, io, os, csv
from db import get_db
from helpers import (xl_header, xl_style_row, xl_style_data_rows, xl_autosize,
                     xl_send, xl_build_sheet, xl_append_section, csv_send, pdf_send, mktable,
                     _pdf_header_block, LOGO_PATH)
from datetime import datetime
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                Paragraph, Spacer, HRFlowable, PageBreak, Image)
from reportlab.lib.enums import TA_CENTER

bp = Blueprint('report', __name__)


@bp.route('/api/atms')
def api_atms():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT DISTINCT atm_id, branch FROM atm_transactions ORDER BY atm_id")
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify([{'id': r[0], 'branch': r[1]} for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/stats')
def api_stats():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*),
                ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1),
                COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL'
                    AND recorded_at>=CURRENT_DATE THEN amount ELSE 0 END),0),
                (SELECT COUNT(DISTINCT atm_id) FROM atm_transactions)
            FROM atm_transactions
            WHERE recorded_at >= NOW() - INTERVAL '7 days'
        """)
        r = cur.fetchone(); cur.close(); conn.close()
        return jsonify({'total_txns': int(r[0]), 'success_rate': float(r[1]), 'cash_today': float(r[2]), 'atm_count': int(r[3])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── TRANSACTION SUMMARY ────────────────────────────────────────────────────────

@bp.route('/report/transaction/<fmt>')
def report_transaction(fmt):
    days = int(request.args.get('days', 7))
    atm  = request.args.get('atm', 'all')
    conn = get_db(); cur = conn.cursor()

    sql = """
        SELECT
            atm_id                                                        AS "ATM",
            branch                                                        AS "Branch",
            COUNT(*)                                                      AS "Total Txns",
            SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)           AS "Approved",
            SUM(CASE WHEN status='DECLINED' THEN 1 ELSE 0 END)           AS "Declined",
            SUM(CASE WHEN status='ERROR'    THEN 1 ELSE 0 END)           AS "Errors",
            ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)
                /NULLIF(COUNT(*),0),2)                                    AS "Success Rate",
            COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL'
                THEN amount ELSE 0 END),0)                               AS "Cash Dispensed ETB"
        FROM atm_transactions
        WHERE recorded_at >= NOW() - INTERVAL %s
    """
    params = [f"{days} days"]
    if atm != 'all':
        sql += " AND atm_id = %s"
        params.append(atm)
    sql += " GROUP BY atm_id, branch ORDER BY atm_id"

    cur.execute(sql, params)
    rows = cur.fetchall(); headers = [d[0] for d in cur.description]
    cur.close(); conn.close()

    title = 'Transaction Summary Report'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Transactions', title, days, atm, headers, rows)
        return xl_send(wb, 'Transactions')
    elif fmt == 'pdf':
        return pdf_send(title, headers, rows, days, atm, 'Transactions')
    else:
        return csv_send(headers, rows, 'Transactions', title=title, days=days, atm=atm)


# ─── CASH LEVEL ─────────────────────────────────────────────────────────────────

@bp.route('/report/cash/<fmt>')
def report_cash(fmt):
    days = int(request.args.get('days', 7))
    atm  = request.args.get('atm', 'all')
    conn = get_db(); cur = conn.cursor()

    sql = """
        SELECT
            atm_id                             AS "ATM",
            branch                             AS "Branch",
            COALESCE(SUM(amount),0)            AS "Total Dispensed ETB",
            COALESCE(ROUND(AVG(amount),2),0)   AS "Avg Withdrawal ETB",
            COUNT(*)                           AS "Withdrawal Count",
            COALESCE(MAX(amount),0)            AS "Largest Withdrawal ETB"
        FROM atm_transactions
        WHERE status='APPROVED' AND txn_type='WITHDRAWAL'
        AND recorded_at >= NOW() - INTERVAL %s
    """
    params = [f"{days} days"]
    if atm != 'all':
        sql += " AND atm_id = %s"
        params.append(atm)
    sql += " GROUP BY atm_id, branch ORDER BY 3 DESC"

    cur.execute(sql, params)
    rows = cur.fetchall(); headers = [d[0] for d in cur.description]
    cur.close(); conn.close()

    title = 'Cash Level & Dispensing Report'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Cash', title, days, atm, headers, rows)
        return xl_send(wb, 'Cash')
    elif fmt == 'pdf':
        return pdf_send(title, headers, rows, days, atm, 'Cash')
    else:
        return csv_send(headers, rows, 'Cash', title=title, days=days, atm=atm)


# ─── ERROR & INCIDENT ────────────────────────────────────────────────────────────

@bp.route('/report/error/<fmt>')
def report_error(fmt):
    days = int(request.args.get('days', 7))
    atm  = request.args.get('atm', 'all')
    conn = get_db(); cur = conn.cursor()

    sql = """
        SELECT
            atm_id      AS "ATM",
            branch      AS "Branch",
            error_code  AS "Error Code",
            COUNT(*)    AS "Occurrences",
            TO_CHAR(MIN(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') AS "First Seen",
            TO_CHAR(MAX(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') AS "Last Seen"
        FROM atm_transactions
        WHERE status='ERROR' AND error_code IS NOT NULL
        AND recorded_at >= NOW() - INTERVAL %s
    """
    params = [f"{days} days"]
    if atm != 'all':
        sql += " AND atm_id = %s"
        params.append(atm)
    sql += " GROUP BY atm_id, branch, error_code ORDER BY 4 DESC"

    cur.execute(sql, params)
    rows = cur.fetchall(); headers = [d[0] for d in cur.description]
    cur.close(); conn.close()

    title = 'Error & Incident Report'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Errors', title, days, atm, headers, rows)
        return xl_send(wb, 'Errors')
    elif fmt == 'pdf':
        return pdf_send(title, headers, rows, days, atm, 'Errors')
    else:
        return csv_send(headers, rows, 'Errors', title=title, days=days, atm=atm)


# ─── ATM PERFORMANCE ────────────────────────────────────────────────────────────

@bp.route('/report/performance/<fmt>')
def report_performance(fmt):
    days = int(request.args.get('days', 7))
    conn = get_db(); cur = conn.cursor()

    sql = """
        WITH stats AS (
            SELECT atm_id, branch,
                COUNT(*) AS total,
                ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)
                    /NULLIF(COUNT(*),0),2) AS rate,
                ROUND(COUNT(*)/%s::numeric,1) AS daily
            FROM atm_transactions
            WHERE recorded_at >= NOW() - INTERVAL %s
            GROUP BY atm_id, branch
        ),
        peaks AS (
            SELECT DISTINCT ON (atm_id) atm_id,
                EXTRACT(HOUR FROM recorded_at) AS hr, COUNT(*) AS cnt
            FROM atm_transactions
            WHERE recorded_at >= NOW() - INTERVAL %s
            GROUP BY atm_id, EXTRACT(HOUR FROM recorded_at)
            ORDER BY atm_id, cnt DESC
        )
        SELECT s.atm_id AS "ATM", s.branch AS "Branch",
            s.total AS "Total Transactions",
            s.rate  AS "Success Rate",
            s.daily AS "Avg Daily Txns",
            CONCAT(p.hr::int,':00-',(p.hr::int+1),':00') AS "Peak Hour",
            RANK() OVER (ORDER BY s.total DESC) AS "Rank"
        FROM stats s JOIN peaks p ON s.atm_id=p.atm_id
        ORDER BY s.total DESC
    """
    days_interval = f"{days} days"
    cur.execute(sql, (days, days_interval, days_interval))
    rows = cur.fetchall(); headers = [d[0] for d in cur.description]
    cur.close(); conn.close()

    title = 'ATM Performance Report'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        xl_build_sheet(wb, 'Performance', title, days, 'all', headers, rows)
        return xl_send(wb, 'Performance')
    elif fmt == 'pdf':
        return pdf_send(title, headers, rows, days, 'all', 'Performance')
    else:
        return csv_send(headers, rows, 'Performance', title=title, days=days, atm='all')


# ─── ATM AVAILABILITY ───────────────────────────────────────────────────────────

@bp.route('/report/availability/<fmt>')
def report_availability(fmt):
    days = int(request.args.get('days', 7))
    atm  = request.args.get('atm', 'all')
    conn = get_db(); cur = conn.cursor()

    sql = """
        WITH hourly AS (
            SELECT
                t.atm_id,
                l.branch,
                DATE_TRUNC('hour', recorded_at) as hour,
                COUNT(*) as txn_count
            FROM atm_transactions t
            JOIN atm_locations l ON t.atm_id = l.atm_id
            WHERE recorded_at >= NOW() - INTERVAL %s
            GROUP BY t.atm_id, l.branch, DATE_TRUNC('hour', recorded_at)
        ),
        total_hours AS (
            SELECT %s * 24 as expected_hours
        ),
        uptime AS (
            SELECT
                atm_id,
                branch,
                COUNT(*) as active_hours,
                (SELECT expected_hours FROM total_hours) as expected_hours,
                ROUND(100.0 * COUNT(*) / (SELECT expected_hours FROM total_hours), 2) as uptime_pct
            FROM hourly
            GROUP BY atm_id, branch
        )
        SELECT
            atm_id as "ATM ID",
            branch as "Branch",
            active_hours as "Active Hours",
            expected_hours as "Expected Hours",
            (expected_hours - active_hours) as "Downtime Hours",
            uptime_pct as "Uptime",
            CASE
                WHEN uptime_pct >= 99.9 THEN 'Excellent'
                WHEN uptime_pct >= 99.0 THEN 'Good'
                WHEN uptime_pct >= 95.0 THEN 'Acceptable'
                ELSE 'Below Target'
            END as "SLA Status"
        FROM uptime
        ORDER BY uptime_pct DESC
    """
    cur.execute(sql, (f"{days} days", days))
    rows = cur.fetchall()
    headers = [d[0] for d in cur.description]
    cur.close(); conn.close()

    title = 'ATM Availability Report'
    if fmt == 'excel':
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet('Availability')
        xl_header(ws, title, days, atm)
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
        for idx, row in enumerate(rows):
            sla_val = str(row[-1] or '') if row else ''
            fill_color = colors_map.get(sla_val, 'FFFFFF')
            cell = ws.cell(row=data_start + idx, column=len(headers))
            cell.fill = openpyxl.styles.PatternFill('solid', fgColor=fill_color)
        xl_autosize(ws)
        return xl_send(wb, 'Availability')
    elif fmt == 'pdf':
        return pdf_send(title, headers, rows, days, atm, 'Availability')
    else:
        return csv_send(headers, rows, 'Availability', title=title, days=days, atm=atm)


# ─── COMPLETE MANAGEMENT REPORT ─────────────────────────────────────────────────

@bp.route('/report/full/<fmt>')
def report_full(fmt):
    days = int(request.args.get('days', 7))
    atm  = request.args.get('atm', 'all')
    conn = get_db()
    interval_str = f"{days} days"

    if fmt == 'excel':
        wb = openpyxl.Workbook(); ws = wb.active
        ws.title = 'Complete Report'
        xl_header(ws, 'Complete Management Report', days, atm)

        # 1. Transactions
        cur = conn.cursor()
        sql1 = """
            SELECT atm_id, branch, COUNT(*),
                SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='DECLINED' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='ERROR'    THEN 1 ELSE 0 END),
                ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2),
                COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL' THEN amount ELSE 0 END),0)
            FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s
        """
        p1 = [interval_str]
        if atm != 'all': sql1 += " AND atm_id = %s"; p1.append(atm)
        sql1 += " GROUP BY atm_id, branch ORDER BY atm_id"
        cur.execute(sql1, p1)
        h1 = ['ATM', 'Branch', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB']
        xl_append_section(ws, '1. Transaction Summary', h1, cur.fetchall())
        cur.close()

        # 2. Cash
        cur = conn.cursor()
        sql2 = """
            SELECT atm_id, branch, COALESCE(SUM(amount),0),
                COALESCE(ROUND(AVG(amount),2),0), COUNT(*), COALESCE(MAX(amount),0)
            FROM atm_transactions WHERE status='APPROVED' AND txn_type='WITHDRAWAL'
            AND recorded_at >= NOW() - INTERVAL %s
        """
        p2 = [interval_str]
        if atm != 'all': sql2 += " AND atm_id = %s"; p2.append(atm)
        sql2 += " GROUP BY atm_id, branch ORDER BY 3 DESC"
        cur.execute(sql2, p2)
        h2 = ['ATM', 'Branch', 'Total Dispensed ETB', 'Avg Withdrawal ETB', 'Withdrawal Count', 'Largest Withdrawal ETB']
        xl_append_section(ws, '2. Cash Level & Dispensing', h2, cur.fetchall())
        cur.close()

        # 3. Errors
        cur = conn.cursor()
        sql3 = """
            SELECT atm_id, branch, error_code, COUNT(*),
                TO_CHAR(MIN(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI'),
                TO_CHAR(MAX(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI')
            FROM atm_transactions WHERE status='ERROR' AND error_code IS NOT NULL
            AND recorded_at >= NOW() - INTERVAL %s
        """
        p3 = [interval_str]
        if atm != 'all': sql3 += " AND atm_id = %s"; p3.append(atm)
        sql3 += " GROUP BY atm_id, branch, error_code ORDER BY 4 DESC"
        cur.execute(sql3, p3)
        h3 = ['ATM', 'Branch', 'Error Code', 'Occurrences', 'First Seen', 'Last Seen']
        xl_append_section(ws, '3. Error & Incident Log', h3, cur.fetchall())
        cur.close()

        # 4. Performance
        cur = conn.cursor()
        sql4 = """
            WITH stats AS (
                SELECT atm_id, branch, COUNT(*) AS total,
                    ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) AS rate,
                    ROUND(COUNT(*)/%s::numeric,1) AS daily
                FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s
                GROUP BY atm_id, branch
            ),
            peaks AS (
                SELECT DISTINCT ON (atm_id) atm_id,
                    EXTRACT(HOUR FROM recorded_at) AS hr, COUNT(*) AS cnt
                FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s
                GROUP BY atm_id, EXTRACT(HOUR FROM recorded_at)
                ORDER BY atm_id, cnt DESC
            )
            SELECT s.atm_id, s.branch, s.total, s.rate, s.daily,
                CONCAT(p.hr::int,':00-',(p.hr::int+1),':00'),
                RANK() OVER (ORDER BY s.total DESC)
            FROM stats s JOIN peaks p ON s.atm_id=p.atm_id ORDER BY s.total DESC
        """
        cur.execute(sql4, (days, interval_str, interval_str))
        h4 = ['ATM', 'Branch', 'Total Transactions', 'Success Rate %', 'Avg Daily Txns', 'Peak Hour', 'Rank']
        xl_append_section(ws, '4. ATM Performance Metrics', h4, cur.fetchall())
        cur.close()

        xl_autosize(ws)

        conn.close()
        return xl_send(wb, 'Complete_Report')

    elif fmt == 'csv':
        # Complete report as a single CSV with section separators
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['DASHEN BANK S.C. — ATM MONITORING SYSTEM'])
        w.writerow(['COMPLETE MANAGEMENT REPORT'])
        w.writerow([f'Period: Last {days} days', f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} EAT'])
        w.writerow([])

        sections = [
            ('1. TRANSACTION SUMMARY',
             "SELECT atm_id, branch, COUNT(*), SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='DECLINED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END), ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2), COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL' THEN amount ELSE 0 END),0) FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch ORDER BY atm_id",
             [interval_str],
             ['ATM', 'Branch', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB']),
            ('2. CASH LEVEL & DISPENSING',
             "SELECT atm_id, branch, COALESCE(SUM(amount),0), COALESCE(ROUND(AVG(amount),2),0), COUNT(*), COALESCE(MAX(amount),0) FROM atm_transactions WHERE status='APPROVED' AND txn_type='WITHDRAWAL' AND recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch ORDER BY 3 DESC",
             [interval_str],
             ['ATM', 'Branch', 'Total Dispensed ETB', 'Avg Withdrawal ETB', 'Withdrawal Count', 'Largest Withdrawal ETB']),
            ('3. ERROR & INCIDENT LOG',
             "SELECT atm_id, branch, error_code, COUNT(*), TO_CHAR(MIN(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI'), TO_CHAR(MAX(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') FROM atm_transactions WHERE status='ERROR' AND error_code IS NOT NULL AND recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch, error_code ORDER BY 4 DESC",
             [interval_str],
             ['ATM', 'Branch', 'Error Code', 'Occurrences', 'First Seen', 'Last Seen']),
        ]

        for sec_title, sql, params, headers in sections:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            w.writerow([sec_title])
            w.writerow(headers)
            w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows])
            w.writerow([])

        # Performance section
        cur = conn.cursor()
        sql4 = """
            WITH stats AS (
                SELECT atm_id, branch, COUNT(*) AS total,
                    ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) AS rate,
                    ROUND(COUNT(*)/%s::numeric,1) AS daily
                FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch
            ),
            peaks AS (
                SELECT DISTINCT ON (atm_id) atm_id, EXTRACT(HOUR FROM recorded_at) AS hr, COUNT(*) AS cnt
                FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s
                GROUP BY atm_id, EXTRACT(HOUR FROM recorded_at) ORDER BY atm_id, cnt DESC
            )
            SELECT s.atm_id, s.branch, s.total, s.rate, s.daily,
                CONCAT(p.hr::int,':00-',(p.hr::int+1),':00'), RANK() OVER (ORDER BY s.total DESC)
            FROM stats s JOIN peaks p ON s.atm_id=p.atm_id ORDER BY s.total DESC
        """
        cur.execute(sql4, (days, interval_str, interval_str))
        rows4 = cur.fetchall(); cur.close()
        w.writerow(['4. ATM PERFORMANCE METRICS'])
        w.writerow(['ATM', 'Branch', 'Total Transactions', 'Success Rate %', 'Avg Daily Txns', 'Peak Hour', 'Rank'])
        w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows4])
        w.writerow([])
        w.writerow(['Dashen Bank ATM Monitoring System | Confidential Management Report'])

        conn.close()
        buf.seek(0)
        return send_file(
            io.BytesIO(buf.getvalue().encode('utf-8-sig')),
            mimetype='text/csv', as_attachment=True,
            download_name=f'Dashen_ATM_Complete_Report_{datetime.now().strftime("%Y%m%d")}.csv'
        )

    else:  # PDF Multi-section Management Report
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                rightMargin=1.5 * cm, leftMargin=1.5 * cm,
                                topMargin=2 * cm, bottomMargin=1.5 * cm)
        P = lambda txt, **kw: Paragraph(txt, ParagraphStyle('_p', **kw))

        story = []
        if os.path.exists(LOGO_PATH):
            img = Image(LOGO_PATH, width=6 * cm, height=1.8 * cm, kind='proportional')
            img.hAlign = 'CENTER'
            story.append(img)
            story.append(Spacer(1, 0.5 * cm))

        story += [
            P('<font color="#0F2557" size="18"><b>DASHEN BANK S.C.</b></font>', alignment=TA_CENTER, spaceAfter=6),
            P('<font color="#0F2557" size="13"><b>ATM MONITORING SYSTEM — COMPLETE MANAGEMENT REPORT</b></font>', alignment=TA_CENTER, spaceAfter=6),
            HRFlowable(width='60%', thickness=2, color=colors.HexColor('#C9A84C'), spaceAfter=10),
            P(f'<font color="#64748B" size="10">Period: Last {days} days  |  Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} EAT</font>', alignment=TA_CENTER),
            Spacer(1, 1 * cm)
        ]

        cur = conn.cursor()

        # 1. Transaction Summary
        s1_sql = "SELECT atm_id, branch, COUNT(*), SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='DECLINED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END), ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2), COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL' THEN amount ELSE 0 END),0) FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch ORDER BY atm_id"
        cur.execute(s1_sql, [interval_str])
        story += [P('<font color="#0F2557" size="12"><b>1. Transaction Summary</b></font>', spaceBefore=12, spaceAfter=8),
                  mktable(['ATM', 'Branch', 'Total Txns', 'Approved', 'Declined', 'Errors', 'Success Rate %', 'Cash Dispensed ETB'], cur.fetchall()),
                  Spacer(1, 0.8 * cm)]

        # 2. Cash Level
        s2_sql = "SELECT atm_id, branch, COALESCE(SUM(amount),0), COALESCE(ROUND(AVG(amount),2),0), COUNT(*), COALESCE(MAX(amount),0) FROM atm_transactions WHERE status='APPROVED' AND txn_type='WITHDRAWAL' AND recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch ORDER BY 3 DESC"
        cur.execute(s2_sql, [interval_str])
        story += [P('<font color="#0F2557" size="12"><b>2. Cash Level & Dispensing</b></font>', spaceBefore=12, spaceAfter=8),
                  mktable(['ATM', 'Branch', 'Total Dispensed ETB', 'Avg Withdrawal ETB', 'Withdrawal Count', 'Largest Withdrawal ETB'], cur.fetchall()),
                  Spacer(1, 0.8 * cm)]

        # 3. Error & Incident
        s3_sql = "SELECT atm_id, branch, error_code, COUNT(*), TO_CHAR(MIN(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI'), TO_CHAR(MAX(recorded_at) AT TIME ZONE 'Africa/Addis_Ababa','YYYY-MM-DD HH24:MI') FROM atm_transactions WHERE status='ERROR' AND error_code IS NOT NULL AND recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch, error_code ORDER BY 4 DESC"
        cur.execute(s3_sql, [interval_str])
        res3 = cur.fetchall()
        if res3:
            story += [P('<font color="#0F2557" size="12"><b>3. Error & Incident Log</b></font>', spaceBefore=12, spaceAfter=8),
                      mktable(['ATM', 'Branch', 'Error Code', 'Occurrences', 'First Seen', 'Last Seen'], res3),
                      Spacer(1, 0.8 * cm)]

        # 4. Performance Metrics
        s4_sql = """
            WITH stats AS (
                SELECT atm_id, branch, COUNT(*) AS total,
                    ROUND(100.0*SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) AS rate,
                    ROUND(COUNT(*)/%s::numeric,1) AS daily
                FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s GROUP BY atm_id, branch
            ),
            peaks AS (
                SELECT DISTINCT ON (atm_id) atm_id, EXTRACT(HOUR FROM recorded_at) AS hr, COUNT(*) AS cnt
                FROM atm_transactions WHERE recorded_at >= NOW() - INTERVAL %s
                GROUP BY atm_id, EXTRACT(HOUR FROM recorded_at) ORDER BY atm_id, cnt DESC
            )
            SELECT s.atm_id, s.branch, s.total, s.rate, s.daily,
                CONCAT(p.hr::int,':00-',(p.hr::int+1),':00'), RANK() OVER (ORDER BY s.total DESC)
            FROM stats s JOIN peaks p ON s.atm_id=p.atm_id ORDER BY s.total DESC
        """
        cur.execute(s4_sql, (days, interval_str, interval_str))
        res4 = cur.fetchall()
        if res4:
            story += [PageBreak(),
                      P('<font color="#0F2557" size="12"><b>4. ATM Performance Metrics</b></font>', spaceBefore=12, spaceAfter=8),
                      mktable(['ATM', 'Branch', 'Total Transactions', 'Success Rate %', 'Avg Daily Txns', 'Peak Hour', 'Rank'], res4),
                      Spacer(1, 0.8 * cm)]

        story.append(P(
            f'<font size="8" color="#64748B">Dashen Bank ATM Monitoring System  |  Confidential Management Report  |  {datetime.now().strftime("%Y-%m-%d")}</font>',
            alignment=TA_CENTER
        ))

        cur.close(); conn.close()
        doc.build(story); buf.seek(0)
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                         download_name=f'Dashen_ATM_Complete_Report_{datetime.now().strftime("%Y%m%d")}.pdf')

