"""report-portal/cash_forecast.py — BRD 5.3 cash depletion forecast.

The BRD requires a forecast of cash depletion based on withdrawal patterns, to
prevent cash-out. Everything needed is already stored (cassette levels in
Zabbix, transaction history in the portal, loads in cash_replenishments) - only
the output was missing.

Design rules, all of them deliberate:

  * No data means "not enough data" - never a date. An ATM with no withdrawal
    history must not be given a fake runway, because the whole point is to
    prevent an empty machine.
  * Only successful cash movements count. Declined/failed transactions are not
    demand, and counting them inflates the burn rate into a false alarm.
  * Confidence is reported, not hidden. A 3-transaction sample and a 3,000
    transaction sample produce very different numbers of equal appearance.
  * The rate is per observed day, not per calendar day, so an ATM that only
    takes money at payday is not averaged into looking idle.
  * Replenishment changes the arithmetic, so the caller can pass loads to get a
    post-load runway rather than a stale pre-load one.

Stdlib only, no Flask, no DB: the arithmetic is unit-tested, the caller does
the I/O.
"""
from datetime import datetime, timedelta

MIN_SAMPLES = 20          # below this the number is a rumour
DEFAULT_WINDOW_DAYS = 14

# Statuses that represent money actually leaving the cassette.
WITHDRAWAL_STATUSES = ('approved', 'completed', 'success', 'settled', 'posted')


def _is_withdrawal(txn):
    """Cash withdrawal that actually succeeded."""
    status = str(txn.get('status') or '').strip().lower()
    amount = txn.get('amount')
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    if status and status not in WITHDRAWAL_STATUSES:
        return False
    return True


def burn_rate(withdrawals, now, window_days=DEFAULT_WINDOW_DAYS):
    """Withdrawals per day, with the sample behind it.

    Returns {'per_day', 'total', 'samples', 'days_observed', 'confidence',
             'window_days'}. per_day is None when there is not enough history
    to say anything - callers must handle that rather than divide by zero.
    """
    cut = now - timedelta(days=window_days)
    recent = [t for t in withdrawals
              if _is_withdrawal(t) and _parse(t.get('recorded_at')) >= cut]
    total = sum(float(t['amount']) for t in recent)
    stamps = [_parse(t.get('recorded_at')) for t in recent]
    stamps = [s for s in stamps if s]
    # Observed span: from the first withdrawal in the window to now. Using the
    # calendar window instead would understate the rate on a young record.
    days = (now - min(stamps)).total_seconds() / 86400.0 if stamps else 0.0
    days = max(days, 1.0)
    confidence = ('low' if len(recent) < MIN_SAMPLES
                  else 'medium' if len(recent) < MIN_SAMPLES * 5 else 'high')
    return {'per_day': (total / days) if recent else None,
            'total': total, 'samples': len(recent),
            'days_observed': round(days, 2), 'confidence': confidence,
            'window_days': window_days}


def _parse(value):
    if isinstance(value, datetime):
        return value
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def forecast(cassettes, withdrawals, now, loads=None, window_days=DEFAULT_WINDOW_DAYS):
    """Runway per cassette and for the ATM as a whole.

    cassettes: [{'cassette': 1, 'notes': 350000}]   current notes, from Zabbix
    withdrawals: [{'recorded_at': ..., 'amount': ..., 'status': ...}]
    loads:      [{'cassette': 1, 'recorded_at': ..., 'notes_added': N}]
                optional; a load inside the window means the cash left after it.

    Returns:
      {'status', 'reason', 'per_day', 'confidence', 'samples',
       'total_notes', 'runway_days', 'depletes_at', 'depletes_in',
       'per_cassette': [{'cassette', 'notes', 'runway_days', 'depletes_at'}],
       'actions': [...]}   never empty when the forecast is usable
    """
    cassettes = list(cassettes or [])
    notes_by_cassette = {int(c.get('cassette')): int(c.get('notes') or 0)
                         for c in cassettes}
    total_notes = sum(notes_by_cassette.values())

    rate = burn_rate(withdrawals, now, window_days)

    base = {'per_day': None, 'confidence': rate['confidence'], 'samples': rate['samples'],
            'total_notes': total_notes, 'runway_days': None, 'depletes_at': None,
            'depletes_in': None, 'per_cassette': [], 'actions': []}

    if total_notes <= 0:
        return dict(base, status='unknown', reason='No cassette levels are being reported.',
                    per_day=rate['per_day'])
    if not rate['per_day']:
        return dict(base, status='not_enough_data',
                    reason=f'No successful withdrawals in the last {window_days} days, '
                           'so there is no pattern to forecast from.',
                    confidence='none')

    # A load inside the window means the cash that burned was topped up: add it
    # back, otherwise the runway reflects a machine we already emptied.
    added = sum(int(l.get('notes_added') or 0) for l in (loads or [])
                if _parse(l.get('recorded_at')) and
                _parse(l.get('recorded_at')) >= now - timedelta(days=window_days))

    effective = total_notes + added
    runway = effective / rate['per_day']
    depletes_at = now + timedelta(days=runway)

    per_cassette = []
    for cassette, notes in sorted(notes_by_cassette.items()):
        share = (notes / effective) if effective else 0
        c_runway = runway * share if share else 0.0
        per_cassette.append({'cassette': cassette, 'notes': notes,
                             'runway_days': round(c_runway, 1),
                             'depletes_at': (now + timedelta(days=c_runway)).isoformat()
                             if c_runway else None})

    actions = []
    if runway <= 1:
        actions.append('Empty within a day — schedule a load now.')
    elif runway <= 3:
        actions.append(f'Empty in about {runway:.0f} day(s) — plan the CIT visit today.')
    else:
        actions.append(f'About {runway:.0f} days of cash left at the current rate.')
    if added:
        actions.append(f'Includes {added:,} notes loaded in the last {window_days} days.')
    if rate['confidence'] == 'low':
        actions.append(f'Low confidence: only {rate["samples"]} withdrawals — '
                       'treat this as an indication, not a schedule.')

    return dict(base, status='ok', reason='', per_day=rate['per_day'],
                runway_days=round(runway, 1), depletes_at=depletes_at.isoformat(),
                depletes_in=_human(runway), per_cassette=per_cassette, actions=actions)


def _human(days):
    if days < 1:
        return 'under a day'
    if days < 2:
        return 'about a day'
    return f'about {int(round(days))} days'


def fleet_risk(atm_id, forecast_result, low_days=3):
    """Roll a single ATM's forecast up into one word for dashboards."""
    if forecast_result.get('status') != 'ok':
        return 'unknown'
    return 'low' if (forecast_result.get('runway_days') or 99) <= low_days else 'ok'
