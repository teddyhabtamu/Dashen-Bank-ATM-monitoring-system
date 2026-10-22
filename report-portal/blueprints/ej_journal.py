"""Read-only view of the quarantined EJ staging tables.

This page is the visible half of the EJ integration. It proves what the
parser produced and why a row is or is not ready for reconciliation, without
letting unvalidated journal data into the KPIs or the official transaction
table.

Admin-only for now: staging data is diagnostic, not an operational ledger.
"""
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template

from blueprints.auth import login_required, role_required, ROLE_ADMIN
from db import get_db

logger = logging.getLogger(__name__)

bp = Blueprint('ej_journal', __name__)
EAT = timezone(timedelta(hours=3))


def _eat(value):
    if value is None:
        return '—'
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EAT).strftime('%Y-%m-%d %H:%M:%S')


def _warnings(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return ['%s=%s' % (k, v) for k, v in sorted(value.items())]
    return [str(value)]


@bp.route('/ej-journal')
@login_required
@role_required(ROLE_ADMIN)
def ej_journal():
    setup_error = None
    parse_runs = []
    transactions = []
    counts = []
    warnings = []
    total = {'rows': 0, 'approved': 0, 'dispensed': 0, 'unmapped': 0}

    try:
        with get_db() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT file_path, vendor, sessions, completed, approved,
                       declined, unknown, warnings, parsed_at, status, error
                  FROM ej_parse_runs
                 ORDER BY parsed_at DESC, id DESC
                 LIMIT 12
            """)
            parse_runs = [
                {
                    'file_path': row[0],
                    'vendor': row[1],
                    'sessions': row[2],
                    'completed': row[3],
                    'approved': row[4],
                    'declined': row[5],
                    'unknown': row[6],
                    'warnings': _warnings(row[7]),
                    'parsed_at': _eat(row[8]),
                    'status': row[9],
                    'error': row[10],
                }
                for row in cur.fetchall()
            ]

            cur.execute("""
                SELECT COALESCE(status, 'UNKNOWN'), count(*),
                       COALESCE(sum(amount) FILTER (WHERE status = 'APPROVED'), 0),
                       COALESCE(sum(dispensed_amount), 0)
                  FROM ej_transactions
                 GROUP BY 1
                 ORDER BY 1
            """)
            counts = [
                {'status': row[0], 'rows': row[1], 'amount': row[2],
                 'dispensed': row[3]}
                for row in cur.fetchall()
            ]

            cur.execute("""
                SELECT count(*),
                       COALESCE(sum(amount) FILTER (WHERE status = 'APPROVED'), 0),
                       COALESCE(sum(dispensed_amount), 0)
                  FROM ej_transactions
            """)
            row = cur.fetchone()
            total['rows'] = row[0]
            total['approved'] = row[1]
            total['dispensed'] = row[2]

            cur.execute("""
                SELECT count(*)
                  FROM ej_transactions e
                  LEFT JOIN atm_locations a ON a.atm_id = e.atm_id
                 WHERE a.atm_id IS NULL
            """)
            total['unmapped'] = cur.fetchone()[0]

            cur.execute("""
                SELECT warning, count(*)
                  FROM ej_transactions e
                  CROSS JOIN LATERAL
                       jsonb_array_elements_text(e.warnings) AS warning
                 GROUP BY warning
                 ORDER BY count(*) DESC, warning
            """)
            warnings = [{'name': row[0], 'rows': row[1]}
                        for row in cur.fetchall()]

            cur.execute("""
                SELECT recorded_at, atm_id, terminal_id, txn_type, status,
                       amount, dispensed_amount, warnings, source_file
                  FROM ej_transactions
                 ORDER BY recorded_at DESC, id DESC
                 LIMIT 50
            """)
            transactions = [
                {
                    'recorded_at': _eat(row[0]),
                    'atm_id': row[1],
                    'terminal_id': row[2],
                    'txn_type': row[3],
                    'status': row[4],
                    'amount': row[5],
                    'dispensed_amount': row[6],
                    'warnings': _warnings(row[7]),
                    'source_file': row[8],
                }
                for row in cur.fetchall()
            ]
            cur.close()
    except Exception as exc:  # noqa: BLE001 - a missing migration must not 500
        logger.warning('EJ staging page unavailable: %s', exc)
        setup_error = (
            'The EJ staging tables are not available yet. Apply '
            'scripts/migrate_ej_shadow.sql to the portal database, then run '
            'scripts/ingest_ej_shadow.py --commit.'

        )

    return render_template(
        'ej_journal.html',
        setup_error=setup_error,
        parse_runs=parse_runs,
        transactions=transactions,
        counts=counts,
        warnings=warnings,
        total=total,
    )
