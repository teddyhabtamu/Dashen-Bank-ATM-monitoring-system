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
import os
import sys
import time
from datetime import datetime, timezone, timedelta

EAT = timezone(timedelta(hours=3))  # the system reports everything in EAT


# ── evidence: Zabbix ──────────────────────────────────────────────────────
def _zabbix_facts(atm_id, client):
    """Read-only Zabbix evidence. Returns a dict; never raises."""
    out = {'host': None, 'items_total': 0, 'items_reporting': 0,
           'items_unsupported': 0, 'newest_data_age_seconds': None,
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
                                     'output': ['itemid', 'key_', 'lastclock', 'state'],
                                     'filter': {'status': 0}}) or []
    out['items_total'] = len(items)
    newest = 0
    for it in items:
        clock = int(it.get('lastclock') or 0)
        if clock:
            out['items_reporting'] += 1
            newest = max(newest, clock)
        if str(it.get('state')) == '1':
            out['items_unsupported'] += 1
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


def verify_atm(atm_id, window_minutes=15, client=None, db_facts=None):
    """Decide whether an ATM is genuinely reporting. Read-only; never raises."""
    checks, blockers = [], []
    vendor = 'NCR'
    z = {'host': None, 'items_total': 0, 'items_reporting': 0, 'items_unsupported': 0,
         'newest_data_age_seconds': None, 'problems': [], 'agent_interface': False,
         'interfaces': [], 'error': ''}

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
                             'No EJ hostname set — no journal pull (fine for GRG, optional for NCR).'))
    if z['problems']:
        checks.append(_check('problems', 'info',
                             f"{len(z['problems'])} firing trigger(s): " + '; '.join(z['problems'][:3])))

    fresh = reporting and age is not None and age <= window_minutes * 60
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


def main(argv=None):
    ap = argparse.ArgumentParser(description='Verify ATM reporting (read-only).')
    ap.add_argument('atm_id', nargs='*', help='ATM IDs (default: every ATM)')
    ap.add_argument('--window', type=int, default=15, help='freshness window in minutes (default 15)')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    args = ap.parse_args(argv)

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


if __name__ == '__main__':
    sys.exit(main())