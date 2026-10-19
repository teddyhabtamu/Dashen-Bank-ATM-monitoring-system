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
import threading

from flask import (Blueprint, Response, render_template, request,
                   redirect, url_for)
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


# ── Phase 4c: fleet page ─────────────────────────────────────────────────
# Verifying 1,202 ATMs is a Zabbix sweep per ATM. That cannot run inside a
# request, so it is computed in the background and served from a shared cache
# (fleet_store) that every gunicorn worker can read.
# Cross-process coordination lives in fleet_store (a DB claim), because
# gunicorn runs 3 workers and an in-memory flag would let two of them sweep the
# fleet at once. This lock only protects threads inside one process.
_FLEET_LOCK = threading.Lock()
_FLEET_RUNNING = False

# Schema DDL is a once-per-process job, not a per-view job. ensure() creates
# the cache table if absent and adds the heartbeat column; Postgres takes an
# ACCESS EXCLUSIVE lock for the ALTER even when the column already exists.
# Running that on every page load serialised concurrent views behind each other
# and, once one leaked transaction existed, made each view wait the full
# lock_timeout (5s). Retried on failure rather than assumed: a sweep that cannot
# reach its cache table should not be locked out until the next restart.
_FLEET_SCHEMA_READY = False
_FLEET_SCHEMA_LOCK = threading.Lock()


def _ensure_fleet_schema():
    global _FLEET_SCHEMA_READY
    if _FLEET_SCHEMA_READY:
        return
    from fleet_store import ensure
    with _FLEET_SCHEMA_LOCK:
        if _FLEET_SCHEMA_READY:
            return
        ensure(_db_conn())
        _FLEET_SCHEMA_READY = True


def _db_conn():
    """A pooled connection. fleet_store calls db() expecting a CONNECTION -
    passing the function itself is what broke every claim/save on first run."""
    from db import get_db
    return get_db()


def _compute_fleet(window_minutes=15):
    """Background worker: build the report and cache it.

    The caller has already claimed the run (fleet_store.claim_run), so this
    never starts a duplicate sweep. Failures are recorded so the page can show
    them instead of spinning.
    """
    global _FLEET_RUNNING
    from fleet_store import record_failure, save, touch_heartbeat
    with _FLEET_LOCK:
        if _FLEET_RUNNING:
            return
        _FLEET_RUNNING = True
    try:
        from verify import fleet
        # heartbeat=lambda: touch_heartbeat(db=_db_conn) keeps the claim alive
        # for as long as this worker is genuinely working. Without it a sweep
        # longer than RUN_WINDOW_MINUTES looks dead and a second one starts.
        out = fleet(window_minutes=window_minutes,
                    heartbeat=lambda: touch_heartbeat(db=_db_conn))
        ok = save({'counts': out['counts'], 'rows': out['rows'], 'error': ''}, db=_db_conn)
        if ok:
            log_action('FLEET_REPORT', f'fleet verified: {out["counts"]}')
        else:
            record_failure('Could not write the fleet report cache.', db=_db_conn)
    except Exception as e:
        logger.warning('fleet report failed: %s', e)
        record_failure(f'Fleet report failed: {e}', db=_db_conn)
    finally:
        with _FLEET_LOCK:
            _FLEET_RUNNING = False


@bp.route('/onboard/fleet', methods=['GET'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_fleet():
    """Fleet onboarding health. Read-only; computes in the background."""
    from fleet_report import format_eat
    from fleet_store import (DEFAULT_TTL_MINUTES, RUN_WINDOW_MINUTES, claim_run,
                            load_any, record_failure)
    ttl = int(request.args.get('ttl', DEFAULT_TTL_MINUTES))
    try:
        _ensure_fleet_schema()
    except Exception as e:
        logger.warning('fleet cache table unavailable: %s', e)
    cached = load_any(db=_db_conn)
    # A run that claimed the sweep and then died leaves the marker set with no
    # rows. Treating that as "still computing" is how this page spun forever:
    # the error was recorded but the route rendered the spinner regardless.
    if cached and cached.get('running') and not cached.get('rows') \
            and cached.get('age_minutes', 0) > RUN_WINDOW_MINUTES:
        record_failure('The previous fleet sweep did not finish. '
                       'Recompute, or check the portal logs.', db=_db_conn)
        cached = load_any(db=_db_conn)
    if cached and cached.get('failed'):
        # Show the failure even when there is no data to show.
        return render_template('fleet.html', cached=cached, computing=False,
                               counts=cached.get('counts') or {}, rows=[],
                               ttl=ttl, failed=cached.get('error'),
                               filter_mode=request.args.get('filter', 'action'),
                               q=(request.args.get('q') or '').strip())
    if not cached or not cached.get('rows'):
        # Nothing to show yet: claim the sweep (cross-process) and compute.
        if claim_run(db=_db_conn):
            threading.Thread(target=_compute_fleet, args=(ttl,), daemon=True).start()
        return render_template('fleet.html', cached=None, computing=True,
                               counts={}, rows=[], ttl=ttl, failed='',
                               filter_mode=request.args.get('filter', 'action'),
                               q=(request.args.get('q') or '').strip())
    cached['generated_label'] = format_eat(cached.get('generated_at'))
    rows = cached['rows']
    mode = request.args.get('filter', 'action')
    if mode == 'all':
        rows = list(rows)
    elif mode == 'healthy':
        rows = [r for r in rows if not r.get('needs_action')]
    else:
        rows = [r for r in rows if r.get('needs_action')]
    q = (request.args.get('q') or '').strip().lower()
    if q:
        rows = [r for r in rows
                if q in str(r.get('atm_id', '')).lower() or q in str(r.get('ip', '')).lower()
                or q in str(r.get('vendor', '')).lower()]
    # computing means "nothing to show yet, wait for the first sweep". A
    # recompute over an existing report is NOT that: the template already has a
    # separate branch (cached.running) that keeps the table and shows a banner.
    # Tying this to cached.running replaced a perfectly usable table with a
    # spinner on every filter click while a background sweep was in flight,
    # which read as "filtering is slow" when filtering is a list comprehension
    # over rows already in memory.
    return render_template('fleet.html', cached=cached,
                           computing=False,
                           counts=cached['counts'], rows=rows, ttl=ttl,
                           failed=(cached.get('error') if cached.get('failed') else ''),
                           filter_mode=mode,
                           q=(request.args.get('q') or '').strip())


@bp.route('/onboard/fleet/refresh', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_fleet_refresh():
    """Recompute now. Expensive on a large fleet, so it is a background job."""
    from fleet_store import claim_run
    try:
        _ensure_fleet_schema()
    except Exception as e:
        logger.warning('fleet cache table unavailable: %s', e)
    if claim_run(db=_db_conn):
        threading.Thread(target=_compute_fleet, daemon=True).start()
    return redirect(url_for('.onboard_fleet'))


@bp.route('/onboard/fleet/csv', methods=['GET'])
@login_required
@role_required(ROLE_ADMIN)
def onboard_fleet_csv():
    """The cached report as CSV - the rollout tracker file."""
    from fleet_store import load
    cached = load(db=_db_conn)
    if not cached:
        return Response('atm_id,status,needs_action,vendor,ip_address,'
                        'items_reporting,items_total,provisioning_action,'
                        'planned_changes,blocker\n',
                        mimetype='text/csv',
                        headers={'Content-Disposition':
                                 'attachment; filename=fleet_report.csv'})
    from fleet_report import to_csv
    return Response(to_csv(cached['rows']), mimetype='text/csv',
                    headers={'Content-Disposition':
                             'attachment; filename=fleet_report.csv'})
