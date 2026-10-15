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
from onboard_rules import (VENDORS, DISTRICTS, PROBE_PORT, BLOCKING,
                            classify_existing, to_atm_data, validate_facts)
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
    """Run ordered checks. Each result:
    (label, ok: True/False/None, detail, fixer, blocking: bool).
    ok=None means STATIC (verified later, stated). Only checks flagged BLOCKING
    stop the wizard: the next step is not offered while one of them fails."""
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
        kind, message = classify_existing(clash[0] if clash else None,
                                          cleaned['atm_id'])
        if kind == 'edit':
            out.append(('ATM already registered', None, message,
                        'This run will update the record, not create a second one.', False))
        elif kind == 'clash':
            out.append(('Not already registered', False, message,
                        'Your fix: pick an unused ATM ID / IP.', True))
        else:
            out.append(('Not already registered', True, message, '—', False))
    except Exception as e:
        logger.warning('onboard uniqueness check failed: %s', e)
        out.append(('Not already registered', None,
                    'Database check unavailable right now.',
                    'Engineer: verify DB reachability.', False))
    # 2. Reachability (LIVE where possible).
    if vendor in PROBE_PORT:
        port = PROBE_PORT[vendor]
        if _tcp_probe(ip, port):
            out.append((f'Reachable on TCP/{port}', True,
                        f'{ip}:{port} answered.', '—', False))
        else:
            # Advisory, not blocking: the host can be created before the box
            # answers, and verify.py is the gate that decides "reporting".
            out.append((f'Reachable on TCP/{port}', False,
                        f'{ip}:{port} silent.',
                        'Network team: forward the vendor port (see onboarding doc Step 0). '
                        'You can still provision — Step 4 will keep saying it is not reporting.', False))
    else:
        out.append(('Reachability', None,
                    'GRG speaks UDP/SNMP — no TCP equivalent; verified live at provision.',
                    'Not a failure. Continue.', False))
    # 3. Model known (STATIC by construction — closed dropdown).
    out.append(('Model known', True,
                f"{cleaned['vendor']} {cleaned['model']} has templates.",
                '—', False))
    # 4. Credential reference (STATIC in preview — escrow lands with RBAC).
    out.append(('Credential reference', None,
                'Credentials resolve at provision time.',
                'Admin: pick the sealed escrow entry when provisioning.', False))
    # 5. Journal preview (STATIC — pattern stated).
    out.append(('Journal path preview', None,
                'Journals pull hourly per ATM once an EJ hostname is set.',
                'Nothing to do now.', False))
    return out


def _blocking(checks):
    """Checks that must stop the wizard (see onboard_rules.BLOCKING)."""
    return [c for c in checks or [] if len(c) > 4 and c[4]]


# The four steps of the flow, in order. `stage` is which one is open: it is
# what turns a growing page into a wizard where exactly one thing is in view.
STAGES = ('facts', 'checks', 'provision', 'verify')


def _page(values, errors, checks, stage='facts', **extra):
    """Every render passes the same context, so a wizard step can never forget
    a value and silently blank the form."""
    ctx = {'vendors': VENDORS, 'districts': DISTRICTS, 'values': values,
           'errors': errors, 'checks': checks, 'stage': stage,
           'saved': False, 'plan': None, 'provision': None, 'verification': None,
           'save_errors': {}, 'blocking': [], 'step_error': ''}
    ctx.update(extra)
    ctx['blocking'] = _blocking(ctx.get('checks'))
    return render_template('onboarding.html', **ctx)


def _saved_values(atm_id):
    """The ATM as stored, so steps 3-4 render the record that exists rather
    than an empty form (which read as "my data vanished")."""
    fields = ('atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
              'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
              'ip_address')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT " + ", ".join(fields) + " FROM atm_locations "
                        "WHERE atm_id = %s", (atm_id,))
            row = cur.fetchone()
            cur.close()
        if row:
            return {f: (v if v is not None else '') for f, v in zip(fields, row)}
    except Exception as e:
        logger.warning('onboard: could not reload %s: %s', atm_id, e)
    return {'atm_id': atm_id}


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
        return _page({}, {}, None, stage='facts')
    values = _form_values()
    cleaned, errors = validate_facts(values)
    if errors:
        return _page(values, errors, None, stage='facts')
    return _page(cleaned, {}, _preflight(cleaned), stage='checks')


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
        return _page(values, errors, None, stage='facts')
    # to_atm_data fills the columns the writer requires but this page never
    # collects (status), so a save cannot fail on an invisible field.
    data = normalise_atm_form(to_atm_data(values))
    # ej_hostname is optional in the wizard; keep whatever is already stored.
    data['ej_hostname'] = (request.form.get('ej_hostname') or '').strip() or None
    res = save_atm_record(data, source='onboarding-wizard')
    if not res['ok']:
        return _page(cleaned, res['errors'], _preflight(cleaned), stage='provision',
                     saved=False, save_errors=res['errors'])
    log_action('ATM_SAVE', res['audit_detail'])
    plan = provision_atms([data['atm_id']], dry_run=True, actor='onboarding-wizard')
    return _page(cleaned, {}, _preflight(cleaned), stage='provision',
                 saved=True, plan=plan)


@bp.route('/onboard/provision', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_provision():
    """Step 3b — actually create/update the Zabbix host, through provision.py
    (the single provisioning writer Registration also uses)."""
    atm_id = (request.form.get('atm_id') or '').strip()
    if not atm_id:
        return _page(_form_values(), {'atm_id': 'ATM ID is required.'}, None,
                     stage='provision')
    plan = provision_atms([atm_id], dry_run=True, actor='onboarding-wizard')
    out = provision_atms([atm_id], dry_run=False, actor='onboarding-wizard')
    summary = out.get('summary') or {}
    # A failed transition must say so: an empty card here reads as "done".
    step_error = out.get('error') or (plan.get('error') if plan else '')
    log_action('ZABBIX_SYNC', f'wizard: {atm_id} created={summary.get("created", 0)} '
                              f'updated={summary.get("updated", 0)} '
                              f'skipped={summary.get("skipped", 0)} '
                              f'failed={summary.get("failed", 0)}')
    return _page(_saved_values(atm_id), {}, None, stage='provision', saved=True,
                 plan=plan, provision=out, step_error=step_error)


@bp.route('/onboard/verify', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_verify():
    """Step 4 — did it actually report? Read-only, and the only thing allowed
    to say an ATM is live."""
    atm_id = (request.form.get('atm_id') or '').strip()
    if not atm_id:
        return _page({}, {'atm_id': 'Save the ATM first.'}, None, stage='provision')
    # values.atm_id is the only field this step needs.
    out = verify_atm(atm_id)
    return _page(_saved_values(atm_id), {}, None, stage='verify', saved=True,
                 verification=out,
                 step_error=('' if out.get('verdict') != 'unknown' else
                             (out.get('blockers') or ['Zabbix could not be read'])[0]))


@bp.route('/onboard/stage', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_stage():
    """Open a specific step (the collapsed summaries link here).

    Recomputes exactly what that step needs, so a step opened later shows the
    CURRENT truth rather than a stale result from when it first ran.
    """
    stage = (request.form.get('stage') or 'facts').strip()
    atm_id = (request.form.get('atm_id') or '').strip()
    if stage not in STAGES:
        stage = 'facts'
    values = _saved_values(atm_id) if atm_id else {}
    checks = plan = provision = verification = None
    if stage == 'checks' and values.get('atm_id'):
        try:
            cleaned, errs = validate_facts(values)
            checks = _preflight(cleaned) if not errs else None
        except Exception as e:
            logger.warning('onboard stage checks failed: %s', e)
    elif stage == 'provision' and atm_id:
        plan = provision_atms([atm_id], dry_run=True, actor='onboarding-wizard')
    elif stage == 'verify' and atm_id:
        verification = verify_atm(atm_id)
    return _page(values, {}, checks, stage=stage, saved=bool(atm_id), plan=plan,
                 provision=provision, verification=verification)
