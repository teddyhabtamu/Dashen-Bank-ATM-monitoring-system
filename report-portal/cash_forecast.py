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
import os
from datetime import datetime, timedelta

MIN_SAMPLES = 20          # below this the number is a rumour
MIN_CONFIDENCE_DAYS = 3   # a burst inside one afternoon is not a daily rate
DEFAULT_WINDOW_DAYS = 14

# Statuses that represent money actually leaving the cassette.
WITHDRAWAL_STATUSES = ('approved', 'completed', 'success', 'settled', 'posted')

# Transaction types that dispense cash from the cassette.  The simulator uses
# the same set; a bank that treats TRANSFER as an account-to-account movement
# can override the SQL filter with CASH_FORECAST_TXN_TYPES=WITHDRAWAL.
# Defined here, not in cash_service, so the SQL filter and the in-memory guard
# cannot drift apart.
CASH_TXN_TYPES = tuple(
    item.strip().upper()
    for item in os.environ.get(
        'CASH_FORECAST_TXN_TYPES',
        'WITHDRAWAL,TRANSFER,CARDLESS_TXN').split(',')
    if item.strip()) or ('WITHDRAWAL',)


def _is_withdrawal(txn):
    """Cash withdrawal that actually succeeded.

    ``txn_type`` is checked when present.  The SQL in cash_service already
    filters to trusted cash types; this guard keeps a caller that forgets that
    filter from counting a deposit or a balance inquiry as cash out.
    """
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
    txn_type = str(txn.get('txn_type') or '').strip().upper()
    if txn_type and txn_type not in CASH_TXN_TYPES:
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
    observed_days = ((now - min(stamps)).total_seconds() / 86400.0
                     if stamps else 0.0)
    # Keep the rate denominator at one day so a burst in the first hour does
    # not divide by a tiny number, but report the real span separately so the
    # UI can say "0.1 days of history" instead of hiding it.
    rate_days = max(observed_days, 1.0)
    if len(recent) < MIN_SAMPLES or observed_days < 1.0:
        confidence = 'low'
    elif (len(recent) < MIN_SAMPLES * 5
          or observed_days < MIN_CONFIDENCE_DAYS):
        confidence = 'medium'
    else:
        confidence = 'high'
    if observed_days < 1:
        span_label = f'{max(1, int(round(observed_days * 24)))} hour(s)'
    else:
        span_label = f'{observed_days:.1f} day(s)'
    return {'per_day': (total / rate_days) if recent else None,
            'total': total, 'samples': len(recent),
            'days_observed': round(observed_days, 2),
            'rate_days': round(rate_days, 2), 'span_label': span_label,
            'confidence': confidence, 'window_days': window_days}


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


def book_cash(loads, withdrawals, now, window_days=DEFAULT_WINDOW_DAYS):
    """Cash the machine SHOULD hold, from the CIT record.

    NCR exposes cassette HEALTH through WMI, not note counts, and the legacy
    SNMP OIDs are unmapped on real hardware - so physical cash is not available
    for NCR at all. The ledger is what we do have:

        book cash = everything ever loaded - everything ever withdrawn

    This is an ESTIMATE and is returned as such. It is wrong when a load was not
    logged, when counting drifted, or when cash was moved between cassettes. It
    is still far better than "no forecast" for deciding whether to schedule a
    CIT visit, provided the caller says so on screen.

    Returns {'notes', 'loaded', 'withdrawn', 'since', 'basis'}; notes is None
    when there is no ledger to work from.
    """
    loaded = sum(int(l.get('notes_added') or 0) for l in (loads or []))
    spent = 0.0
    first = None
    for t in withdrawals or []:
        if not _is_withdrawal(t):
            continue
        when = _parse(t.get('recorded_at'))
        if when:
            first = when if first is None else min(first, when)
        spent += float(t['amount'])
    if loaded <= 0:
        return {'notes': None, 'loaded': 0, 'withdrawn': round(spent, 2),
                'since': None, 'basis': 'no cash has been logged as loaded'}
    notes = int(round(loaded - spent))
    basis = f'{loaded:,} loaded - {spent:,.0f} withdrawn'
    if first:
        basis += f' (since {first.strftime("%Y-%m-%d")})'
    return {'notes': max(notes, 0), 'loaded': loaded, 'withdrawn': round(spent, 2),
            'since': first.strftime('%Y-%m-%d') if first else None, 'basis': basis,
            'negative': notes < 0}


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
    value_by_cassette = {int(c.get('cassette')): c.get('value')
                         for c in cassettes}
    denom_by_cassette = {int(c.get('cassette')): int(c.get('denomination') or 0)
                         for c in cassettes}
    has_value = bool(cassettes) and all(v is not None for v in value_by_cassette.values())
    total_notes = sum(notes_by_cassette.values())
    total_value = (sum(int(v or 0) for v in value_by_cassette.values())
                   if has_value else None)

    rate = burn_rate(withdrawals, now, window_days)

    base = {'per_day': None, 'confidence': rate['confidence'],
            'samples': rate['samples'], 'days_observed': rate['days_observed'],
            'span_label': rate['span_label'],
            'total_notes': total_notes, 'total_value': total_value,
            'value_unit': 'ETB' if has_value else 'notes',
            'runway_days': None, 'depletes_at': None,
            'depletes_in': None, 'per_cassette': [], 'actions': []}

    source = 'measured cassette levels'
    if total_notes <= 0:
        # No telemetry: fall back to the ledger, clearly labelled.
        has_value = False
        total_value = None
        base['total_value'] = None
        base['value_unit'] = 'notes'
        book = book_cash(loads, withdrawals, now, window_days)
        if book['notes'] is None:
            return dict(base, status='unknown', source='none',
                        reason='No cassette levels are being reported and no cash '
                               'has been logged as loaded, so there is nothing to '
                               'forecast from.',
                        per_day=rate['per_day'])
        # A ledger that is short (more withdrawn than loaded) is a FINDING, not
        # missing data: the machine is empty and the ledger is wrong. Reporting
        # "unknown" there would hide both facts behind one vague word.
        total_notes = book['notes']
        source = 'book estimate (loads minus withdrawals)'
        level_detail = (f"Cassette levels are not collected for this ATM, so this "
                        f"uses the cash ledger instead: {book['basis']}.")
        if book.get('negative'):
            # More cash left the machine than the ledger says was ever loaded.
            # That is not a forecast problem, it is a missing-load problem, and
            # a cash team needs to hear it rather than see a tidy zero.
            level_detail += (' More was withdrawn than the ledger records as '
                             'loaded, so loads are missing or the ledger is wrong.')
    else:
        level_detail = '' 
    if not rate['per_day']:
        return dict(base, status='not_enough_data',
                    reason=f'No successful withdrawals in the last {window_days} days, '
                           'so there is no pattern to forecast from.',
                    confidence='none')

    # A load inside the window means the cash that burned was topped up: add it
    # back, otherwise a measured balance reflects a machine we already emptied.
    # This correction applies ONLY to measured levels. The book estimate is
    # already "loads minus withdrawals" - adding the loads again counted them
    # twice and produced "0 notes" next to "2.5 days runway".
    added = 0
    added_value = 0
    if not level_detail:
        for l in (loads or []):
            when = _parse(l.get('recorded_at'))
            if not when or when < now - timedelta(days=window_days):
                continue
            notes_added = int(l.get('notes_added') or 0)
            added += notes_added
            denom = denom_by_cassette.get(int(l.get('cassette') or 0), 0)
            if denom:
                added_value += notes_added * denom

    if has_value:
        effective = (total_value or 0) + added_value
        unit = 'ETB'
    else:
        effective = total_notes + added
        unit = 'notes'
    runway = effective / rate['per_day']
    depletes_at = now + timedelta(days=runway)

    per_cassette = []
    for cassette, notes in sorted(notes_by_cassette.items()):
        # (empty when the numbers came from the ledger, not from cassettes)
        if has_value:
            c_value = int(value_by_cassette.get(cassette) or 0)
            c_runway = c_value / rate['per_day']
        else:
            share = (notes / effective) if effective else 0
            c_runway = runway * share if share else 0.0
        per_cassette.append({'cassette': cassette, 'notes': notes,
                             'value': value_by_cassette.get(cassette),
                             'runway_days': round(c_runway, 1),
                             'runway_hours': round(c_runway * 24, 1) if c_runway else 0.0,
                             'depletes_at': (now + timedelta(days=c_runway)).isoformat()
                             if c_runway else None})

    actions = []
    if runway <= 1:
        actions.append('Empty within a day — schedule a load now.')
    elif runway <= 3:
        actions.append(f'Empty in about {runway:.0f} day(s) — plan the CIT visit today.')
    else:
        actions.append(f'About {runway:.0f} days of cash left at the current rate.')
    if added_value:
        actions.append(f'Includes {added_value:,} ETB loaded in the last {window_days} days.')
    elif added:
        actions.append(f'Includes {added:,} notes loaded in the last {window_days} days.')
    if not level_detail and not total_notes:
        actions.append('The ledger says this ATM is already empty — check it on '
                       'the next CIT visit.')
    if rate['confidence'] == 'low':
        if rate['samples'] < MIN_SAMPLES:
            why = f'only {rate["samples"]} withdrawals'
        elif rate['days_observed'] < 1:
            why = 'less than a day of history'
        else:
            why = f'only {rate["days_observed"]:.1f} days of history'
        actions.append(f'Low confidence: {why} — treat this as an indication, '
                       'not a schedule.')

    if level_detail:
        actions.insert(0, level_detail)
        if source.startswith('book'):
            actions.append('This is an estimate from the cash ledger, not a '
                           'measured level: uncounted loads or drift would move it.')
    return dict(base, status='ok', reason='', source=source, per_day=rate['per_day'],
                runway_days=round(runway, 1), depletes_at=depletes_at.isoformat(),
                depletes_in=_human(runway), per_cassette=per_cassette, actions=actions,
                total_notes=total_notes, total_value=total_value, value_unit=unit)


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
