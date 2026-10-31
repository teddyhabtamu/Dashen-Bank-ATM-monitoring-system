"""Automatic ingest of `atm.identity` from Zabbix.

The installer already runs the identity probe as SYSTEM and writes the result
to `verify-*.json`. The missing link was transport: the portal only saw the
JSON when a technician pasted it into the Agent page.

This module closes that link without adding a new network path:

    Zabbix server/proxy --TCP 10050--> agent `atm.identity`
        -> item lastvalue (text)
        -> portal reads the value through the Zabbix API
        -> parse/validate -> atm_agent_identity

Why `item.get(lastvalue)` and not `history.get`: identity is evidence, not a
time series. One current value per ATM is enough, and `lastvalue` avoids
pulling a week of JSON. The item itself collects hourly and keeps one day of
history, so a box that has been offline longer than that correctly reports no
current value.

Identity remains evidence, never authority: `save_identity` writes the side
table, and `conflicts` only reports differences. Nothing here writes
`atm_locations`.
"""
from __future__ import annotations

import logging
import time

from agent_registry import (load_identity, save_identity,
                            set_ej_hostname_if_empty)
from atm_identity import IdentityError, conflicts, parse
from audit import log_action
from log_safety import safe_log

logger = logging.getLogger(__name__)

ITEM_KEY = 'atm.identity'
ITEM_BATCH = 250
MAX_VALUE_AGE_SECONDS = 24 * 3600


def _client():
    from zabbix_sync import _Client
    client = _Client()
    client.login()
    if not client.token:
        raise RuntimeError(client.last_error or 'Zabbix login failed')
    return client


def _hosts_and_items(client, atm_ids):
    """Resolve hostids and the atm.identity item for a batch of ATM ids.

    One host.get and one item.get per 250 hosts, not per ATM. Returns
    (hosts, items); both are Zabbix API dicts.
    """
    ids = [str(i).strip() for i in (atm_ids or []) if str(i).strip()]
    hosts, items = [], []
    for start in range(0, len(ids), ITEM_BATCH):
        chunk = ids[start:start + ITEM_BATCH]
        part = client.call('host.get', {
            'output': ['hostid', 'host'],
            'filter': {'host': chunk},
        })
        if part is None:
            raise RuntimeError(client.last_error or 'Zabbix host.get returned no result')
        hosts.extend(part)
    hostids = [h['hostid'] for h in hosts]
    for start in range(0, len(hostids), ITEM_BATCH):
        chunk = hostids[start:start + ITEM_BATCH]
        part = client.call('item.get', {
            'hostids': chunk,
            'output': ['itemid', 'hostid', 'key_', 'lastvalue', 'lastclock',
                       'state', 'error'],
            'filter': {'key_': [ITEM_KEY]},
        })
        if part is None:
            raise RuntimeError(client.last_error or 'Zabbix item.get returned no result')
        items.extend(part)
    return hosts, items


def _atm_row(atm_id):
    """The registry fields conflicts() needs, from the authoritative row."""
    from provision import load_atms
    rows = load_atms([atm_id])
    return rows[0] if rows else None


def ingest_value(atm_id, raw, clock=None, source='zabbix:auto'):
    """Validate and store one identity value. Never raises on bad evidence.

    Returns {'status': stored|unchanged|rejected|no_atm, ...}. A rejected
    document changes nothing; an unchanged one avoids a needless write.
    """
    try:
        identity = parse(raw)
    except IdentityError as exc:
        logger.info('identity rejected for %s: %s',
                    safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF
        return {'status': 'rejected', 'error': str(exc)}

    stored = load_identity(atm_id)
    if stored and stored.get('payload') == identity:
        return {'status': 'unchanged', 'atm_id': atm_id}

    atm = _atm_row(atm_id)
    if not atm:
        return {'status': 'no_atm', 'atm_id': atm_id}

    conflict_rows = conflicts(identity, atm)
    try:
        save_identity(atm_id, identity, conflict_rows, received_by=source)
    except Exception as exc:  # noqa: BLE001 - DB trouble must be visible, not fatal
        logger.warning('identity save failed for %s: %s',
                       safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF
        return {'status': 'store_failed', 'error': str(exc), 'atm_id': atm_id}

    try:
        log_action('AGENT_IDENTITY_INGEST',
                   f'{atm_id} source={source} '
                   f'terminal={identity.get("terminal_id") or "-"} '
                   f'ej={((identity.get("ej") or {}).get("hostname")) or "-"} '
                   f'conflicts={len(conflict_rows)}')
    except Exception as exc:  # noqa: BLE001 - the identity is already stored
        logger.warning('identity audit write failed for %s: %s',
                       safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF

    # Seed only an EMPTY EJ hostname, and only from the strongest source: the
    # <HOST> tag in the journal filename. This is what lets the existing
    # hourly puller pick up a new ATM without a second portal visit. An
    # existing value is never overwritten; a mismatch stays a conflict.
    ej = identity.get('ej') or {}
    if (not (atm.get('ej_hostname') or '').strip()
            and ej.get('hostname')
            and ej.get('hostname_source') == 'ej-filename'):
        try:
            if set_ej_hostname_if_empty(atm_id, ej['hostname']):
                log_action('AGENT_EJ_HOSTNAME_SET',
                           f'{atm_id} hostname={ej["hostname"]} '
                           f'source=auto:identity')
                logger.info('seeded EJ hostname for %s from identity', atm_id)
        except Exception as exc:  # noqa: BLE001 - identity is already stored
            logger.warning('EJ hostname seed failed for %s: %s',
                           safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF

    return {'status': 'stored', 'atm_id': atm_id, 'identity': identity,
            'conflicts': conflict_rows}


def _fresh_enough(clock):
    """`item.get` only exposes lastvalue within the history window. Reject a
    value older than 24h rather than presenting stale identity as current."""
    if not clock:
        return True
    try:
        return (time.time() - int(clock)) <= MAX_VALUE_AGE_SECONDS
    except (TypeError, ValueError):
        return True


def sync_all(limit=None):
    """Ingest every active ATM's current identity item.

    Returns counts only; no identity payloads in logs. Per-ATM failures are
    collected so one bad document cannot stop the fleet sweep.
    """
    from provision import load_atms
    rows = load_atms(None, active_only=True)
    if limit:
        rows = rows[:max(0, int(limit))]
    ids = [r['atm_id'] for r in rows if (r.get('ip_address') or '').strip()]
    out = {'checked': len(ids), 'stored': 0, 'unchanged': 0, 'rejected': 0,
           'no_value': 0, 'unsupported': 0, 'stale': 0, 'errors': []}
    if not ids:
        return out

    client = _client()
    hosts, items = _hosts_and_items(client, ids)
    by_hostid = {h['hostid']: h.get('host') for h in hosts}
    seen = set()
    for item in items:
        atm_id = by_hostid.get(item.get('hostid'))
        if not atm_id:
            continue
        seen.add(atm_id)
        if str(item.get('state')) == '1':
            out['unsupported'] += 1
            continue
        raw = item.get('lastvalue')
        if not raw:
            out['no_value'] += 1
            continue
        if not _fresh_enough(item.get('lastclock')):
            out['stale'] += 1
            continue
        try:
            result = ingest_value(atm_id, raw, clock=item.get('lastclock'),
                                  source='zabbix:auto')
        except Exception as exc:  # noqa: BLE001 - one ATM must not stop the sweep
            out['errors'].append(f'{atm_id}: {exc}')
            continue
        status = result.get('status')
        if status in out:
            out[status] += 1
        elif status in ('store_failed', 'no_atm'):
            out['errors'].append(f'{atm_id}: {result.get("error") or status}')

    out['missing_item'] = len(set(ids) - seen)
    return out


def sync_one(atm_id, client=None):
    """Ingest one ATM's current identity item. Returns a status dict."""
    client = client or _client()
    hosts, items = _hosts_and_items(client, [atm_id])
    if not items:
        return {'status': 'no_item', 'atm_id': atm_id,
                'message': 'atm.identity is not linked to this host yet.'}
    item = items[0]
    if str(item.get('state')) == '1':
        return {'status': 'unsupported', 'atm_id': atm_id,
                'error': item.get('error') or 'atm.identity is unsupported on the agent.'}
    raw = item.get('lastvalue')
    if not raw:
        return {'status': 'no_value', 'atm_id': atm_id,
                'message': 'No atm.identity value has been collected yet.'}
    if not _fresh_enough(item.get('lastclock')):
        return {'status': 'stale', 'atm_id': atm_id,
                'message': 'The last identity value is older than 24 hours.'}
    return ingest_value(atm_id, raw, clock=item.get('lastclock'),
                        source='zabbix:auto')


def request_check_now(atm_id, client=None):
    """Ask Zabbix to execute the passive item now.

    `task.create` type 6 is 'Execute now'. It requires Super-admin; a normal
    API user gets a clear error and the hourly collection remains the fallback.
    """
    client = client or _client()
    _hosts, items = _hosts_and_items(client, [atm_id])
    if not items:
        return False, 'atm.identity is not linked to this host yet.'
    result = client.call('task.create', {
        'type': 6,
        'request': {'itemid': items[0]['itemid']},
    })
    if result is None:
        return False, client.last_error or 'Zabbix refused the check-now request.'
    return True, ''


def refresh_one(atm_id, wait_seconds=6, client=None):
    """Request an immediate check and wait briefly for its value.

    A 6-second wait covers a healthy agent. If the box is slow or offline we
    return `requested` and the scheduler will ingest it on the next pass; the
    operator is never told the refresh failed when it is merely queued.
    """
    client = client or _client()
    ok, error = request_check_now(atm_id, client=client)
    if not ok:
        return {'status': 'request_failed', 'error': error, 'atm_id': atm_id}

    deadline = time.time() + max(0, int(wait_seconds))
    last = None
    while time.time() <= deadline:
        time.sleep(1)
        last = sync_one(atm_id, client=client)
        if last.get('status') in ('stored', 'unchanged'):
            return last
        if last.get('status') in ('unsupported', 'no_item'):
            return last
    return {
        'status': 'requested',
        'atm_id': atm_id,
        'last': last,
        'message': ('Identity check requested; the value will appear after '
                    'the next collection.'),
    }
