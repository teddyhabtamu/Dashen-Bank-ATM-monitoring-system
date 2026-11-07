"""Self-service report builder: page, preview API, exports and saved reports.

The heavy lifting lives in ``report_engine`` (catalog + SQL compiler) and
``report_store`` (persistence).  This module is the HTTP boundary: it turns a
JSON definition into a validated engine definition, enforces role rules and
writes an audit trail for previews and exports.
"""

from __future__ import annotations

import logging
import re

from flask import (Blueprint, jsonify, render_template, request, session)

from audit import log_action
from blueprints.auth import (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER,
                             login_required, role_required)
from db import get_db
import report_engine
import report_exports
import report_store

logger = logging.getLogger(__name__)

bp = Blueprint('report_builder', __name__)

_ALL_ROLES = (ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
_SCHEDULE_FORMATS = ('pdf', 'excel', 'csv')
_MAX_RECIPIENTS = 50


def _valid_email(value):
    """Match the admin scheduler's recipient rule (single @, dotted domain)."""
    value = (value or '').strip()
    if value.count('@') != 1 or any(ch.isspace() for ch in value):
        return False
    local, _, domain = value.partition('@')
    if not local or not domain or '.' not in domain:
        return False
    return all(label for label in domain.split('.'))


def _can_view_definition(definition, username, role):
    return (role == ROLE_ADMIN
            or definition.get('owner') == username
            or definition.get('visibility') == 'shared')


def _can_manage_definition(definition, username, role):
    """Only the owner (or an admin) may schedule deliveries.

    Being able to reopen a shared report is not permission to email its data
    to an arbitrary address.
    """
    return role == ROLE_ADMIN or definition.get('owner') == username


def _schedule_definition_id(params):
    match = re.search(r'definition_id\s*=\s*(\d+)', params or '')
    return int(match.group(1)) if match else None


def _definition_schedules(conn, definition_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, name, format, schedule, recipients, params, enabled, "
            "       last_run, last_status, last_error "
            "FROM scheduled_reports "
            "WHERE report_type = 'custom' AND params = %s "
            "ORDER BY id",
            (f'definition_id={definition_id}',))
        rows = cur.fetchall()
    finally:
        try:
            cur.close()
        except Exception:
            pass
    schedules = []
    for row in rows:
        schedules.append({
            'id': row[0],
            'name': row[1],
            'format': row[2],
            'schedule': row[3],
            'recipients': row[4],
            'enabled': bool(row[6]),
            'last_run': row[7].strftime('%Y-%m-%d %H:%M')
            if row[7] and hasattr(row[7], 'strftime') else (row[7] or None),
            'last_status': row[8],
            'last_error': row[9],
        })
    return schedules


def _delete_definition_schedules(conn, definition_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM scheduled_reports "
            "WHERE report_type = 'custom' AND params = %s",
            (f'definition_id={definition_id}',))
        conn.commit()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _schedule_counts(conn):
    """Map definition id -> number of schedules. Best effort: the badge is
    cosmetic and must never block the saved-report list."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT params FROM scheduled_reports "
                "WHERE report_type = 'custom'")
            rows = cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        counts = {}
        for (params,) in rows:
            definition_id = _schedule_definition_id(params)
            if definition_id:
                counts[definition_id] = counts.get(definition_id, 0) + 1
        return counts
    except Exception:
        return {}


@bp.route('/reports/builder')
@login_required
@role_required(*_ALL_ROLES)
def builder_page():
    default_start = (request.args.get('start') or '').strip()
    default_end = (request.args.get('end') or '').strip()
    # Only hand dates to the page when they are plausible.  The engine still
    # validates them when the definition is submitted; this avoids a broken
    # first preview on a hand-edited URL.
    import datetime as dt
    for value in (default_start, default_end):
        if value:
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                default_start = default_end = ''
                break
    return render_template(
        'report_builder.html',
        catalog=report_engine.catalog_payload(),
        default_start=default_start,
        default_end=default_end,
        default_definition_id=(request.args.get('definition_id') or '').strip(),
        can_share=session.get('role') == ROLE_ADMIN,
        username=session.get('username') or '',
    )


@bp.route('/api/reports/catalog')
@login_required
@role_required(*_ALL_ROLES)
def catalog():
    return jsonify(report_engine.catalog_payload())


@bp.route('/api/reports/recipients')
@login_required
@role_required(*_ALL_ROLES)
def recipients():
    """Directory autocomplete for schedule recipients.

    The admin schedule page has its own admin-only picker; the builder is used
    by viewers and operators too, so it needs the same suggestions without
    granting them the user-management page. Only username and email are
    returned, and free-text addresses remain allowed.
    """
    query = (request.args.get('q') or '').strip()
    if len(query) < 2:
        return jsonify([])
    try:
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT username, email FROM app_users "
                    "WHERE email IS NOT NULL AND email <> '' "
                    "AND (username ILIKE %s OR email ILIKE %s) "
                    "ORDER BY username LIMIT 10",
                    (f'%{query}%', f'%{query}%'))
                rows = cur.fetchall()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        return jsonify([{'username': row[0], 'email': row[1]}
                        for row in rows])
    except Exception:
        logger.exception('Could not load recipient suggestions')
        return jsonify([])


@bp.route('/api/reports/options')
@login_required
@role_required(*_ALL_ROLES)
def options():
    try:
        force = request.args.get('force') in ('1', 'true', 'yes')
        with get_db() as conn:
            payload = report_engine.filter_options(conn, force=force)
        return jsonify(payload)
    except Exception:
        logger.exception('Could not load report filter options')
        return jsonify({
            'error': 'Filter options could not be loaded. The builder can '
                     'still run with manually entered values.',
            'warnings': ['Filter options could not be loaded.'],
        }), 500


_DEFINITION_KEYS = ('dataset', 'group_by', 'measures', 'filters', 'sort',
                    'limit')


def _definition_payload(data):
    """Pull the definition out of a request that may also carry metadata."""
    if not isinstance(data, dict):
        return data
    if isinstance(data.get('definition'), dict):
        return data['definition']
    return {key: data[key] for key in _DEFINITION_KEYS if key in data}


def _preview_payload(data):
    return report_engine.validate_definition(
        _definition_payload(data),
        max_limit=report_engine.MAX_PREVIEW_LIMIT)


@bp.route('/api/reports/preview', methods=['POST'])
@login_required
@role_required(*_ALL_ROLES)
def preview():
    data = request.get_json(silent=True) or {}
    try:
        definition = _preview_payload(data)
        with get_db() as conn:
            # Preview is capped for the browser; the stored definition still
            # controls how many rows an export may contain.
            result = report_engine.run_report(
                conn, definition,
                limit=min(definition.limit,
                          report_engine.PREVIEW_ROW_LIMIT),
                timeout_ms=12000)
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except report_engine.ReportExecutionError as exc:
        logger.warning('Report preview failed: %s', exc)
        return jsonify({'error': str(exc)}), 422
    except Exception:
        logger.exception('Report preview failed')
        return jsonify({
            'error': 'The report could not be generated. Try a narrower '
                     'period or fewer filters.',
        }), 500

    log_action(
        'REPORT_PREVIEW',
        f"dataset={definition.dataset.id} hash={definition.definition_hash} "
        f"rows={result['row_count']} title={result['title']!r}")
    return jsonify(result)


@bp.route('/api/reports/export/<fmt>', methods=['POST'])
@login_required
@role_required(*_ALL_ROLES)
def export(fmt):
    if fmt not in ('excel', 'csv', 'pdf'):
        return jsonify({'error': 'Unsupported export format.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        definition = report_engine.validate_definition(
            _definition_payload(data),
            max_limit=report_engine.MAX_EXPORT_LIMIT)
        with get_db() as conn:
            result = report_engine.run_report(
                conn, definition, limit=definition.limit,
                timeout_ms=30000)
        response = report_exports.render(fmt, result)
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except report_engine.ReportExecutionError as exc:
        logger.warning('Report export failed: %s', exc)
        return jsonify({'error': str(exc)}), 422
    except Exception:
        logger.exception('Report export failed')
        return jsonify({
            'error': 'The report could not be generated. Try a narrower '
                     'period or fewer filters.',
        }), 500

    log_action(
        'REPORT_EXPORT',
        f"format={fmt} dataset={definition.dataset.id} "
        f"hash={definition.definition_hash} rows={result['row_count']} "
        f"title={result['title']!r}")
    return response


@bp.route('/report/custom/<int:definition_id>/<fmt>')
@login_required
@role_required(*_ALL_ROLES)
def custom_report_file(definition_id, fmt):
    """Generate a saved report for the scheduler.

    The scheduler authenticates with ``X-Internal-Key`` on ``/report/`` paths
    (see auth._is_internal), so this is the only file path it can reach.  A
    signed-in user can also download their own or a shared definition; a
    private definition owned by someone else is still rejected.
    """
    if fmt not in _SCHEDULE_FORMATS:
        return jsonify({'error': 'Unsupported export format.'}), 404
    internal = not session.get('logged_in')
    try:
        with get_db() as conn:
            saved = report_store.get_definition(conn, definition_id)
            if not saved:
                return jsonify({'error': 'Saved report not found.'}), 404
            if not internal and not _can_view_definition(
                    saved, session.get('username'), session.get('role')):
                return jsonify({'error': 'Access denied.'}), 403
            definition = report_engine.validate_definition(
                saved['definition'],
                max_limit=report_engine.MAX_EXPORT_LIMIT)
            result = report_engine.run_report(
                conn, definition, limit=definition.limit, timeout_ms=60000)
        response = report_exports.render(fmt, result)
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except report_engine.ReportExecutionError as exc:
        logger.warning('Scheduled report %s failed: %s', definition_id, exc)
        return jsonify({'error': str(exc)}), 422
    except Exception:
        logger.exception('Scheduled report %s could not be generated',
                         definition_id)
        return jsonify({
            'error': 'The report could not be generated. Narrow the saved '
                     'definition or check the reporting period.',
        }), 500

    log_action(
        'REPORT_SCHEDULE_EXPORT',
        f"format={fmt} id={definition_id} dataset={definition.dataset.id} "
        f"rows={result['row_count']}")
    return response


# ── Saved reports ───────────────────────────────────────────────────────────

@bp.route('/api/reports/definitions', methods=['GET'])
@login_required
@role_required(*_ALL_ROLES)
def list_saved_reports():
    try:
        with get_db() as conn:
            rows = report_store.list_definitions(
                conn, session.get('username') or '', session.get('role'))
            counts = _schedule_counts(conn)
        for row in rows:
            row['schedule_count'] = counts.get(row.get('id'), 0)
        return jsonify({'definitions': rows})
    except Exception:
        logger.exception('Could not list saved reports')
        return jsonify({'error': 'Saved reports could not be loaded.'}), 500


def _save_fields(data, *, role):
    name = str(data.get('name') or '').strip()
    if not name:
        raise report_engine.ReportDefinitionError('Give the report a name.')
    if len(name) > 120:
        raise report_engine.ReportDefinitionError(
            'Report name must be 120 characters or fewer.')
    description = str(data.get('description') or '').strip()
    if len(description) > 1000:
        raise report_engine.ReportDefinitionError(
            'Description must be 1000 characters or fewer.')
    visibility = str(data.get('visibility') or 'private').strip().lower()
    if visibility not in ('private', 'shared'):
        raise report_engine.ReportDefinitionError(
            'Visibility must be private or shared.')
    if visibility == 'shared' and role != ROLE_ADMIN:
        raise PermissionError('Only an administrator can share a report.')
    definition = report_engine.validate_definition(
        _definition_payload(data),
        max_limit=report_engine.MAX_EXPORT_LIMIT)
    return name, description, visibility, definition


@bp.route('/api/reports/definitions', methods=['POST'])
@login_required
@role_required(*_ALL_ROLES)
def create_saved_report():
    data = request.get_json(silent=True) or {}
    try:
        name, description, visibility, definition = _save_fields(
            data, role=session.get('role'))
        with get_db() as conn:
            row = report_store.create_definition(
                conn,
                name=name,
                description=description,
                dataset=definition.dataset.id,
                payload=definition.payload(),
                owner=session.get('username') or 'unknown',
                visibility=visibility,
            )
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except PermissionError as exc:
        return jsonify({'error': str(exc)}), 403
    except Exception:
        logger.exception('Could not save report')
        return jsonify({'error': 'The report could not be saved.'}), 500

    log_action('REPORT_SAVE', f"id={row['id']} name={name!r} "
                              f"visibility={visibility}")
    return jsonify(row), 201


@bp.route('/api/reports/definitions/<int:definition_id>', methods=['PUT'])
@login_required
@role_required(*_ALL_ROLES)
def update_saved_report(definition_id):
    data = request.get_json(silent=True) or {}
    try:
        name, description, visibility, definition = _save_fields(
            data, role=session.get('role'))
        with get_db() as conn:
            existing = report_store.get_definition(conn, definition_id)
            if not existing:
                return jsonify({'error': 'Saved report not found.'}), 404
            if existing.get('is_system'):
                return jsonify({'error': 'System reports cannot be edited.'}), 403
            if (existing.get('owner') != session.get('username')
                    and session.get('role') != ROLE_ADMIN):
                return jsonify({
                    'error': 'Only the owner or an administrator can edit '
                             'this report.'}), 403
            row = report_store.update_definition(
                conn, definition_id,
                name=name,
                description=description,
                dataset=definition.dataset.id,
                payload=definition.payload(),
                visibility=visibility,
            )
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except PermissionError as exc:
        return jsonify({'error': str(exc)}), 403
    except Exception:
        logger.exception('Could not update report %s', definition_id)
        return jsonify({'error': 'The report could not be updated.'}), 500

    log_action('REPORT_UPDATE', f"id={definition_id} name={name!r}")
    return jsonify(row)


@bp.route('/api/reports/definitions/<int:definition_id>', methods=['DELETE'])
@login_required
@role_required(*_ALL_ROLES)
def delete_saved_report(definition_id):
    try:
        with get_db() as conn:
            existing = report_store.get_definition(conn, definition_id)
            if not existing:
                return jsonify({'error': 'Saved report not found.'}), 404
            if existing.get('is_system'):
                return jsonify({'error': 'System reports cannot be deleted.'}), 403
            if (existing.get('owner') != session.get('username')
                    and session.get('role') != ROLE_ADMIN):
                return jsonify({
                    'error': 'Only the owner or an administrator can delete '
                             'this report.'}), 403
            deleted = report_store.delete_definition(conn, definition_id)
            if deleted:
                # A deleted definition must not leave a schedule that sends
                # a "saved report no longer exists" failure every morning.
                try:
                    _delete_definition_schedules(conn, definition_id)
                except Exception:
                    logger.warning('Could not remove schedules for deleted '
                                   'report %s', definition_id)
    except Exception:
        logger.exception('Could not delete report %s', definition_id)
        return jsonify({'error': 'The report could not be deleted.'}), 500

    if not deleted:
        return jsonify({'error': 'Saved report not found.'}), 404
    log_action('REPORT_DELETE', f"id={definition_id} name={existing['name']!r}")
    return jsonify({'success': True})


# ── Scheduled delivery of saved reports ─────────────────────────────────────

def _schedule_payload(data):
    """Validate the small schedule boundary shared by create and update."""
    from scheduler import validate_cron

    name = str(data.get('name') or '').strip()
    if not name:
        raise report_engine.ReportDefinitionError(
            'Give the schedule a name.')
    if len(name) > 100:
        raise report_engine.ReportDefinitionError(
            'Schedule name must be 100 characters or fewer.')

    schedule = str(data.get('schedule') or '').strip()
    cron_error = validate_cron(schedule)
    if cron_error:
        raise report_engine.ReportDefinitionError(
            f'Invalid schedule: {cron_error}')

    fmt = str(data.get('format') or 'pdf').strip().lower()
    if fmt not in _SCHEDULE_FORMATS:
        raise report_engine.ReportDefinitionError(
            'Choose PDF, Excel or CSV.')

    raw_recipients = data.get('recipients')
    if isinstance(raw_recipients, list):
        recipients = [str(item).strip() for item in raw_recipients]
    else:
        recipients = [item.strip()
                      for item in str(raw_recipients or '').split(',')]
    recipients = [item for item in recipients if item]
    if not recipients:
        raise report_engine.ReportDefinitionError(
            'Add at least one recipient email address.')
    if len(recipients) > _MAX_RECIPIENTS:
        raise report_engine.ReportDefinitionError(
            f'Use at most {_MAX_RECIPIENTS} recipients.')
    bad = [item for item in recipients if not _valid_email(item)]
    if bad:
        raise report_engine.ReportDefinitionError(
            'Invalid email address(es): ' + ', '.join(bad))
    enabled = data.get('enabled', True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return {
        'name': name,
        'schedule': schedule,
        'format': fmt,
        'recipients': ', '.join(recipients),
        'enabled': bool(enabled),
    }


def _schedule_target(conn, definition_id):
    saved = report_store.get_definition(conn, definition_id)
    if not saved:
        return None, (jsonify({'error': 'Saved report not found.'}), 404)
    if not _can_manage_definition(
            saved, session.get('username'), session.get('role')):
        return None, (jsonify({
            'error': 'Only the report owner or an administrator can '
                     'schedule it.'}), 403)
    return saved, None


def _schedule_row(conn, schedule_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, name, report_type, format, schedule, recipients, "
            "       params, enabled, last_run, last_status, last_error "
            "FROM scheduled_reports WHERE id = %s",
            (schedule_id,))
        return cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:
            pass


@bp.route('/api/reports/definitions/<int:definition_id>/schedules',
          methods=['GET'])
@login_required
@role_required(*_ALL_ROLES)
def list_definition_schedules(definition_id):
    try:
        with get_db() as conn:
            saved, error = _schedule_target(conn, definition_id)
            if error:
                return error
            schedules = _definition_schedules(conn, definition_id)
        return jsonify({'schedules': schedules,
                        'definition': {'id': saved['id'],
                                       'name': saved['name']}})
    except Exception:
        logger.exception('Could not list schedules for report %s',
                         definition_id)
        return jsonify({'error': 'Schedules could not be loaded.'}), 500


@bp.route('/api/reports/definitions/<int:definition_id>/schedules',
          methods=['POST'])
@login_required
@role_required(*_ALL_ROLES)
def create_definition_schedule(definition_id):
    data = request.get_json(silent=True) or {}
    try:
        fields = _schedule_payload(data)
        with get_db() as conn:
            saved, error = _schedule_target(conn, definition_id)
            if error:
                return error
            # A schedule that points at a definition the engine can no longer
            # validate would fail silently every morning.  Refuse it now.
            report_engine.validate_definition(
                saved['definition'],
                max_limit=report_engine.MAX_EXPORT_LIMIT)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO scheduled_reports "
                    "(name, report_type, format, schedule, recipients, "
                    " params, enabled) "
                    "VALUES (%s, 'custom', %s, %s, %s, %s, %s) "
                    "RETURNING id, name, format, schedule, recipients, "
                    "          enabled, last_run, last_status, last_error",
                    (fields['name'], fields['format'], fields['schedule'],
                     fields['recipients'], f'definition_id={definition_id}',
                     fields['enabled']))
                row = cur.fetchone()
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        log_action(
            'REPORT_SCHEDULE_CREATE',
            f"definition={definition_id} name={fields['name']!r} "
            f"cron={fields['schedule']!r} format={fields['format']}")
        return jsonify({
            'id': row[0], 'name': row[1], 'format': row[2],
            'schedule': row[3], 'recipients': row[4],
            'enabled': bool(row[5]),
            'last_run': row[6].strftime('%Y-%m-%d %H:%M')
            if row[6] and hasattr(row[6], 'strftime') else row[6],
            'last_status': row[7], 'last_error': row[8],
        }), 201
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.exception('Could not create a schedule for report %s',
                         definition_id)
        return jsonify({'error': 'The schedule could not be saved.'}), 500


def _schedule_and_owner(conn, schedule_id):
    row = _schedule_row(conn, schedule_id)
    if not row:
        return None, None, (jsonify({'error': 'Schedule not found.'}), 404)
    if row[2] != 'custom':
        return None, None, (jsonify({
            'error': 'Only custom-report schedules can be managed here.'}), 400)
    definition_id = _schedule_definition_id(row[6])
    if not definition_id:
        return None, None, (jsonify({
            'error': 'This schedule is not linked to a saved report.'}), 400)
    saved, error = _schedule_target(conn, definition_id)
    if error:
        return None, None, error
    return row, saved, None


@bp.route('/api/reports/schedules/<int:schedule_id>', methods=['PUT'])
@login_required
@role_required(*_ALL_ROLES)
def update_definition_schedule(schedule_id):
    data = request.get_json(silent=True) or {}
    try:
        fields = _schedule_payload(data)
        with get_db() as conn:
            row, saved, error = _schedule_and_owner(conn, schedule_id)
            if error:
                return error
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE scheduled_reports SET name = %s, format = %s, "
                    "schedule = %s, recipients = %s, enabled = %s "
                    "WHERE id = %s "
                    "RETURNING id, name, format, schedule, recipients, "
                    "          enabled, last_run, last_status, last_error",
                    (fields['name'], fields['format'], fields['schedule'],
                     fields['recipients'], fields['enabled'], schedule_id))
                updated = cur.fetchone()
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        log_action(
            'REPORT_SCHEDULE_UPDATE',
            f"id={schedule_id} definition={saved['id']} "
            f"name={fields['name']!r} cron={fields['schedule']!r}")
        return jsonify({
            'id': updated[0], 'name': updated[1], 'format': updated[2],
            'schedule': updated[3], 'recipients': updated[4],
            'enabled': bool(updated[5]),
            'last_run': updated[6].strftime('%Y-%m-%d %H:%M')
            if updated[6] and hasattr(updated[6], 'strftime') else updated[6],
            'last_status': updated[7], 'last_error': updated[8],
        })
    except report_engine.ReportDefinitionError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.exception('Could not update schedule %s', schedule_id)
        return jsonify({'error': 'The schedule could not be updated.'}), 500


@bp.route('/api/reports/schedules/<int:schedule_id>/toggle',
          methods=['POST'])
@login_required
@role_required(*_ALL_ROLES)
def toggle_definition_schedule(schedule_id):
    data = request.get_json(silent=True) or {}
    try:
        with get_db() as conn:
            row, saved, error = _schedule_and_owner(conn, schedule_id)
            if error:
                return error
            if 'enabled' in data:
                enabled = bool(data.get('enabled'))
            else:
                enabled = not bool(row[7])
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE scheduled_reports SET enabled = %s "
                    "WHERE id = %s RETURNING enabled, name",
                    (enabled, schedule_id))
                updated = cur.fetchone()
                conn.commit()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        log_action(
            'REPORT_SCHEDULE_TOGGLE',
            f"id={schedule_id} definition={saved['id']} "
            f"{'enabled' if updated[0] else 'disabled'}")
        return jsonify({'success': True, 'enabled': bool(updated[0]),
                        'name': updated[1]})
    except Exception:
        logger.exception('Could not toggle schedule %s', schedule_id)
        return jsonify({'error': 'The schedule could not be updated.'}), 500


@bp.route('/api/reports/schedules/<int:schedule_id>', methods=['DELETE'])
@login_required
@role_required(*_ALL_ROLES)
def delete_definition_schedule(schedule_id):
    try:
        with get_db() as conn:
            row, saved, error = _schedule_and_owner(conn, schedule_id)
            if error:
                return error
            cur = conn.cursor()
            try:
                cur.execute(
                    "DELETE FROM scheduled_reports WHERE id = %s "
                    "RETURNING id, name", (schedule_id,))
                deleted = cur.fetchone()
                conn.commit()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        log_action(
            'REPORT_SCHEDULE_DELETE',
            f"id={schedule_id} definition={saved['id']} "
            f"name={(deleted[1] if deleted else row[1])!r}")
        return jsonify({'success': True})
    except Exception:
        logger.exception('Could not delete schedule %s', schedule_id)
        return jsonify({'error': 'The schedule could not be deleted.'}), 500
