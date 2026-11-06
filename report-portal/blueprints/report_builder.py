"""Self-service report builder: page, preview API, exports and saved reports.

The heavy lifting lives in ``report_engine`` (catalog + SQL compiler) and
``report_store`` (persistence).  This module is the HTTP boundary: it turns a
JSON definition into a validated engine definition, enforces role rules and
writes an audit trail for previews and exports.
"""

from __future__ import annotations

import logging

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


# ── Saved reports ───────────────────────────────────────────────────────────

@bp.route('/api/reports/definitions', methods=['GET'])
@login_required
@role_required(*_ALL_ROLES)
def list_saved_reports():
    try:
        with get_db() as conn:
            rows = report_store.list_definitions(
                conn, session.get('username') or '', session.get('role'))
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
    except Exception:
        logger.exception('Could not delete report %s', definition_id)
        return jsonify({'error': 'The report could not be deleted.'}), 500

    if not deleted:
        return jsonify({'error': 'Saved report not found.'}), 404
    log_action('REPORT_DELETE', f"id={definition_id} name={existing['name']!r}")
    return jsonify({'success': True})
