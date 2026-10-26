"""report-portal/verify.py — did this ATM actually start reporting?

Phase 3 of onboarding. Provisioning a host is not the same as a working ATM:
the host can exist, be enabled, have every item, and still have collected
nothing because the box is unreachable, the agent is missing, or the WMI
collector was never installed. Marking such an ATM "live" is how a rollout
ends up green on paper and blind in production.

So: an ATM is only live when the evidence says so. This module gathers that
evidence and returns a verdict. It changes nothing — no writes, ever — so it
is safe to run on the whole fleet.

Evidence sources:
  * Zabbix  — does the host exist, is it monitored, do items actually report,
              are they unsupported (agent missing / key wrong), how fresh.
  * Portal  — the DB row and the last recorded state.

Verdicts:
  reporting               — items reporting within the freshness window
  provisioned_not_reporting — the host is there but nothing (fresh) is arriving
  not_provisioned         — no Zabbix host at all

Usage (inside the portal container):
  python3 verify.py ATM-TEST-NCR
  python3 verify.py --window 30            # freshness window in minutes
  python3 verify.py --json                 # machine-readable
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))  # the system reports everything in EAT

# An ATM is only reporting when ITS OWN telemetry arrives. Counting "at least
# one fresh item" is not enough: Zabbix-internal items (agent.available,
# host.uptime) always tick, so a dead host still looks alive - which is exactly
# how this gate reported a dead ATM as OK on its first UAT run.
# These key sets mirror zabbix_modules/status_rules.py:
#   NCR  -> WMI master item (atm.parts), the authoritative hardware feed
#   GRG  -> XFS CDM status + cassette inventory, with the legacy SNMP keys
#           accepted while old GRG hosts are still being migrated
AUTHORITATIVE_KEYS = {
    'NCR': [['atm.parts']],
    'GRG': [
        ['atm.xfs.cdm.device_status', 'atm.xfs.cdm.cassette.count[0050A]'],
        ['atm.status', 'atm.net.link', 'atm.cash.cassette1'],
    ],
}


# Chunk size for the batched sweep. 1,300 hosts x ~90 items is ~117k item rows
# in a single item.get response - tens of megabytes of JSON in one gunicorn
# worker. Chunking keeps each payload sane while still collapsing ~3,900 API
# calls into a couple of dozen.
ZABBIX_CHUNK = int(os.environ.get('FLEET_ZABBIX_CHUNK', '250'))

ITEM_OUT = ['itemid', 'hostid', 'key_', 'lastclock', 'state', 'type']


def _blank_facts():
    return {'host': None, 'items_total': 0, 'items_reporting': 0,
            'items_unsupported': 0, 'items_internal': 0, 'keys_reporting': 0,
            'newest_data_age_seconds': None, 'key_clocks': {},
            'problems': [], 'agent_interface': False, 'interfaces': [],
            'error': ''}


def _zabbix_snapshot(client, atm_ids):
    """Whole-fleet Zabbix evidence in a handful of calls, not three per ATM.

    verify_atm() asked Zabbix the same three questions once per machine:
    host.get, item.get, trigger.get - plus a FOURTH host.get that re-selected
    interfaces the first call had already returned. At 1,300 ATMs that is ~3,900
    serial HTTP round-trips and 1,300 separate logins, because verify_atm is
    called with client=None and builds a fresh _Client each time.

    Zabbix's API is a batch API: item.get and trigger.get both accept an ARRAY
    of hostids. This fetches the fleet in chunks and hands each ATM its slice,
    so the per-ATM logic is unchanged and the call count stops scaling.

    Returns {atm_id: facts}. Never raises: a failure marks every ATM as
    'could not read Zabbix' rather than inventing a verdict.
    """
    out = {a: _blank_facts() for a in atm_ids}
    if not atm_ids:
        return out

    # Chunked for the same reason as item.get: the cap has to hold for every
    # call, or the fleet size quietly reappears inside one request.
    hosts, failed = [], False
    for i in range(0, len(atm_ids), ZABBIX_CHUNK):
        part = client.call('host.get', {
            # 'host' is the TECHNICAL name - the one that equals atm_id, and the
            # one the filter below matches on. 'name' is the visible name and is
            # the ATM's display name ("4 Kilo Test NCR"), NOT its id.
            #
            # Matching on 'name' reported every real ATM as "not provisioned":
            # nothing in the fleet is called ATM-TEST-NCR by that field. It only
            # looked right in tests because the fake hosts had name == id.
            'output': ['hostid', 'host', 'status'],
            'selectInterfaces': ['interfaceid', 'type', 'port', 'ip'],
            'filter': {'host': list(atm_ids[i:i + ZABBIX_CHUNK])}})
        if part is None:
            failed = True
            break
        hosts.extend(part)
    if failed:
        err = getattr(client, 'last_error', '') or 'Zabbix API returned no result'
        for f in out.values():
            f['error'] = err
        return out

    for h in hosts:
        f = out.get(h.get('host') or h.get('name'))
        if f is None:
            continue
        f['host'] = h
        f['status_enabled'] = str(h.get('status')) == '0'
        for i in (h.get('interfaces') or []):
            f['interfaces'].append("%s:%s" % (i.get('type'), i.get('port')))
            if str(i.get('type')) == '1':
                f['agent_interface'] = True

    hostids = [h['hostid'] for h in hosts]
    now = int(time.time())
    for i in range(0, len(hostids), ZABBIX_CHUNK):
        chunk = hostids[i:i + ZABBIX_CHUNK]
        items = client.call('item.get', {'hostids': chunk, 'output': ITEM_OUT,
                                         'filter': {'status': 0}}) or []
        probs = client.call('trigger.get', {'hostids': chunk,
                                            'output': ['triggerid', 'hostid', 'description', 'priority'],
                                            'filter': {'value': 1, 'status': 0}}) or []
        items_by_host = {}
        for it in items:
            items_by_host.setdefault(it.get('hostid'), []).append(it)
        probs_by_host = {}
        for pr in probs:
            probs_by_host.setdefault(pr.get('hostid'), []).append(pr.get('description'))
        for h in hosts:
            if h['hostid'] not in chunk:
                continue
            f = out.get(h.get('host') or h.get('name'))
            if f is None:
                continue
            newest = 0
            for it in items_by_host.get(h['hostid'], []):
                clock = int(it.get('lastclock') or 0)
                key = str(it.get('key_') or '')
                # Zabbix-internal items are identified by their KEY, not their
                # type: internal items (zabbix[...]) are type 0, but so is every
                # passive agent item - including atm.parts, the UserParameter
                # master item this gate exists to require.
                if key.lower().startswith('zabbix['):
                    f['items_internal'] += 1
                    continue
                if clock:
                    f['items_reporting'] += 1
                    f['keys_reporting'] += 1
                    newest = max(newest, clock)
                if str(it.get('state')) == '1':
                    f['items_unsupported'] += 1
                if clock and key:
                    f['key_clocks'][key] = clock
            f['items_total'] = f['items_reporting'] + f['items_unsupported'] + f['items_internal']
            if newest:
                f['newest_data_age_seconds'] = now - newest
            f['problems'] = [d for d in probs_by_host.get(h['hostid'], []) if d]
    return out


def _db_snapshot(atm_ids):
    """Portal evidence for the whole fleet in two queries instead of two per ATM."""
    out = {a: {'row': {}, 'state': '', 'last_seen': '', 'db_error': ''} for a in atm_ids}
    if not atm_ids:
        return out
    try:
        from db import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id, vendor, model, status, ip_address, ej_hostname "
                        "FROM atm_locations WHERE atm_id = ANY(%s)", (list(atm_ids),))
            for r in cur.fetchall():
                if r[0] in out:
                    out[r[0]]['row'] = {'atm_id': r[0], 'vendor': r[1], 'model': r[2],
                                        'status': r[3], 'ip_address': r[4],
                                        'ej_hostname': r[5]}
            cur.execute("SELECT atm_id, state, last_seen FROM atm_current_state "
                        "WHERE atm_id = ANY(%s)", (list(atm_ids),))
            for r in cur.fetchall():
                if r[0] in out:
                    out[r[0]]['state'] = r[1] or ''
                    out[r[0]]['last_seen'] = r[2].isoformat() if r[2] else ''
            cur.close()
    except Exception as e:
        for f in out.values():
            f['db_error'] = str(e)
    return out


# ── evidence: Zabbix ──────────────────────────────────────────────────────
def _zabbix_facts(atm_id, client):
    """Read-only Zabbix evidence. Returns a dict; never raises."""
    out = {'host': None, 'items_total': 0, 'items_reporting': 0,
           'items_unsupported': 0, 'items_internal': 0, 'keys_reporting': 0,
           'newest_data_age_seconds': None, 'key_clocks': {},
           'problems': [], 'agent_interface': False, 'interfaces': [],
           'error': ''}
    hosts = client.call('host.get', {'output': ['hostid', 'status'],
                                     'selectInterfaces': ['interfaceid', 'type', 'port', 'ip'],
                                     'filter': {'host': [atm_id]}})
    if hosts is None:
        # Could not ask, as opposed to asked and found nothing. Reporting is
        # then UNKNOWN — never assume the worst, never assume the best.
        out['error'] = getattr(client, 'last_error', '') or 'Zabbix API returned no result'
        return out
    if not hosts:
        return out
    host = hosts[0]
    out['host'] = host
    out['status_enabled'] = str(host.get('status')) == '0'

    items = client.call('item.get', {'hostids': [host['hostid']],
                                     'output': ['itemid', 'key_', 'lastclock', 'state', 'type'],
                                     'filter': {'status': 0}}) or []
    out['items_total'] = len(items)
    out['items_internal'] = 0
    out['keys_reporting'] = 0
    newest = 0
    for it in items:
        clock = int(it.get('lastclock') or 0)
        # Zabbix-internal items are identified by their KEY, not their type:
        # internal items (zabbix[...]) are type 0, but so is every passive
        # agent item - including atm.parts, the UserParameter master item this
        # gate exists to require. Keying off type discarded the one item that
        # matters and reported a live ATM as silent.
        if str(it.get('key_') or '').lower().startswith('zabbix['):
            # These report whether Zabbix itself is alive, NOT whether the ATM
            # is. They must never count as evidence that an ATM is reporting.
            out['items_internal'] += 1
            continue
        if clock:
            out['items_reporting'] += 1
            out['keys_reporting'] += 1
            newest = max(newest, clock)
        if str(it.get('state')) == '1':
            out['items_unsupported'] += 1
        if clock and it.get('key_'):
            out['key_clocks'][it['key_']] = clock
    if newest:
        out['newest_data_age_seconds'] = int(time.time()) - newest

    probs = client.call('trigger.get', {'hostids': [host['hostid']],
                                        'output': ['triggerid', 'description', 'priority'],
                                        'filter': {'value': 1, 'status': 0}}) or []
    out['problems'] = [p.get('description') for p in probs]

    ifaces = client.call('host.get', {'output': ['hostid'],
                                      'selectInterfaces': ['type', 'port', 'ip'],
                                      'hostids': [host['hostid']]}) or []
    for h in ifaces:
        for i in (h.get('interfaces') or []):
            out['interfaces'].append(f"{i.get('type')}:{i.get('port')}")
            if str(i.get('type')) == '1':
                out['agent_interface'] = True
    return out


# ── evidence: portal ──────────────────────────────────────────────────────
def _db_facts(atm_id):
    """Read-only portal evidence. Missing DB is not fatal — the Zabbix half
    still gives a verdict, it just cannot cross-check the record."""
    out = {'row': {}, 'state': '', 'last_seen': '', 'db_error': ''}
    try:
        from db import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id, vendor, model, status, ip_address, ej_hostname "
                        "FROM atm_locations WHERE atm_id = %s", (atm_id,))
            r = cur.fetchone()
            if r:
                out['row'] = {'atm_id': r[0], 'vendor': r[1], 'model': r[2],
                              'status': r[3], 'ip_address': r[4], 'ej_hostname': r[5]}
            cur.execute("SELECT state, last_seen FROM atm_current_state WHERE atm_id = %s", (atm_id,))
            s = cur.fetchone()
            if s:
                out['state'] = s[0] or ''
                out['last_seen'] = s[1].isoformat() if s[1] else ''
            cur.close()
    except Exception as e:
        out['db_error'] = str(e)
    return out


# ── the verdict ───────────────────────────────────────────────────────────
def _check(name, status, detail):
    return {'name': name, 'status': status, 'detail': detail}


def verify_atm(atm_id, window_minutes=15, client=None, db_facts=None,
               zabbix_facts=None):
    """Decide whether an ATM is genuinely reporting. Read-only; never raises.

    zabbix_facts / db_facts / client are injection points. The fleet sweep hands
    this function a slice of one batched snapshot; the onboarding wizard hands it
    nothing, so the single-ATM path is unchanged.
    """
    checks, blockers = [], []
    vendor = 'NCR'
    if zabbix_facts is not None:
        z = zabbix_facts
        if z.get('error') and not z.get('host'):
            checks.append(_check('zabbix', 'fail',
                                f"Could not read Zabbix: {z['error']}"))
    else:
        z = _blank_facts()
        try:
            if client is None:
                from zabbix_sync import _Client
                client = _Client()
                client.login()
            z = _zabbix_facts(atm_id, client)
        except Exception as e:
            z['error'] = str(e)
            checks.append(_check('zabbix', 'fail', f'Could not read Zabbix: {e}'))

    db = db_facts if db_facts is not None else _db_facts(atm_id)
    if db.get('row'):
        vendor = db['row'].get('vendor') or vendor
    elif db.get('db_error'):
        checks.append(_check('portal record', 'warn', f"Could not read the ATM record: {db['db_error']}"))
    else:
        checks.append(_check('portal record', 'fail', f'No ATM record named {atm_id}'))
        blockers.append('No ATM record — onboard it before checking reporting.')

    if z.get('error') and not z.get('host'):
        verdict, ok = 'unknown', False
        blockers.append('Zabbix could not be read, so reporting is unverified.')
        return _result(atm_id, verdict, ok, checks, blockers, z, db, window_minutes)

    if not z.get('host'):
        checks.append(_check('zabbix host', 'fail', 'No Zabbix host with this name.'))
        blockers.append('Not provisioned: run the provisioner (wizard Step 2 / Registration save).')
        return _result(atm_id, 'not_provisioned', False, checks, blockers, z, db, window_minutes)

    checks.append(_check('zabbix host', 'pass',
                         'Host exists and is monitored.'
                         if z.get('status_enabled') else 'Host exists but is DISABLED.'))
    if not z.get('status_enabled'):
        blockers.append('Zabbix host is disabled — it was deleted in the portal or switched off.')

    # NCR hardware facts ride the Zabbix agent; without that interface the WMI
    # items can never collect, however healthy the SNMP side looks.
    if vendor == 'NCR' and z.get('interfaces') and not z.get('agent_interface'):
        # Only judge this when interfaces were actually readable. An empty
        # read means the query told us nothing, and inventing a fault from a
        # failed lookup is the same mistake this gate exists to stop.
        checks.append(_check('agent interface', 'fail',
                             'No Zabbix agent interface (type 1 / 10050) — WMI items cannot collect.'))
        blockers.append('NCR host has no agent interface: add one, and confirm the agent runs on the box.')

    total, reporting, unsup = z['items_total'], z['items_reporting'], z['items_unsupported']
    age = z['newest_data_age_seconds']
    age_txt = f'{age // 60} min' if age is not None else 'never'

    # The decisive check: the vendor's own keys must be arriving.
    groups = AUTHORITATIVE_KEYS.get(vendor) or AUTHORITATIVE_KEYS['NCR']
    wanted = max(groups, key=lambda group: sum(1 for k in group if z['key_clocks'].get(k)))
    seen = [k for k in wanted if z['key_clocks'].get(k)]
    auth_ok = len(seen) == len(wanted)
    if z['items_internal'] and not z['items_reporting']:
        checks.append(_check('internal items', 'fail',
                             f"Only Zabbix-internal items are reporting ({z['items_internal']}). "
                             'They track Zabbix itself, not the ATM, so they prove nothing here.'))
    if auth_ok:
        age_auth = max(int(time.time()) - z['key_clocks'][k] for k in seen)
        checks.append(_check('telemetry', 'pass',
                             f"Vendor telemetry arriving ({', '.join(seen)}), newest {age_auth // 60} min ago."))
    else:
        missing = [k for k in wanted if k not in z['key_clocks']]
        if vendor == 'NCR':
            cause = ('NCR: the Zabbix agent on the box is not answering on 10050, '
                     'or atm-wmi-collector.ps1 is not installed.')
        elif any(k.startswith('atm.xfs.') for k in missing):
            cause = 'GRG: no XFS data — check the Zabbix agent and grg-xfs-probe.ps1.'
        else:
            cause = 'GRG: no SNMP data — check the UDP/161 firewall and community.'
        checks.append(_check('telemetry', 'fail',
                             f"Vendor telemetry never arrived: {', '.join(missing)}. " + cause))
        blockers.append(f"No {vendor} telemetry ({', '.join(missing)} never collected).")

    if total == 0:
        checks.append(_check('items', 'fail', 'Host has no enabled items — is a template linked?'))
        blockers.append('No items: the vendor template is not linked to this host.')
    elif reporting == 0:
        detail = f'No item has ever collected ({total} items, 0 reporting).'
        if unsup == total:
            detail += ' All are UNSUPPORTED — the Zabbix agent is not answering on the box.'
        else:
            detail += ' Check the IP, the firewall, and that the Zabbix agent service is running.'
        checks.append(_check('items', 'fail', detail))
        blockers.append(detail)
    else:
        fresh = age is not None and age <= window_minutes * 60
        checks.append(_check('items', 'pass' if fresh else 'warn',
                             f'{reporting}/{total} items reporting, newest data {age_txt} ago.'))

    if db.get('state'):
        checks.append(_check('portal state', 'info',
                             f"Recorded state {db['state']}"
                             + (f", last seen {db['last_seen']}" if db.get('last_seen') else '')))
    if db['row'].get('ej_hostname'):
        checks.append(_check('journal', 'info',
                             f"EJ pull configured for {db['row']['ej_hostname']} (hourly)."))
    else:
        checks.append(_check('journal', 'skip',
                             'No EJ hostname set — no journal pull. Set it in ATM Registration; '
                             'it names the local evidence file for NCR and GRG.'))
    if z['problems']:
        checks.append(_check('problems', 'info',
                             f"{len(z['problems'])} firing trigger(s): " + '; '.join(z['problems'][:3])))

    fresh = (auth_ok and reporting > 0
             and age is not None and age <= window_minutes * 60)
    # Fresh data alone is not "live": a disabled host or an NCR host with no
    # agent interface cannot report hardware no matter what its items say.
    if fresh and not blockers:
        verdict, ok = 'reporting', True
    else:
        verdict, ok = 'provisioned_not_reporting', False
    return _result(atm_id, verdict, ok, checks, blockers, z, db, window_minutes)


def _result(atm_id, verdict, ok, checks, blockers, z, db, window_minutes):
    return {
        'atm_id': atm_id, 'verdict': verdict, 'ok': ok,
        'checked_at': datetime.now(EAT).strftime('%Y-%m-%d %H:%M EAT'),
        'window_minutes': window_minutes,
        'checks': checks, 'blockers': blockers,
        'facts': {'items_total': z['items_total'],
                  'items_reporting': z['items_reporting'],
                  'items_internal': z['items_internal'],
                  'items_unsupported': z['items_unsupported'],
                  'newest_data_age_seconds': z['newest_data_age_seconds'],
                  'interfaces': z['interfaces'],
                  'vendor': db['row'].get('vendor'), 'ip_address': db['row'].get('ip_address'),
                  'state': db.get('state')},
    }


# ── fleet / CLI ───────────────────────────────────────────────────────────
def verify_all(atm_ids=None, window_minutes=15, client=None):
    if atm_ids is None:
        try:
            from db import get_db
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT atm_id FROM atm_locations ORDER BY atm_id")
                atm_ids = [r[0] for r in cur.fetchall()]
                cur.close()
        except Exception:
            atm_ids = []
    results = [verify_atm(a, window_minutes=window_minutes, client=client) for a in atm_ids]
    return {'checked': len(results),
            'reporting': sum(1 for r in results if r['verdict'] == 'reporting'),
            'not_reporting': sum(1 for r in results if r['verdict'] == 'provisioned_not_reporting'),
            'not_provisioned': sum(1 for r in results if r['verdict'] == 'not_provisioned'),
            'results': results}


def fleet(atm_ids=None, window_minutes=15, with_plan=True,
          verify_fn=None, plan_fn=None, heartbeat=None):
    """The fleet answer: one row per ATM, worst first, with the next action.

    Read-only. `with_plan` adds a provisioning DRY RUN so the report can also
    say "these hosts would be created/repaired" without writing anything.

    verify_fn / plan_fn exist for tests: they pin the return-shape contract of
    the two workers, which is exactly where this function went wrong once.

    heartbeat is called at each phase boundary. A sweep can legitimately outlast
    the claim window on a large fleet or a slow Zabbix server, and without a
    liveness signal another page load would see an expired claim and start a
    SECOND sweep over the same cache row.
    """
    from fleet_report import counts, recommend_next_action, summarise, to_csv
    if atm_ids is None:
        try:
            from db import get_db
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT atm_id FROM atm_locations ORDER BY atm_id")
                atm_ids = [r[0] for r in cur.fetchall()]
                cur.close()
        except Exception:
            atm_ids = []
    verify_fn = verify_fn or verify_atm

    # One Zabbix login and one batched sweep for the whole fleet, instead of a
    # fresh client (and login) plus three API calls per ATM. At 1,300 ATMs the
    # old path was ~3,900 serial round-trips - this is a couple of dozen, and
    # the cost no longer scales with the fleet.
    def beat():
        if heartbeat:
            try:
                heartbeat()
            except Exception as e:
                logger.warning('fleet heartbeat callback failed: %s', e)

    results = None
    if verify_fn is verify_atm and atm_ids:
        try:
            from zabbix_sync import _Client
            client = _Client()
            client.login()
            zsnap = _zabbix_snapshot(client, atm_ids)
            dsnap = _db_snapshot(atm_ids)
            beat()
            results = [verify_atm(a, window_minutes=window_minutes, client=client,
                                  zabbix_facts=zsnap.get(a), db_facts=dsnap.get(a))
                       for a in atm_ids]
            beat()
        except Exception:
            # Fall back to the per-ATM path, which reports the failure honestly
            # per ATM rather than leaving the page spinning.
            logger.warning('fleet: batched sweep unavailable, falling back per ATM',
                           exc_info=True)
            results = None
    if results is None:
        results = [verify_fn(a, window_minutes=window_minutes) for a in atm_ids]
    plans = None
    if with_plan and atm_ids:
        try:
            if plan_fn is None:
                from provision import provision_atms
                plan_fn = provision_atms
            # provision_atms() returns a DICT: {ok, summary, results, review,
            # error, ...}. The rows the report needs live under 'results'.
            #
            # max_batch is raised to the fleet size: the 200 cap protects the
            # WRITE path, but this is a read-only dry run, and at the default
            # the report silently covered 200 of 1,300 ATMs while claiming to
            # describe the fleet. Truncating a report is not a safety property.
            planned = plan_fn(atm_ids, dry_run=True, actor='verify',
                              max_batch=max(len(atm_ids), 1))
            beat()
            plans = (planned or {}).get('results') or []
            if (planned or {}).get('error'):
                sys.stderr.write(f"verify: provisioning plan unavailable: "
                                 f"{planned.get('error')}\n")
        except Exception as e:
            sys.stderr.write(f'verify: could not plan provisioning: {e}\n')
    rows = summarise(results, plans)
    for r in rows:
        r['next_action'] = recommend_next_action(r)
    return {'counts': counts(rows), 'rows': rows, 'csv': to_csv(rows)}


def main(argv=None):
    ap = argparse.ArgumentParser(description='Verify ATM reporting (read-only).')
    ap.add_argument('atm_id', nargs='*', help='ATM IDs (default: every ATM)')
    ap.add_argument('--window', type=int, default=15, help='freshness window in minutes (default 15)')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--csv', action='store_true', help='fleet CSV (what is dark and why)')
    ap.add_argument('--no-plan', action='store_true',
                    help='skip the provisioning dry run (faster, less detail)')
    args = ap.parse_args(argv)

    if args.csv:
        print(fleet(atm_ids=args.atm_id or None, window_minutes=args.window,
                    with_plan=not args.no_plan)['csv'])
        return 0

    if args.atm_id:
        results = [verify_atm(a, window_minutes=args.window) for a in args.atm_id]
        summary = {'checked': len(results)}
    else:
        out = verify_all(window_minutes=args.window)
        results = out['results']
        summary = {k: out[k] for k in ('checked', 'reporting', 'not_reporting', 'not_provisioned')}

    if args.json:
        print(json.dumps({'summary': summary, 'results': results}, indent=1, default=str))
        return 0 if all(r['ok'] for r in results) else 1

    icon = {'reporting': 'OK  ', 'provisioned_not_reporting': 'SILENT',
            'not_provisioned': 'NONE ', 'unknown': '????'}
    for r in results:
        print(f"{icon.get(r['verdict'], '?')}  {r['atm_id']:<18} {r['verdict']}")
        for c in r['checks']:
            if c['status'] in ('fail', 'warn'):
                print(f"        [{c['status']}] {c['name']}: {c['detail']}")
    print('\n' + '  '.join(f"{k}={v}" for k, v in summary.items()))
    return 0 if all(r['ok'] for r in results) else 1


def main_fleet(argv=None):
    """Print the one-line-per-ATM fleet report with a next action per row."""
    import argparse as _ap
    ap = _ap.ArgumentParser(description='Fleet onboarding health (read-only).')
    ap.add_argument('atm_id', nargs='*')
    ap.add_argument('--window', type=int, default=15)
    ap.add_argument('--csv', action='store_true')
    args = ap.parse_args(argv)
    out = fleet(atm_ids=args.atm_id or None, window_minutes=args.window)
    if args.csv:
        print(out['csv'])
        return 0
    for r in out['rows']:
        mark = 'ACTION' if r['needs_action'] else 'ok'
        print(f"{mark:<6}  {r['atm_id']:<18} {r['status_label']:<28} "
              f"{r['vendor']:<4} {r['ip']:<15} {r['summary']}")
        if r['needs_action']:
            print(f"      next: {r['next_action']}")
    c = out['counts']
    print('\n' + '  '.join(f"{k}={c[k]}" for k in
                             ('total', 'healthy', 'needs_action', 'reporting',
                              'not_provisioned', 'failed', 'review')))
    return 0 if c['needs_action'] == 0 else 1


if __name__ == '__main__':
    # Default to the fleet report: for a rollout the question is never about
    # one ATM, it is "what is dark and what do I do about it".
    if '--single' in sys.argv:
        sys.argv.remove('--single')
        sys.exit(main())
    sys.exit(main_fleet())