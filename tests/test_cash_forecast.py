"""Tests for report-portal/cash_forecast.py — BRD 5.3.

The failure this module must never have: telling a cash team an ATM will run
dry on a date invented from three transactions, or from none at all. Every
test below is one of those two mistakes.
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import cash_forecast as cf  # noqa: E402

NOW = datetime(2026, 10, 15, 12, 0)


def tx(days_ago, amount, status='APPROVED'):
    return {'recorded_at': (NOW - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S'),
            'amount': amount, 'status': status}


def steady_history(n=200, amount=20000, span_days=10):
    """n withdrawals spread evenly over the last `span_days` days."""
    return [tx(span_days * (i + 1) / n, amount) for i in range(n)]


# ── the honesty rules ────────────────────────────────────────────────────
def test_no_withdrawals_means_no_date_not_a_fake_runway():
    out = cf.forecast([{'cassette': 1, 'notes': 500000}], [], NOW)
    assert out['status'] == 'not_enough_data'
    assert out['depletes_at'] is None
    assert out['runway_days'] is None
    assert 'no pattern' in out['reason'].lower()


def test_no_cassette_levels_means_unknown_not_zero():
    out = cf.forecast([], steady_history(), NOW)
    assert out['status'] == 'unknown'
    assert out['depletes_at'] is None


def test_failed_and_declined_transactions_are_not_demand():
    """A card decline did not take notes out of the cassette. Counting it would
    invent a cash-out risk that does not exist."""
    good = steady_history(n=100, amount=20000, span_days=5)
    noise = [tx(1, 50000, status='DECLINED'), tx(1, 50000, status='FAILED'),
             tx(0, 99999, status='REVERSED')]
    with_noise = cf.burn_rate(good + noise, NOW)
    clean = cf.burn_rate(good, NOW)
    assert with_noise['per_day'] == clean['per_day']
    assert with_noise['samples'] == clean['samples']


def test_zero_and_negative_amounts_are_ignored():
    assert cf._is_withdrawal(tx(1, 0)) is False
    assert cf._is_withdrawal(tx(1, -500)) is False
    assert cf._is_withdrawal({'amount': 'abc'}) is False


def test_a_tiny_sample_is_flagged_low_confidence():
    out = cf.forecast([{'cassette': 1, 'notes': 500000}], [tx(1, 40000), tx(0, 40000)], NOW)
    assert out['status'] == 'ok'
    assert out['confidence'] == 'low'
    assert any('Low confidence' in a for a in out['actions'])


def test_a_real_sample_is_high_confidence_and_actionable():
    # 400,000/day (see steady_history), so 4,000,000 notes = 10 days: comfortable.
    out = cf.forecast([{'cassette': 1, 'notes': 4000000}], steady_history(), NOW)
    assert out['status'] == 'ok' and out['confidence'] == 'high'
    assert out['runway_days'] > 0 and out['depletes_at']
    assert any('days of cash left' in a for a in out['actions'])


# ── the arithmetic ───────────────────────────────────────────────────────
def test_runway_matches_hand_calculation():
    # 200 withdrawals of 20,000 spread over the last 10 days = 40,000/day
    hist = steady_history(n=200, amount=20000, span_days=10)
    out = cf.forecast([{'cassette': 1, 'notes': 400000}], hist, NOW)
    assert out['per_day'] == round(4000000 / 10, 0) or abs(out['per_day'] - 400000) < 1
    assert abs(out['runway_days'] - 1.0) < 0.1, out['runway_days']


def test_rate_uses_observed_days_not_calendar_days():
    """Three withdrawals in the last two days is a busy machine, not an idle
    one averaged over 14 days."""
    recent = [tx(2, 50000), tx(1, 50000), tx(0, 50000)]
    rate = cf.burn_rate(recent, NOW, window_days=14)
    assert rate['days_observed'] <= 2.1
    assert rate['per_day'] > 20000, 'must not dilute a recent burst over 14 days'


def test_a_load_inside_the_window_extends_the_runway():
    """Cash that was loaded and then spent still counts as supply; without this
    the forecast reports a machine we already emptied."""
    hist = steady_history(n=100, amount=20000, span_days=5)
    without = cf.forecast([{'cassette': 1, 'notes': 200000}], hist, NOW)
    with_load = cf.forecast(
        [{'cassette': 1, 'notes': 200000}], hist, NOW,
        loads=[{'cassette': 1, 'recorded_at': NOW.strftime('%Y-%m-%d %H:%M:%S'),
                'notes_added': 300000}])
    assert with_load['runway_days'] > without['runway_days']
    assert any('loaded in the last' in a for a in with_load['actions'])


def test_an_old_load_outside_the_window_is_ignored():
    hist = steady_history(n=100, amount=20000, span_days=5)
    old = cf.forecast([{'cassette': 1, 'notes': 200000}], hist, NOW,
                      loads=[{'cassette': 1,
                              'recorded_at': (NOW - timedelta(days=40)).strftime('%Y-%m-%d %H:%M:%S'),
                              'notes_added': 999999}])
    assert old['runway_days'] < 10


def test_cash_service_reads_xfs_denominations_from_the_master_item():
    import cash_service
    payload = {'cdm': {'cassettes': {
        '0050A': {'denomination': 50}, '0100A': {'denomination': 100},
        '0100B': {'denomination': 100}, '0200A': {'denomination': 200}}}}
    assert cash_service._xfs_denominations({'atm.xfs.cdm': json.dumps(payload)}) == {
        1: 50, 2: 100, 3: 100, 4: 200}


def test_cash_value_uses_denominations_for_runway():
    hist = steady_history(n=200, amount=20000, span_days=10)  # 400,000 ETB/day
    out = cf.forecast([{'cassette': 1, 'notes': 10000, 'denomination': 100,
                        'value': 1000000}], hist, NOW)
    assert out['total_notes'] == 10000
    assert out['total_value'] == 1000000
    assert out['value_unit'] == 'ETB'
    assert out['runway_days'] == 2.5
    assert out['per_cassette'][0]['value'] == 1000000


def test_cash_value_loads_are_converted_with_the_cassette_denomination():
    hist = steady_history(n=200, amount=20000, span_days=10)  # 400,000 ETB/day
    out = cf.forecast(
        [{'cassette': 1, 'notes': 1000, 'denomination': 100, 'value': 100000}],
        hist, NOW,
        loads=[{'cassette': 1,
                'recorded_at': (NOW - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
                'notes_added': 5000}])
    assert out['total_value'] == 100000
    assert out['runway_days'] == 1.5
    assert any('500,000 ETB loaded' in a for a in out['actions'])


def test_per_cassette_split_proportions_and_sums_to_the_whole():
    out = cf.forecast([{'cassette': 1, 'notes': 300000}, {'cassette': 2, 'notes': 100000}],
                      steady_history(), NOW)
    rows = {r['cassette']: r for r in out['per_cassette']}
    assert rows[1]['runway_days'] > rows[2]['runway_days'], 'more notes, more runway'
    assert abs(sum(r['runway_days'] for r in out['per_cassette'])
               - out['runway_days']) < 1.0


def test_empty_measured_cassettes_fall_back_to_the_ledger_not_zero_value():
    out = cf.forecast(
        [{'cassette': 1, 'notes': 0, 'denomination': 100, 'value': 0}],
        steady_history(), NOW,
        loads=[{'cassette': 1,
                'recorded_at': (NOW - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
                'notes_added': 5000000}])
    assert out['status'] == 'ok'
    assert out['source'].startswith('book estimate')
    assert out['value_unit'] == 'notes'
    assert out['total_value'] is None
    assert out['total_notes'] == 1000000


def test_an_empty_cassette_gets_no_runway_rather_than_zero_days():
    out = cf.forecast([{'cassette': 1, 'notes': 500000}, {'cassette': 2, 'notes': 0}],
                      steady_history(), NOW)
    row = [r for r in out['per_cassette'] if r['cassette'] == 2][0]
    assert row['runway_days'] == 0.0 and row['depletes_at'] is None


def test_imminent_cash_out_says_so_immediately():
    hist = steady_history(n=200, amount=50000, span_days=10)     # 1,000,000/day
    out = cf.forecast([{'cassette': 1, 'notes': 400000}], hist, NOW)
    assert out['runway_days'] < 1
    assert any('within a day' in a for a in out['actions'])
    assert cf.fleet_risk('ATM-1', out) == 'low'


def test_a_healthy_machine_is_not_flagged_low():
    hist = steady_history(n=200, amount=20000, span_days=10)     # 400,000/day
    out = cf.forecast([{'cassette': 1, 'notes': 4000000}], hist, NOW)   # 10 days
    assert cf.fleet_risk('ATM-1', out) == 'ok'
    assert cf.fleet_risk('ATM-1', cf.forecast([{'cassette': 1, 'notes': 900000}],
                                              hist, NOW)) == 'low', '2.25 days is low'


def test_risk_is_unknown_rather_than_ok_when_there_is_no_forecast():
    """An ATM with no history must not be counted as safe in a fleet count."""
    out = cf.forecast([{'cassette': 1, 'notes': 500000}], [], NOW)
    assert cf.fleet_risk('ATM-1', out) == 'unknown'


def test_timestamp_formats_from_the_database_are_understood():
    for ts in ('2026-10-15 09:00:00', '2026-10-15T09:00:00', '2026-10-15',
               datetime(2026, 10, 15, 9, 0)):
        assert cf._parse(ts) is not None
    assert cf._parse('not a date') is None
    assert cf._parse(None) is None


def test_window_setting_is_respected():
    hist = steady_history(n=100, amount=20000, span_days=20)
    week = cf.burn_rate(hist, NOW, window_days=7)
    fortnight = cf.burn_rate(hist, NOW, window_days=14)
    assert week['samples'] < fortnight['samples']


def test_human_readout_is_sane():
    assert cf._human(0.4) == 'under a day'
    assert cf._human(1.4) == 'about a day'
    assert cf._human(9.6) == 'about 10 days'


# ── BRD 5.3 for NCR: physical cash does not exist, the ledger does ───────
def test_book_cash_is_loads_minus_withdrawals():
    loads = [{'cassette': 1, 'notes_added': 500000,
              'recorded_at': '2026-10-10 08:00:00'}]
    hist = steady_history(n=100, amount=10000, span_days=5)   # 200,000/day -> 1,000,000
    book = cf.book_cash(loads, hist, NOW)
    assert book['loaded'] == 500000
    assert book['withdrawn'] == 1000000
    assert book['notes'] == 0                      # clamped for arithmetic
    # ...but the overspend itself is a finding the cash team must see.
    assert book['negative'] is True


def test_book_cash_balanced_is_not_negative():
    # steady_history() = 200 x 20,000 over 10 days = 400,000/day -> 4,000,000 out.
    loads = [{'cassette': 1, 'notes_added': 6000000, 'recorded_at': '2026-10-01 08:00:00'}]
    book = cf.book_cash(loads, steady_history(), NOW)
    assert book['withdrawn'] == 4000000
    assert book['notes'] == 2000000
    assert book['negative'] is False


def test_an_overspent_ledger_is_flagged_in_the_forecast():
    # 400,000/day out, only 100,000 ever loaded -> empty AND the ledger is wrong.
    loads = [{'cassette': 1, 'notes_added': 100000,
              'recorded_at': (NOW - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')}]
    out = cf.forecast([], steady_history(), NOW, loads=loads)
    assert out['status'] == 'ok', 'an empty machine with a bad ledger is a finding'
    assert out['runway_days'] == 0.0
    assert any('loads are missing' in a for a in out['actions'])


def test_book_cash_needs_a_ledger():
    assert cf.book_cash([], steady_history(), NOW)['notes'] is None
    assert cf.book_cash(None, None, NOW)['notes'] is None


def test_forecast_falls_back_to_the_ledger_and_says_so():
    """NCR exposes cassette health, not notes, so the vault is empty. The
    forecast must still answer - and must label itself an estimate."""
    # 400,000/day out. Loaded 3,000,000 in total and 1,000,000 of it inside the
    # window, so the ledger balance is 3,000,000 - 4,000,000 < 0 -> empty.
    hist = steady_history(n=200, amount=20000, span_days=10)
    loads = [{'cassette': 1, 'notes_added': 3000000,
              'recorded_at': '2026-09-20 08:00:00'},
             {'cassette': 2, 'notes_added': 1000000,
              'recorded_at': (NOW - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')}]
    out = cf.forecast([], hist, NOW, loads=loads)               # no cassette levels
    assert out['status'] == 'ok'
    assert out['source'].startswith('book estimate')
    assert out['total_notes'] == 0
    assert out['runway_days'] == 0.0, 'an empty ledger balance means zero runway'
    # This ledger balances exactly to zero, so nothing is missing from it -
    # the separate overspent test covers that case.
    assert any('schedule a load now' in a for a in out['actions'])
    assert not any('loads are missing' in a for a in out['actions'])


def test_the_book_estimate_never_counts_the_same_load_twice():
    """Regression: the book path subtracted lifetime withdrawals and then the
    existing 'add recent loads' correction added the loads again, so the card
    could read '0 notes' and '2.5 days runway' at the same time."""
    hist = steady_history(n=200, amount=20000, span_days=10)      # 400,000/day
    loads = [{'cassette': 1, 'notes_added': 5000000, 'recorded_at': '2026-09-01 08:00:00'}]
    out = cf.forecast([], hist, NOW, loads=loads)
    assert out['total_notes'] == 5000000 - 4000000
    assert abs(out['runway_days'] - (1000000 / 400000)) < 0.01
    assert not any('Includes' in a for a in out['actions']), 'no second load credit'
    assert any('cash ledger' in a for a in out['actions'])
    assert any('estimate' in a.lower() for a in out['actions'])


def test_measured_levels_win_over_the_ledger():
    loads = [{'cassette': 1, 'notes_added': 9999999,
              'recorded_at': NOW.strftime('%Y-%m-%d %H:%M:%S'), }]
    hist = steady_history()
    out = cf.forecast([{'cassette': 1, 'notes': 4000000}], hist, NOW, loads=loads)
    assert out['source'] == 'measured cassette levels'
    assert out['total_notes'] == 4000000


def test_no_levels_and_no_ledger_is_unknown_not_a_date():
    out = cf.forecast([], steady_history(), NOW, loads=None)
    assert out['status'] == 'unknown'
    assert out['depletes_at'] is None
    assert out['source'] == 'none'


def test_per_cassette_rows_are_absent_when_the_numbers_are_from_the_ledger():
    """Splitting an estimate across cassettes would invent a precision the
    ledger does not have."""
    loads = [{'cassette': 1, 'notes_added': 500000,
              'recorded_at': (NOW - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')}]
    out = cf.forecast([], steady_history(), NOW, loads=loads)
    assert out['per_cassette'] == []
