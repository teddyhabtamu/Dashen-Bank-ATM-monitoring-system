"""report-portal/provision.py — the single provisioning entry point for ATMs.

Why this exists: the Zabbix side of onboarding lived behind
`zabbix_sync.sync_hosts`, called from a background thread in the Registration
blueprint. That worked, but it had no front door — no dry run, no per-ATM
result a UI could render, and nothing an onboarding wizard could call. Adding a
second caller meant the risk of two writers drifting apart, which is the exact
failure mode this module removes.

Rules this module enforces:
  * The DATABASE row is the source of truth, not a submitted form dict. A
    caller that passes stale or half-filled facts cannot mis-provision.
  * Unknown vendor/model stops the ATM instead of guessing a template.
  * dry_run=True issues reads only — no host.create/update, no hostgroup.create.
  * Never raises. Zabbix or DB trouble comes back as a result with a reason.

Scope per ATM is deliberately small: host, interface + SNMP credentials,
{$ATM_VENDOR} macro, vendor template link, host groups. Templates, items,
triggers and the EJ pull list are NOT touched here — those are shared or
owned elsewhere (templates are imported once; EJ targets come from
scripts/ej_pull_targets.py reading this same table).
"""
import logging
import os

from zabbix_sync import plan_hosts, sync_hosts_detailed

logger = logging.getLogger(__name__)

# Vendor -> the Zabbix template it must be linked to. Anything not listed here
# is an unknown vendor: stop and send it to review rather than attaching a
# template that may not fit the hardware.
VENDOR_TEMPLATES = {
    'NCR': 'Dashen Bank ATM Hardware',
    'GRG': 'Dashen Bank ATM Hardware - GRG',
}

COLUMNS = ('atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
           'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
           'install_date', 'status', 'ip_address', 'snmp_community',
           'snmp_version', 'ej_log_format', 'ej_hostname')


def _db():
    from db import get_db
    return get_db()


def load_atms(atm_ids=None, active_only=False):
    """Authoritative ATM rows. None = every ATM (reconcile/repair runs)."""
    sql = f"SELECT {', '.join(COLUMNS)} FROM atm_locations"
    args = []
    if atm_ids:
        ids = [i for i in (atm_ids or []) if (i or '').strip()]
        if not ids:
            return []
        sql += " WHERE atm_id = ANY(%s)"
        args.append(ids)
    elif active_only:
        sql += " WHERE LOWER(COALESCE(status, 'active')) = 'active'"
    sql += " ORDER BY atm_id"
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(sql, args or None)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
    return rows


def load_live_atm_ids():
    """Every atm_id currently in the registry, for orphan-host adoption.

    A Zabbix host whose technical name is absent from this set cannot belong
    to any live portal ATM. The sync may safely re-adopt it when it still owns
    an ATM's visible name after a delete/re-import. None on a DB read failure,
    which makes the sync fall back to disabled-only adoption.
    """
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id FROM atm_locations")
            ids = {str(row[0]) for row in cur.fetchall() if row[0]}
            cur.close()
        return ids
    except Exception as e:
        logger.warning('provision: could not read live ATM ids: %s', e)
        return None


def review_queue(rows):
    """ATMs this system must not provision on its own: unknown vendor, or a
    vendor with no template. They are reported, never silently skipped."""
    out = []
    for r in rows or []:
        vendor = (r.get('vendor') or '').strip().upper()
        if vendor not in VENDOR_TEMPLATES:
            out.append({'atm_id': r.get('atm_id'),
                        'reason': f'unknown vendor "{vendor or "?"}" — needs engineer review'})
    return out


def provision_atms(atm_ids=None, dry_run=True, actor='', active_only=False,
                   max_batch=None):
    """Provision ATM hosts in Zabbix from the database.

    Returns:
        {'dry_run', 'actor', 'ok', 'summary', 'results', 'review', 'error'}

    results[i] = {'atm_id', 'action', 'changes', 'error', 'pending_groups'}
      action is one of: create, update, skipped, no_ip, conflict, review, failed

    Default is dry_run=True on purpose: a caller has to opt in to writing.
    """
    result = {'dry_run': bool(dry_run), 'actor': actor or 'system', 'ok': False,
              'summary': {}, 'results': [], 'review': [], 'error': ''}
    try:
        rows = load_atms(atm_ids, active_only=active_only)
    except Exception as e:
        result['error'] = f'Could not read the ATM records: {e}'
        logger.warning('provision: DB read failed: %s', e)
        return result
    if not rows:
        result['ok'] = True
        result['error'] = 'No matching ATM records.'
        return result

    # Defence in depth: load_atms() filters by id in SQL, but a caller that
    # was passed 3 ATMs must never have 4 provisioned because a future change
    # to the query widened it. Intersect explicitly before anything is written.
    if atm_ids:
        wanted = {i for i in (atm_ids or []) if (i or '').strip()}
        rows = [r for r in rows if r.get('atm_id') in wanted]
        if not rows:
            result['ok'] = True
            result['error'] = 'No matching ATM records.'
            return result

    blocked = {b['atm_id']: b['reason'] for b in review_queue(rows)}
    passable = [r for r in rows if r['atm_id'] not in blocked]
    live_ids = load_live_atm_ids()

    try:
        if dry_run:
            # max_batch is threaded through so a read-only fleet report can
            # plan every ATM instead of the write path's default slice.
            summary, outcomes = (
                plan_hosts(passable, max_batch=max_batch,
                           live_atm_ids=live_ids)
                if max_batch else
                plan_hosts(passable, live_atm_ids=live_ids))
        else:
            summary, outcomes = sync_hosts_detailed(
                passable, live_atm_ids=live_ids)
    except Exception as e:  # both are best-effort, but never trust that
        result['error'] = f'Zabbix call failed: {e}'
        logger.warning('provision: %s', e)
        return result

    for p in outcomes:
        action = p['action']
        err = p.get('error') or ''
        if not dry_run and action in ('create', 'update') and not p.get('applied'):
            action = 'failed'          # it was planned, the write did not land
        if err.startswith('skipped: unknown vendor'):
            action = 'review'
        result['results'].append({
            'atm_id': p['atm_id'], 'action': action,
            'changes': p.get('changes') or [],
            'pending_groups': p.get('pending_groups') or [],
            'error': err})

    for aid, why in blocked.items():
        result['review'].append({'atm_id': aid, 'reason': why})
        result['results'].append({'atm_id': aid, 'action': 'review', 'changes': [],
                                  'pending_groups': [], 'error': why})

    result['summary'] = summary
    result['ok'] = not summary.get('failed')

    # Every write path (registration, wizard, manual retry, worker) records
    # its outcome here. A dry run never touches the state table: planning is
    # not evidence that anything was provisioned.
    if not dry_run:
        try:
            from provision_state import record_outcomes
            record_outcomes(result['results'])
        except Exception as e:  # noqa: BLE001 - the Zabbix write already landed
            logger.warning('provision state update failed: %s', e)
    return result


def provision_one(atm_id, dry_run=True, actor=''):
    """Single-ATM convenience wrapper — the shape the wizard's Step 2 needs."""
    return provision_atms([atm_id], dry_run=dry_run, actor=actor)


def reindex_active(dry_run=True, actor='reconcile'):
    """Reconcile every active ATM. Idempotent: hosts already correct are
    reported as skipped. This is the repair path after a Zabbix restore, a
    template change, or a bulk import — no script editing."""
    return provision_atms(None, dry_run=dry_run, actor=actor, active_only=True)