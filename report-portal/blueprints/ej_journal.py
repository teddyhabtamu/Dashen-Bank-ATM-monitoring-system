"""Fleet-scale view of the quarantined EJ staging tables.

The old page was a one-ATM validation cockpit: it showed the 12 newest files
and 50 newest transactions. That is useful while proving a parser and useless
at 1,300 ATMs, where the interesting rows are the exceptions.

This page is exception-first:
  * one row per active ATM, with the latest journal day and parse state
  * fleet totals and attention counts
  * drill-down per ATM for files, transactions and warnings

It remains read-only and admin-only. No KPI, report, cash forecast or anomaly
query reads staging data.
"""
import logging
from datetime import datetime, time, timedelta, timezone

from flask import (Blueprint, abort, make_response, render_template, request,
                   send_file, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.routing import BuildError
from db import get_db
from audit import log_action
from evidence import (VIEW_MAX_BYTES, VIEW_PAGE_SIZE, paginate_lines, raw_info,
                      raw_path, read_text_lines, sha256_file)
from blueprints.auth import login_required, role_required, ROLE_ADMIN

logger = logging.getLogger(__name__)

bp = Blueprint('ej_journal', __name__)
EAT = timezone(timedelta(hours=3))
PAGE_SIZE = 25
TXN_PAGE_SIZE = 50


def _now_eat():
    return datetime.now(EAT)


def _today_eat():
    return _now_eat().date()


def _days(raw, default=30):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 90))


def _page(raw):
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _bounds(days):
    """EAT calendar dates -> naive UTC timestamps for recorded_at."""
    today = _today_eat()
    since = today - timedelta(days=days - 1)
    until = today + timedelta(days=1)
    since_utc = datetime.combine(since, time.min).replace(tzinfo=EAT)
    until_utc = datetime.combine(until, time.min).replace(tzinfo=EAT)
    return since, today, (
        since_utc.astimezone(timezone.utc).replace(tzinfo=None),
        until_utc.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _eat(value, fmt='%Y-%m-%d %H:%M:%S'):
    if value is None:
        return '—'
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(EAT).strftime(fmt)
    return value.strftime(fmt)


def _warnings(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return ['%s=%s' % (key, val) for key, val in sorted(value.items())]
    return [str(value)]


def _status(row, today):
    if row['parse_errors']:
        return 'error'
    if row['journal_date'] is None:
        return 'no data'
    # NCR rotates around 17:30 and does not create today's file until the
    # first transaction. Yesterday's journal is still normal in the morning;
    # only two or more missed days is stale.
    if row['journal_date'] < today - timedelta(days=1):
        return 'stale'
    return 'ok'


def _filter_sql(vendor, q, alias):
    """Return (sql fragments, params) shared by the fleet queries."""
    clauses, params = [], []
    if vendor:
        clauses.append(f'UPPER({alias}.vendor) = %s')
        params.append(vendor)
    if q:
        pattern = f'%{q}%'
        clauses.append(f'({alias}.atm_id ILIKE %s OR '
                       f"COALESCE({alias}.branch, '') ILIKE %s)")
        params.extend([pattern, pattern])
    return clauses, params


def _fleet_health(cur, today, vendor, q):
    """Count the latest journal state for every active ATM.

    The fleet table only renders one page.  The health bar needs the whole
    filtered fleet, so this repeats the latest/aggregate CTE and counts the
    same status rule used by :func:`_status`.  It deliberately does not apply
    the selected period: an ATM whose last journal is older than the period is
    still stale, not "no data".
    """
    clauses, params = _filter_sql(vendor, q, 'a')
    where = ["LOWER(COALESCE(a.status, 'active')) = 'active'"]
    where.extend(clauses)
    stale_before = today - timedelta(days=1)
    cur.execute(f"""
        WITH latest AS (
            SELECT atm_id, MAX(journal_date) AS journal_date
              FROM ej_parse_runs
             WHERE atm_id IS NOT NULL
             GROUP BY atm_id
        ), agg AS (
            SELECT p.atm_id, p.journal_date,
                   COUNT(*) FILTER (WHERE p.status = 'error')::int
                       AS parse_errors
              FROM ej_parse_runs p
              JOIN latest l
                ON l.atm_id = p.atm_id
               AND l.journal_date = p.journal_date
             GROUP BY p.atm_id, p.journal_date
        )
        SELECT
          COUNT(*) FILTER (WHERE COALESCE(agg.parse_errors, 0) > 0)::int
              AS error,
          COUNT(*) FILTER (
              WHERE COALESCE(agg.parse_errors, 0) = 0
                AND agg.journal_date IS NULL)::int AS no_data,
          COUNT(*) FILTER (
              WHERE COALESCE(agg.parse_errors, 0) = 0
                AND agg.journal_date < %s)::int AS stale,
          COUNT(*) FILTER (
              WHERE COALESCE(agg.parse_errors, 0) = 0
                AND agg.journal_date >= %s)::int AS ok
          FROM atm_locations a
          LEFT JOIN agg ON agg.atm_id = a.atm_id
         WHERE {' AND '.join(where)}
    """, [stale_before, stale_before] + params)
    row = cur.fetchone()
    return {
        'ok': row[3] or 0,
        'stale': row[2] or 0,
        'error': row[0] or 0,
        'no_data': row[1] or 0,
        'total': sum((row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0)),
    }


def _fleet_summary(cur, since, since_utc, until_utc, vendor, q):
    """Cards and attention counts, all bounded by the selected period."""
    p_clauses = ["COALESCE(journal_date, parsed_at::date) >= %s"]
    p_params = [since]
    if vendor:
        p_clauses.append('UPPER(vendor) = %s')
        p_params.append(vendor)

    cur.execute(f"""
        SELECT count(*) AS files,
               count(*) FILTER (WHERE status = 'error') AS parse_errors,
               count(*) FILTER (WHERE atm_id IS NULL) AS unmapped_files,
               count(DISTINCT atm_id) AS reporting_atms
          FROM ej_parse_runs
         WHERE {' AND '.join(p_clauses)}
    """, p_params)
    row = cur.fetchone()
    summary = {
        'files': row[0] or 0,
        'parse_errors': row[1] or 0,
        'unmapped_files': row[2] or 0,
        'reporting_atms': row[3] or 0,
    }

    t_clauses = ['recorded_at >= %s', 'recorded_at < %s']
    t_params = [since_utc, until_utc]
    if vendor:
        t_clauses.append('UPPER(vendor) = %s')
        t_params.append(vendor)
    if q:
        pattern = f'%{q}%'
        t_clauses.append("(atm_id ILIKE %s OR COALESCE(branch, '') ILIKE %s)")
        t_params.extend([pattern, pattern])

    cur.execute(f"""
        SELECT count(*),
               count(*) FILTER (WHERE status = 'APPROVED'),
               COALESCE(sum(amount) FILTER (WHERE status = 'APPROVED'), 0),
               COALESCE(sum(dispensed_amount), 0),
               count(*) FILTER (
                   WHERE status = 'APPROVED'
                     AND COALESCE(dispensed_amount, 0) = 0)
          FROM ej_transactions
         WHERE {' AND '.join(t_clauses)}
    """, t_params)
    row = cur.fetchone()
    summary.update({
        'staged': row[0] or 0,
        'approved': row[1] or 0,
        'approved_amount': row[2] or 0,
        'dispensed': row[3] or 0,
        'no_cash_evidence': row[4] or 0,
    })

    a_clauses = ["LOWER(COALESCE(status, 'active')) = 'active'"]
    a_params = []
    if vendor:
        a_clauses.append('UPPER(vendor) = %s')
        a_params.append(vendor)
    if q:
        pattern = f'%{q}%'
        a_clauses.append("(atm_id ILIKE %s OR COALESCE(branch, '') ILIKE %s)")
        a_params.extend([pattern, pattern])
    cur.execute(f"""
        SELECT count(*) FROM atm_locations WHERE {' AND '.join(a_clauses)}
    """, a_params)
    summary['active_atms'] = cur.fetchone()[0] or 0

    u_clauses = ['e.recorded_at >= %s', 'e.recorded_at < %s',
                 'a.atm_id IS NULL']
    u_params = [since_utc, until_utc]
    if vendor:
        u_clauses.append('UPPER(e.vendor) = %s')
        u_params.append(vendor)
    if q:
        pattern = f'%{q}%'
        u_clauses.append("(e.atm_id ILIKE %s OR COALESCE(e.branch, '') ILIKE %s)")
        u_params.extend([pattern, pattern])
    cur.execute(f"""
        SELECT count(*)
          FROM ej_transactions e
          LEFT JOIN atm_locations a ON a.atm_id = e.atm_id
         WHERE {' AND '.join(u_clauses)}
    """, u_params)
    summary['unmapped_txns'] = cur.fetchone()[0] or 0

    return summary


def _attention_files(cur, since, vendor):
    clauses = ["COALESCE(journal_date, parsed_at::date) >= %s",
               "(status = 'error' OR atm_id IS NULL)"]
    params = [since]
    if vendor:
        clauses.append('UPPER(vendor) = %s')
        params.append(vendor)
    cur.execute(f"""
        SELECT file_path, hostname, journal_date, parsed_at, status, error
          FROM ej_parse_runs
         WHERE {' AND '.join(clauses)}
         ORDER BY parsed_at DESC
         LIMIT 10
    """, params)
    return [{
        'file_path': row[0],
        'hostname': row[1],
        'journal_date': row[2].isoformat() if row[2] else '—',
        'parsed_at': _eat(row[3]),
        'status': row[4],
        'error': row[5],
    } for row in cur.fetchall()]


@bp.route('/ej-journal')
@login_required
@role_required(ROLE_ADMIN)
def ej_journal():
    setup_error = None
    summary = {}
    rows = []
    attention_files = []
    days = _days(request.args.get('days'), 30)
    vendor = (request.args.get('vendor') or '').strip().upper()
    if vendor not in ('NCR', 'GRG'):
        vendor = ''
    q = (request.args.get('q') or '').strip()[:64]
    page = _page(request.args.get('page'))
    since, today, (since_utc, until_utc) = _bounds(days)
    total = 0
    total_pages = 1

    try:
        with get_db() as conn:
            cur = conn.cursor()
            summary = _fleet_summary(cur, since, since_utc, until_utc, vendor, q)
            summary['health'] = _fleet_health(cur, today, vendor, q)
            attention_files = _attention_files(cur, since, vendor)

            clauses, params = _filter_sql(vendor, q, 'a')
            where = ["LOWER(COALESCE(a.status, 'active')) = 'active'"]
            where.extend(clauses)
            base_params = [since] + params
            sql = f"""
                WITH latest AS (
                    SELECT atm_id, MAX(journal_date) AS journal_date
                      FROM ej_parse_runs
                     WHERE atm_id IS NOT NULL
                       AND COALESCE(journal_date, parsed_at::date) >= %s
                     GROUP BY atm_id
                ), agg AS (
                    SELECT p.atm_id, p.journal_date,
                           MAX(p.parsed_at) AS parsed_at,
                           SUM(p.sessions)::int AS sessions,
                           SUM(p.completed)::int AS completed,
                           SUM(p.declined)::int AS declined,
                           SUM(p.unknown)::int AS unknown,
                           SUM(COALESCE((p.warnings->>'no_receipt')::int, 0))::int
                               AS no_receipt,
                           COUNT(*) FILTER (WHERE p.status = 'error')::int
                               AS parse_errors
                      FROM ej_parse_runs p
                      JOIN latest l
                        ON l.atm_id = p.atm_id
                       AND l.journal_date = p.journal_date
                     GROUP BY p.atm_id, p.journal_date
                )
                SELECT a.atm_id, a.branch, a.vendor,
                       agg.journal_date, agg.parsed_at,
                       COALESCE(agg.sessions, 0) AS sessions,
                       COALESCE(agg.completed, 0) AS completed,
                       COALESCE(agg.declined, 0) AS declined,
                       COALESCE(agg.unknown, 0) AS unknown,
                       COALESCE(agg.no_receipt, 0) AS no_receipt,
                       COALESCE(agg.parse_errors, 0) AS parse_errors,
                       COUNT(*) OVER() AS total_rows
                  FROM atm_locations a
                  LEFT JOIN agg ON agg.atm_id = a.atm_id
                 WHERE {' AND '.join(where)}
                 ORDER BY (COALESCE(agg.parse_errors, 0) > 0) DESC,
                          agg.journal_date DESC NULLS LAST,
                          a.atm_id
                 LIMIT %s OFFSET %s
            """
            query_params = base_params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
            cur.execute(sql, query_params)
            raw_rows = cur.fetchall()

            if not raw_rows and page > 1:
                page = 1
                cur.execute(sql, base_params + [PAGE_SIZE, 0])
                raw_rows = cur.fetchall()

            rows = [{
                'atm_id': row[0],
                'branch': row[1],
                'vendor': row[2],
                'journal_date': row[3],
                'parsed_at': _eat(row[4]),
                'sessions': row[5],
                'completed': row[6],
                'declined': row[7],
                'unknown': row[8],
                'no_receipt': row[9],
                'parse_errors': row[10],
            } for row in raw_rows]
            for row in rows:
                row['status'] = _status(row, today)
            total = raw_rows[0][11] if raw_rows else 0
            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            cur.close()
    except Exception as exc:  # noqa: BLE001 - a missing migration must not 500
        logger.warning('EJ fleet page unavailable: %s', exc)
        setup_error = (
            'The EJ staging tables are not available or are missing the fleet '
            'metadata. Apply scripts/migrate_ej_shadow.sql, then re-run '
            'scripts/ingest_ej_shadow.py --commit.'
        )

    return render_template(
        'ej_journal.html',
        setup_error=setup_error,
        summary=summary,
        rows=rows,
        attention_files=attention_files,
        days=days,
        vendor=vendor,
        q=q,
        page=page,
        total=total,
        total_pages=total_pages,
        last_updated=_now_eat().strftime('%H:%M'),
    )


@bp.route('/ej-journal/<atm_id>')
@login_required
@role_required(ROLE_ADMIN)
def ej_journal_detail(atm_id):
    setup_error = None
    atm = None
    parse_runs = []
    transactions = []
    warnings = []
    summary = {}
    days = _days(request.args.get('days'), 30)
    page = _page(request.args.get('page'))
    since, today, (since_utc, until_utc) = _bounds(days)
    total = 0
    total_pages = 1

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT atm_id, branch, vendor, terminal_id, ej_hostname, status
                  FROM atm_locations
                 WHERE atm_id = %s
            """, (atm_id,))
            row = cur.fetchone()
            if not row:
                abort(404)
            atm = {
                'atm_id': row[0], 'branch': row[1], 'vendor': row[2],
                'terminal_id': row[3], 'ej_hostname': row[4], 'status': row[5],
            }

            cur.execute("""
                SELECT count(*),
                       count(*) FILTER (WHERE status = 'error'),
                       COALESCE(SUM(sessions), 0),
                       COALESCE(SUM(completed), 0),
                       COALESCE(SUM(declined), 0),
                       COALESCE(SUM(unknown), 0),
                       COALESCE(SUM(COALESCE((warnings->>'no_receipt')::int, 0)), 0)
                  FROM ej_parse_runs
                 WHERE atm_id = %s
                   AND COALESCE(journal_date, parsed_at::date) >= %s
            """, (atm_id, since))
            row = cur.fetchone()
            summary = {
                'files': row[0] or 0,
                'parse_errors': row[1] or 0,
                'sessions': row[2] or 0,
                'completed': row[3] or 0,
                'declined': row[4] or 0,
                'unknown': row[5] or 0,
                'no_receipt': row[6] or 0,
            }

            cur.execute("""
                SELECT count(*),
                       count(*) FILTER (WHERE status = 'APPROVED'),
                       COALESCE(sum(amount) FILTER (WHERE status = 'APPROVED'), 0),
                       COALESCE(sum(dispensed_amount), 0),
                       count(*) FILTER (
                           WHERE status = 'APPROVED'
                             AND COALESCE(dispensed_amount, 0) = 0)
                  FROM ej_transactions
                 WHERE atm_id = %s
                   AND recorded_at >= %s
                   AND recorded_at < %s
            """, (atm_id, since_utc, until_utc))
            row = cur.fetchone()
            summary.update({
                'staged': row[0] or 0,
                'approved': row[1] or 0,
                'approved_amount': row[2] or 0,
                'dispensed': row[3] or 0,
                'no_cash_evidence': row[4] or 0,
            })

            cur.execute("""
                SELECT file_path, hostname, journal_date, vendor, sessions,
                       completed, approved, declined, unknown, warnings,
                       parsed_at, status, error, id
                  FROM ej_parse_runs
                 WHERE atm_id = %s
                   AND COALESCE(journal_date, parsed_at::date) >= %s
                 ORDER BY journal_date DESC NULLS LAST, parsed_at DESC
                 LIMIT 200
            """, (atm_id, since))
            parse_runs = [{
                'file_path': r[0], 'hostname': r[1],
                'journal_date': r[2].isoformat() if r[2] else '—',
                'vendor': r[3], 'sessions': r[4], 'completed': r[5],
                'approved': r[6], 'declined': r[7], 'unknown': r[8],
                'warnings': _warnings(r[9]), 'parsed_at': _eat(r[10]),
                'status': r[11], 'error': r[12], 'id': r[13],
            } for r in cur.fetchall()]
            for run in parse_runs:
                run.update(raw_info(run['file_path']))

            cur.execute("""
                SELECT count(*)
                  FROM ej_transactions
                 WHERE atm_id = %s
                   AND recorded_at >= %s
                   AND recorded_at < %s
            """, (atm_id, since_utc, until_utc))
            total = cur.fetchone()[0] or 0
            total_pages = max(1, (total + TXN_PAGE_SIZE - 1) // TXN_PAGE_SIZE)
            if page > total_pages:
                page = total_pages

            cur.execute("""
                SELECT recorded_at, atm_id, terminal_id, txn_type, status,
                       amount, dispensed_amount, warnings, source_file,
                       parse_run_id, started_at, ended_at
                  FROM ej_transactions
                 WHERE atm_id = %s
                   AND recorded_at >= %s
                   AND recorded_at < %s
                 ORDER BY recorded_at DESC, id DESC
                 LIMIT %s OFFSET %s
            """, (atm_id, since_utc, until_utc, TXN_PAGE_SIZE,
                  (page - 1) * TXN_PAGE_SIZE))
            transactions = [{
                'recorded_at': _eat(r[0]), 'atm_id': r[1],
                'terminal_id': r[2], 'txn_type': r[3], 'status': r[4],
                'amount': r[5], 'dispensed_amount': r[6],
                'warnings': _warnings(r[7]), 'source_file': r[8],
                'parse_run_id': r[9],
                'started_at': _eat(r[10]) if r[10] else None,
                'ended_at': _eat(r[11]) if r[11] else None,
            } for r in cur.fetchall()]

            cur.execute("""
                SELECT warning, count(*)
                  FROM ej_transactions e
                  CROSS JOIN LATERAL
                       jsonb_array_elements_text(e.warnings) AS warning
                 WHERE e.atm_id = %s
                   AND e.recorded_at >= %s
                   AND e.recorded_at < %s
                 GROUP BY warning
                 ORDER BY count(*) DESC, warning
            """, (atm_id, since_utc, until_utc))
            warnings = [{'name': r[0], 'rows': r[1]} for r in cur.fetchall()]
            cur.close()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning('EJ detail page unavailable: %s', exc)
        setup_error = (
            'The EJ staging tables are not available or are missing the fleet '
            'metadata. Apply scripts/migrate_ej_shadow.sql, then re-run '
            'scripts/ingest_ej_shadow.py --commit.'
        )

    return render_template(
        'ej_journal_detail.html',
        setup_error=setup_error,
        atm=atm,
        summary=summary,
        parse_runs=parse_runs,
        transactions=transactions,
        warnings=warnings,
        days=days,
        page=page,
        total=total,
        total_pages=total_pages,
    )


@bp.route('/ej-journal/<atm_id>/raw/<int:run_id>')
@login_required
@role_required(ROLE_ADMIN)
def ej_raw_download(atm_id, run_id):
    """Download the byte-identical raw journal for one parse run.

    The database id is the only path input. The evidence helper rejects
    non-canonical names, symlinks and anything outside EVIDENCE_RAW_DIR.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT file_path
                  FROM ej_parse_runs
                 WHERE id = %s AND atm_id = %s
            """, (run_id, atm_id))
            row = cur.fetchone()
            cur.close()
    except Exception as exc:  # noqa: BLE001 - do not leak DB detail to the browser
        logger.warning('raw EJ lookup failed: %s', exc)
        abort(404)

    if not row:
        abort(404)
    path = raw_path(row[0])
    if not path:
        abort(404)

    try:
        size = path.stat().st_size
        digest = sha256_file(path)
    except OSError as exc:
        logger.warning('raw EJ read failed: %s', exc)
        abort(404)

    log_action(
        'EJ_RAW_DOWNLOAD',
        f'ATM {atm_id} file {path.name} bytes {size} sha256 {digest}',
    )
    response = send_file(
        path,
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=path.name,
        max_age=0,
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/ej-journal/<atm_id>/raw/<int:run_id>/view',
          methods=['GET', 'POST'])
@login_required
@role_required(ROLE_ADMIN)
def ej_raw_view(atm_id, run_id):
    """Read-only, paginated view of one archived raw journal.

    This route never reads from the ATM and never accepts a path. The only
    inputs are the database id and the ATM id; the evidence helper resolves the
    file inside the read-only archive.

    Search is POSTed and never placed in the URL: a raw-journal search can
    contain a PAN, and query strings are routinely written to proxy access
    logs. Pagination posts the same fields again.
    """
    if request.method == 'POST':
        q = '' if request.form.get('clear') else (request.form.get('q') or '')
        q = q.strip()[:200]
        page = _page(request.form.get('page'))
    else:
        q = ''
        page = _page(request.args.get('page'))

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT p.file_path, p.journal_date, p.parsed_at, p.status,
                       p.error, p.sessions, p.hostname, l.branch
                  FROM ej_parse_runs p
                  LEFT JOIN atm_locations l ON l.atm_id = p.atm_id
                 WHERE p.id = %s AND p.atm_id = %s
            """, (run_id, atm_id))
            row = cur.fetchone()
            cur.close()
    except Exception as exc:  # noqa: BLE001 - do not expose database detail
        logger.warning('raw EJ view lookup failed: %s', exc)
        abort(404)

    if not row:
        abort(404)
    file_path, journal_date, parsed_at, status, error, sessions, hostname, branch = row
    path = raw_path(file_path)
    if not path:
        abort(404)

    try:
        info = raw_info(file_path)
        text = read_text_lines(path)
    except OSError as exc:
        logger.warning('raw EJ view read failed: %s', exc)
        abort(404)

    view = {
        'lines': [],
        'total': 0,
        'page': 1,
        'pages': 1,
    }
    if not text['too_large'] and not text['binary']:
        view = paginate_lines(text['lines'], query=q, page=page,
                              per_page=VIEW_PAGE_SIZE)

    log_action(
        'EJ_RAW_VIEW',
        f'ATM {atm_id} file {path.name} page {view["page"]} '
        f'q_len {len(q)} total {view["total"]}',
    )
    try:
        images_url = url_for(
            'atm_images.atm_images', atm_id=atm_id,
            day=journal_date.isoformat() if journal_date else None,
        ) if journal_date else None
    except BuildError:
        # The raw viewer is also mounted in isolation by the route tests.
        images_url = None
    response = make_response(render_template(
        'ej_raw_view.html',
        images_url=images_url,
        atm_id=atm_id,
        branch=branch,
        run_id=run_id,
        file_label=path.name,
        file_size=info.get('raw_size_label', '—'),
        journal_date=journal_date.isoformat() if journal_date else '—',
        parsed_at=_eat(parsed_at),
        status=status,
        error=error,
        sessions=sessions,
        hostname=hostname,
        q=q,
        too_large=text['too_large'],
        binary=text['binary'],
        view=view,
        view_max_mb=VIEW_MAX_BYTES // (1024 * 1024),
    ))
    response.headers['Cache-Control'] = 'no-store'
    return response
