"""
blueprints/onboarding.py — guided ATM onboarding wizard.

Three steps, ADMIN role only:
  1. Facts      — closed vocabularies, inline validation, zero writes.
  2. Pre-flight — LIVE probes (duplicate check, TCP reachability) and STATIC
                  statements, never implying more certainty than we have.
  3. Provision  — saves the record through the same writer Registration uses
                  (blueprints.admin.save_atm_record), then previews the Zabbix
                  changes with provision.provision_atms(dry_run=True) and only
                  writes when an admin confirms.
  4. Verify     — verify.verify_atm decides whether the ATM actually reports,
                  so "live" means measured, not assumed.

Writes are deliberate, admin-only and audited. Credential escrow and new RBAC
roles are still NOT here (docs/onboarding-wizard-spec.md).
"""
import logging
import socket

from flask import Blueprint, render_template, request
from blueprints.auth import login_required, role_required, ROLE_ADMIN
from audit import log_action
from db import get_db
from onboard_rules import (VENDORS, DISTRICTS, PROBE_PORT, validate_facts,
                            to_atm_data)
from provision import provision_atms
from verify import verify_atm

logger = logging.getLogger(__name__)

bp = Blueprint('onboarding', __name__, url_prefix='/admin')


def _tcp_probe(ip, port, timeout=3):
    """True if TCP connects, False otherwise. Read-only network probe."""
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _preflight(cleaned):
    """Run ordered checks. Each result: (label, ok: True/False/None,
    detail, fixer). ok=None means STATIC (verified later, stated)."""
    out = []
    ip = cleaned['ip_address']
    vendor = cleaned['vendor']
    # 1. DB uniqueness (LIVE).
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id FROM atm_locations WHERE atm_id = %s OR ip_address = %s",
                        (cleaned['atm_id'], ip))
            clash = cur.fetchone()
            cur.close()
        if clash:
            out.append(('Not already registered', False,
                        f'Conflicts with existing row {clash[0]}.',
                        'Your fix: pick an unused ATM ID / IP.'))
        else:
            out.append(('Not already registered', True,
                        'ATM ID and IP are unused.', '—'))
    except Exception as e:
        logger.warning('onboard uniqueness check failed: %s', e)
        out.append(('Not already registered', None,
                    'Database check unavailable right now.',
                    'Engineer: verify DB reachability.'))
    # 2. Reachability (LIVE where possible).
    if vendor in PROBE_PORT:
        port = PROBE_PORT[vendor]
        if _tcp_probe(ip, port):
            out.append((f'Reachable on TCP/{port}', True,
                        f'{ip}:{port} answered.', '—'))
        else:
            out.append((f'Reachable on TCP/{port}', False,
                        f'{ip}:{port} silent.',
                        'Network team: forward the vendor port (see onboarding doc Step 0).'))
    else:
        out.append(('Reachability', None,
                    'GRG speaks UDP/SNMP — no TCP equivalent; verified live at provision.',
                    'Not a failure. Continue.'))
    # 3. Model known (STATIC by construction — closed dropdown).
    out.append(('Model known', True,
                f"{cleaned['vendor']} {cleaned['model']} has templates.",
                '—'))
    # 4. Credential reference (STATIC in preview — escrow lands with RBAC).
    out.append(('Credential reference', None,
                'Preview build: credentials resolve at provision time.',
                'Admin: pick the sealed escrow entry when provisioning.'))
    # 5. Journal preview (STATIC — pattern stated).
    out.append(('Journal path preview', None,
                'Journals pull hourly per ATM after onboarding.',
                'Nothing to do now.'))
    return out


def _page(values, errors, checks, **extra):
    """Every render passes the same context, so a wizard step can never forget
    a value and silently blank the form."""
    ctx = {'vendors': VENDORS, 'districts': DISTRICTS, 'values': values,
           'errors': errors, 'checks': checks,
           'saved': False, 'plan': None, 'provision': None, 'verification': None,
           'save_errors': {}}
    ctx.update(extra)
    return render_template('onboarding.html', **ctx)


def _form_values():
    return {k: (request.form.get(k, '') or '') for k in
            ('atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
             'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
             'ip_address')}


@bp.route('/onboard', methods=['GET', 'POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard():
    """Step 1 (facts) + Step 2 (pre-flight). Still zero writes."""
    if request.method == 'GET':
        return _page({}, {}, None)
    values = _form_values()
    cleaned, errors = validate_facts(values)
    if errors:
        return _page(values, errors, None)
    return _page(cleaned, {}, _preflight(cleaned))


@bp.route('/onboard/save', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_save():
    """Step 3a — write the ATM record (same writer as Registration), then show
    what provisioning WOULD do. Nothing is sent to Zabbix yet."""
    from blueprints.admin import normalise_atm_form, save_atm_record
    values = _form_values()
    cleaned, errors = validate_facts(values)
    if errors:
        return _page(values, errors, None)
    # to_atm_data fills the columns the writer requires but this page never
    # collects (status), so a save cannot fail on an invisible field.
    data = normalise_atm_form(to_atm_data(values))
    # ej_hostname is optional in the wizard; keep whatever is already stored.
    data['ej_hostname'] = (request.form.get('ej_hostname') or '').strip() or None
    res = save_atm_record(data, source='onboarding-wizard')
    if not res['ok']:
        return _page(cleaned, res['errors'], _preflight(cleaned),
                     saved=False, save_errors=res['errors'])
    log_action('ATM_SAVE', res['audit_detail'])
    plan = provision_atms([data['atm_id']], dry_run=True, actor='onboarding-wizard')
    return _page(cleaned, {}, _preflight(cleaned), saved=True, plan=plan)


@bp.route('/onboard/provision', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_provision():
    """Step 3b — actually create/update the Zabbix host, through provision.py
    (the single provisioning writer Registration also uses)."""
    atm_id = (request.form.get('atm_id') or '').strip()
    if not atm_id:
        return _page(_form_values(), {'atm_id': 'ATM ID is required.'}, None)
    plan = provision_atms([atm_id], dry_run=True, actor='onboarding-wizard')
    out = provision_atms([atm_id], dry_run=False, actor='onboarding-wizard')
    summary = out.get('summary') or {}
    log_action('ZABBIX_SYNC', f'wizard: {atm_id} created={summary.get("created", 0)} '
                              f'updated={summary.get("updated", 0)} '
                              f'skipped={summary.get("skipped", 0)} '
                              f'failed={summary.get("failed", 0)}')
    saved = {'atm_id': atm_id}
    return _page(saved, {}, None, saved=True, plan=plan, provision=out)


@bp.route('/onboard/verify', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_verify():
    """Step 4 — did it actually report? Read-only, and the only thing allowed
    to say an ATM is live."""
    atm_id = (request.form.get('atm_id') or '').strip()
    if not atm_id:
        return _page({}, {'atm_id': 'Save the ATM first.'}, None)
    # values.atm_id is the only field this step needs.
    out = verify_atm(atm_id)
    return _page({'atm_id': atm_id}, {}, None, saved=True,
                 verification=out)
