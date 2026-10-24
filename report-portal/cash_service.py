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
import logging
from datetime import datetime, timedelta

from cash_forecast import DEFAULT_WINDOW_DAYS, forecast
from zabbix_modules.xfs_metrics import is_xfs

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


def _cassette_notes(atm_id):
    """Notes per cassette, from the same Zabbix items the vault graphic uses.

    Returns (levels, reason). An ATM with levels that were never collected gets
    an empty list plus a reason - the forecast then says "unknown" instead of
    reporting a zero balance, which would read as "already empty".
    """
    try:
        from blueprints.admin import fetch_zabbix_metrics
        metrics = fetch_zabbix_metrics(atm_id) or {}
        vendor = _atm_vendor(atm_id)
        count = 4 if vendor == 'NCR' or is_xfs(metrics) else 3
        out, seen = [], False
        for n in range(1, count + 1):
            raw = metrics.get(f'atm.cash.cassette{n}')
            if raw in (None, ''):
                continue
            seen = True
            try:
                out.append({'cassette': n, 'notes': int(float(raw))})
            except (TypeError, ValueError):
                continue
        if not seen:
            return [], 'No cassette levels are being collected for this ATM.'
        if not out:
            return [], 'Cassette levels came back unreadable.'
        return out, ''
    except Exception as e:
        logger.warning('cash forecast: cassette levels failed for %s: %s', atm_id, e)
        return [], f'Could not read cassette levels: {e}'


def _withdrawals(atm_id, window_days):
    try:
        with _db()() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT recorded_at, amount, status FROM atm_transactions_visible "
                "WHERE atm_id = %s AND recorded_at >= NOW() - (%s || ' days')::interval "
                "ORDER BY recorded_at",
                (atm_id, str(int(window_days))))
            rows = [{'recorded_at': r[0].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[0], 'strftime') else r[0],
                     'amount': r[1], 'status': r[2]} for r in cur.fetchall()]
            cur.close()
            return rows, ''
    except Exception as e:
        logger.warning('cash forecast: withdrawals failed for %s: %s', atm_id, e)
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
        logger.warning('cash forecast: loads failed for %s: %s', atm_id, e)
        return [], f'Could not read the replenishment log: {e}'


def forecast_atm(atm_id, now=None, window_days=DEFAULT_WINDOW_DAYS):
    """Cash forecast for one ATM, with every shortfall reported."""
    now = now or datetime.now()
    problems = []
    levels, why = _cassette_notes(atm_id)
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
