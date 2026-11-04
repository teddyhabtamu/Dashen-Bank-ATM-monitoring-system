"""report-portal/cash_service.py — I/O for the BRD 5.3 cash forecast.

Keeps cash_forecast.py pure (arithmetic, unit-tested) and puts every query,
probe and Flask-shaped decision here, where it can be read once.

Sources:
  cassette levels  Zabbix latest data (the only live source of notes in a cassette)
  withdrawals      atm_transactions_visible - successful cash movements only
  loads            cash_replenishments - the CIT visits that refilled it

If a source cannot be read the forecast says so. A cash team acting on a
number that was half-derived is worse off than one told "cannot tell".
"""
import json
import logging
import time
from datetime import datetime, timedelta

from cash_forecast import CASH_TXN_TYPES, DEFAULT_WINDOW_DAYS, forecast
from log_safety import safe_log
from txn_sources import TRUSTED_SOURCES
from zabbix_modules.state_freshness import freshness_budget_seconds
from zabbix_modules.xfs_metrics import DISPENSE_UNITS, is_xfs

logger = logging.getLogger(__name__)


def _db():
    from db import get_db
    return get_db


def _atm_vendor(atm_id):
    try:
        with _db()() as conn:
            cur = conn.cursor()
            cur.execute("SELECT vendor FROM atm_locations WHERE atm_id = %s", (atm_id,))
            row = cur.fetchone()
            cur.close()
            return (row[0] if row else None) or 'NCR'
    except Exception:
        return 'NCR'


def _xfs_denominations(metrics):
    """Map cassette number -> denomination from the GRG XFS master JSON."""
    raw = metrics.get('atm.xfs.cdm')
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        units = ((payload.get('cdm') or {}).get('cassettes') or {})
    except (TypeError, ValueError):
        return {}
    out = {}
    for n, unit_id in enumerate(DISPENSE_UNITS, 1):
        unit = units.get(unit_id) or {}
        try:
            denom = int(unit.get('denomination') or 0)
        except (TypeError, ValueError):
            denom = 0
        if denom:
            out[n] = denom
    return out


def _cassette_notes(atm_id):
    """Notes and cash value per cassette, from the vault's Zabbix items.

    Returns (levels, reason, clock). ``clock`` is the epoch of the reading that
    produced the levels. A stale reading is not used: the forecast falls back to
    the cash ledger and says why, rather than presenting old counts as current.
    An ATM with levels that were never collected gets an empty list plus a
    reason - the forecast then says "unknown" instead of reporting a zero
    balance, which would read as "already empty".
    """
    try:
        from blueprints.admin import fetch_zabbix_snapshot
        snapshot = fetch_zabbix_snapshot(atm_id)
        if snapshot is None:
            return ([], 'Zabbix did not return a cassette reading for this ATM '
                    '(the host may be missing or the API unavailable).', 0)
        metrics = snapshot.get('metrics') or {}
        clocks = snapshot.get('clocks') or {}
        delays = snapshot.get('delays') or {}
        vendor = _atm_vendor(atm_id)
        count = 4 if vendor == 'NCR' or is_xfs(metrics) else 3
        denoms = _xfs_denominations(metrics) if is_xfs(metrics) else {}
        out, seen = [], False
        for n in range(1, count + 1):
            raw = metrics.get(f'atm.cash.cassette{n}')
            if raw in (None, ''):
                continue
            seen = True
            try:
                notes = int(float(raw))
            except (TypeError, ValueError):
                continue
            denom = denoms.get(n)
            out.append({'cassette': n, 'notes': notes,
                        'denomination': denom,
                        'value': notes * denom if denom else None})
        if not seen:
            return [], 'No cassette levels are being collected for this ATM.', 0
        if not out:
            return [], 'Cassette levels came back unreadable.', 0

        # The cassette counts are dependent items: their clock is the master's.
        if is_xfs(metrics):
            level_clock = int(clocks.get('atm.xfs.cdm') or 0)
            delay = delays.get('atm.xfs.cdm') or '1m'
        else:
            level_clock = max(
                int(clocks.get(f'atm.cash.cassette{n}') or 0)
                for n in range(1, count + 1))
            delay = delays.get('atm.cash.cassette1') or '1m'
        if level_clock:
            allowed = freshness_budget_seconds(delay)
            age = max(0, int(time.time() - level_clock))
            if age > allowed:
                return [], (
                    f'Cassette levels are {age // 60} min old; the last reading '
                    f'is outside the {allowed}s freshness budget, so no forecast '
                    'is made from it.'), level_clock
        return out, '', level_clock
    except Exception as e:
        logger.warning('cash forecast: cassette levels failed for %s: %s', safe_log(atm_id), safe_log(e))  # NOSONAR: safe_log strips CR/LF
        return [], f'Could not read cassette levels: {e}', 0


def _withdrawals(atm_id, window_days):
    """Successful cash movements from *trusted* sources only.

    The visible view intentionally exposes demo sources while their demo window
    is open.  A cash team must never be told to schedule a load from simulated
    transactions, so the forecast asks for EJ/ISO8583 explicitly and filters to
    cash-dispensing transaction types in SQL.
    """
    try:
        with _db()() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT t.recorded_at, t.amount, t.status, t.txn_type, t.source "
                "FROM atm_transactions_visible t "
                "WHERE t.atm_id = %s "
                "  AND t.source = ANY(%s) "
                "  AND upper(coalesce(t.txn_type, '')) = ANY(%s) "
                "  AND t.recorded_at >= NOW() - (%s || ' days')::interval "
                "ORDER BY t.recorded_at",
                (atm_id, list(TRUSTED_SOURCES), list(CASH_TXN_TYPES),
                 str(int(window_days))))
            rows = [{'recorded_at': r[0].strftime('%Y-%m-%d %H:%M:%S')
                     if hasattr(r[0], 'strftime') else r[0],
                     'amount': r[1], 'status': r[2],
                     'txn_type': r[3], 'source': r[4]}
                    for r in cur.fetchall()]
            cur.close()
            return rows, ''
    except Exception as e:
        logger.warning('cash forecast: withdrawals failed for %s: %s', safe_log(atm_id), safe_log(e))  # NOSONAR: safe_log strips CR/LF
        return [], f'Could not read transaction history: {e}'


def _loads(atm_id, window_days):
    try:
        with _db()() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT cassette, recorded_at, notes_added FROM cash_replenishments "
                "WHERE atm_id = %s AND recorded_at >= NOW() - (%s || ' days')::interval",
                (atm_id, str(int(window_days))))
            rows = [{'cassette': r[0], 'recorded_at': r[1].strftime('%Y-%m-%d %H:%M:%S')
                     if hasattr(r[1], 'strftime') else r[1], 'notes_added': r[2]}
                    for r in cur.fetchall()]
            cur.close()
            return rows, ''
    except Exception as e:
        logger.warning('cash forecast: loads failed for %s: %s', safe_log(atm_id), safe_log(e))  # NOSONAR: safe_log strips CR/LF
        return [], f'Could not read the replenishment log: {e}'


def forecast_atm(atm_id, now=None, window_days=DEFAULT_WINDOW_DAYS):
    """Cash forecast for one ATM, with every shortfall reported."""
    now = now or datetime.now()
    problems = []
    levels, why, level_clock = _cassette_notes(atm_id)
    if why:
        problems.append(why)
    withdrawals, why = _withdrawals(atm_id, window_days)
    if why:
        problems.append(why)
    loads, why = _loads(atm_id, window_days)
    if why:
        problems.append(why)

    out = forecast(levels, withdrawals, now, loads=loads, window_days=window_days)
    out['atm_id'] = atm_id
    out['window_days'] = window_days
    out['checked_at'] = now.strftime('%Y-%m-%d %H:%M')
    if level_clock:
        out['levels_checked_at'] = datetime.fromtimestamp(level_clock).strftime('%Y-%m-%d %H:%M')
        out['levels_age_seconds'] = max(0, int(time.time() - level_clock))
    # The page-wide banner already says demo data is on screen; repeat it on
    # the cash card because this card can tell someone to send a CIT visit.
    try:
        from txn_sources import status_cached
        _note, level = status_cached()
        if level == 'demo':
            out['demo_excluded'] = (
                'Demo transactions are enabled but were excluded from this '
                'forecast. The burn rate uses only EJ/ISO8583 data.')
    except Exception:
        pass
    # Every unreadable source, in one sentence. Two fields saying the same
    # thing in different words is how an operator stops trusting the card.
    if problems:
        out['reason'] = (out.get('reason') or '') + (
            (' ' if out.get('reason') else '') + ' '.join(problems)).strip()
    return out


def fleet_cash_risk(atms, now=None, window_days=DEFAULT_WINDOW_DAYS, low_days=3):
    """Runway risk per ATM - the BRD 2.3 'prevent cash-out' view.

    Only ATMs whose forecast could actually be computed are counted as safe;
    the rest are 'unknown', so a missing feed never inflates the healthy count.
    """
    from cash_forecast import fleet_risk
    rows = []
    for a in atms or []:
        atm_id = a.get('atm_id') if isinstance(a, dict) else a
        res = forecast_atm(atm_id, now=now, window_days=window_days)
        rows.append({'atm_id': atm_id, 'risk': fleet_risk(atm_id, res, low_days),
                     'runway_days': res.get('runway_days'),
                     'status': res.get('status'), 'reason': res.get('reason', ''),
                     'actions': res.get('actions', [])})
    rows.sort(key=lambda r: (r['risk'] != 'low', r['runway_days'] is None,
                             r['runway_days'] if r['runway_days'] is not None else 0,
                             r['atm_id']))
    return {'low': sum(1 for r in rows if r['risk'] == 'low'),
            'ok': sum(1 for r in rows if r['risk'] == 'ok'),
            'unknown': sum(1 for r in rows if r['risk'] == 'unknown'),
            'rows': rows}
