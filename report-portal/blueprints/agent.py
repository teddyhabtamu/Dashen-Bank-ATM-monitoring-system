"""Admin surface for agent profiles and observed identity.

This is the bridge between the registry and the box: it renders the profile a
technician will apply, and it accepts the identity the box reports back. It is
admin-only because the profile lists every deployable and the identity can
contain serial numbers.

Nothing here writes identity into `atm_locations`. Conflicts are shown; the
registry only changes through the registration form, deliberately.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from flask import (Blueprint, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)

from agent_profile import (ProfileError, build_profile, load_manifest, verify)
from agent_registry import (load_identity, save_identity, set_image_roots)
from atm_identity import (IdentityError, clean_paths, conflicts, parse,
                          probe_drift, readiness)
from audit import log_action
from blueprints.auth import ROLE_ADMIN, login_required, role_required
from db import get_db
from log_safety import safe_log

logger = logging.getLogger(__name__)

bp = Blueprint('agent', __name__)

_ATM_SQL = """
SELECT atm_id, atm_name, vendor, model, district, city, ip_address,
       terminal_id, ej_hostname, ej_log_format, image_roots
FROM atm_locations WHERE atm_id = %s
"""


def _atm(atm_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_ATM_SQL, (atm_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        cur.close()
    if not row:
        abort(404)
    return dict(zip(cols, row))


def _secret():
    return (os.environ.get('ATM_PROFILE_SECRET') or '').strip()


def _profile_for(atm_id):
    atm = _atm(atm_id)
    try:
        manifest = load_manifest()
        profile = build_profile(atm, manifest, secret=_secret())
    except ProfileError as exc:
        return atm, None, str(exc)
    return atm, profile, ''


def _expected_probes(profile):
    return {p['name']: {'sha256': p.get('sha256'), 'version': p.get('version')}
            for p in (profile or {}).get('probes') or []}


def _download_payload(profile):
    """The exact bytes the download route serves, and their SHA-256.

    The canonical `profile_hash` is computed over sorted compact JSON, which
    the installer cannot reproduce in Windows PowerShell. `/EXPECTEDHASH=`
    therefore pins this raw hash instead; both are shown on the page so the
    two values are never confused.
    """
    payload = json.dumps(profile, indent=2) + '\n'
    return payload, hashlib.sha256(payload.encode('utf-8')).hexdigest()


@bp.route('/atms/<atm_id>/agent')
@login_required
@role_required(ROLE_ADMIN)
def agent_page(atm_id):
    atm, profile, error = _profile_for(atm_id)
    stored = load_identity(atm_id)
    identity = None
    drift = None
    ready = None
    conflict_rows = []
    if stored:
        identity = stored.get('payload') or {}
        # Recompute against the current row: the registry may have changed
        # after the identity arrived, and a stale conflict list is worse than
        # none.
        conflict_rows = conflicts(identity, atm)
        drift = probe_drift(identity, _expected_probes(profile))
        ready = readiness(identity, atm.get('image_roots') or [])

    signature_valid = None
    download_sha256 = None
    if profile:
        _payload, download_sha256 = _download_payload(profile)
    if profile and _secret():
        signature_valid = verify(profile, _secret())
    return render_template(
        'agent.html',
        atm=atm,
        profile=profile,
        profile_error=error,
        stored=stored,
        identity=identity,
        conflicts=conflict_rows,
        drift=drift,
        readiness=ready,
        signed=bool((profile or {}).get('signature')),
        signature_valid=signature_valid,
        download_sha256=download_sha256,
    )


@bp.route('/atms/<atm_id>/agent/profile.json')
@login_required
@role_required(ROLE_ADMIN)
def agent_profile_download(atm_id):
    _atm_row, profile, error = _profile_for(atm_id)
    if error:
        return jsonify({'error': error, 'atm_id': atm_id}), 409
    payload, download_sha256 = _download_payload(profile)
    log_action('AGENT_PROFILE_DOWNLOAD',
               f'{atm_id} hash={profile["profile_hash"][:16]} '
               f'sha256={download_sha256[:16]} ready={profile["ready"]}')
    resp = Response(payload, mimetype='application/json')
    resp.headers['Content-Disposition'] = f'attachment; filename="{atm_id}.profile.json"'
    resp.headers['X-Profile-Sha256'] = download_sha256
    resp.headers['X-Profile-Canonical-Sha256'] = profile['profile_hash']
    return resp


@bp.route('/atms/<atm_id>/agent/identity', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def agent_identity_ingest(atm_id):
    """Accept the JSON the identity probe prints; manual paste is the fallback.

    Rejecting is the default: an unparseable or out-of-range document changes
    nothing and returns a message the technician can act on.
    """
    atm = _atm(atm_id)
    wants_json = request.is_json or request.args.get('format') == 'json'
    if request.is_json:
        raw = request.get_json(silent=True)
    else:
        raw = (request.form.get('payload') or '').strip()

    try:
        identity = parse(raw)
    except IdentityError as exc:
        message = f'Identity rejected: {exc}'
        logger.info('identity rejected for %s: %s', safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF
        if wants_json:
            return jsonify({'ok': False, 'error': message}), 400
        flash(message, 'error')
        return redirect(url_for('agent.agent_page', atm_id=atm_id))

    conflict_rows = conflicts(identity, atm)
    try:
        save_identity(atm_id, identity, conflict_rows, session.get('username', ''))
    except Exception as exc:  # noqa: BLE001 - report, do not 500 the page
        logger.warning('identity save failed for %s: %s', safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF
        message = 'Identity was valid but could not be stored; the database is unavailable.'
        if wants_json:
            return jsonify({'ok': False, 'error': message}), 503
        flash(message, 'error')
        return redirect(url_for('agent.agent_page', atm_id=atm_id))

    log_action('AGENT_IDENTITY_INGEST',
               f'{atm_id} terminal={identity.get("terminal_id") or "-"} '
               f'ej={((identity.get("ej") or {}).get("hostname")) or "-"} '
               f'conflicts={len(conflict_rows)}')
    if wants_json:
        return jsonify({'ok': True, 'atm_id': atm_id, 'conflicts': conflict_rows})
    if conflict_rows:
        flash(f'Identity stored — {len(conflict_rows)} conflict(s) need review.', 'error')
    else:
        flash('Identity stored; the box agrees with the registry.', 'success')
    return redirect(url_for('agent.agent_page', atm_id=atm_id))


def _roots_from_text(text):
    """One path per line; blank lines and # comments are ignored."""
    values = []
    for line in (text or '').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            values.append(line)
    return clean_paths(values, 'image_roots')


@bp.route('/atms/<atm_id>/agent/roots', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def agent_roots_save(atm_id):
    _atm(atm_id)
    try:
        roots = _roots_from_text(request.form.get('roots'))
    except IdentityError as exc:
        flash(f'Image roots not saved: {exc}', 'error')
        return redirect(url_for('agent.agent_page', atm_id=atm_id))
    try:
        set_image_roots(atm_id, roots)
    except Exception as exc:  # noqa: BLE001
        logger.warning('image roots save failed for %s: %s', safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF
        flash('Image roots were valid but could not be stored.', 'error')
        return redirect(url_for('agent.agent_page', atm_id=atm_id))
    log_action('AGENT_ROOTS_SET', f'{atm_id} roots={len(roots)}')
    flash(f'{len(roots)} approved image root(s) saved.', 'success')
    return redirect(url_for('agent.agent_page', atm_id=atm_id))
