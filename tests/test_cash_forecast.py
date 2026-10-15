"""Tests for report-portal/cash_forecast.py — BRD 5.3.

The failure this module must never have: telling a cash team an ATM will run
dry on a date invented from three transactions, or from none at all. Every
test below is one of those two mistakes.
"""
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


def test_per_cassette_split_proportions_and_sums_to_the_whole():
    out = cf.forecast([{'cassette': 1, 'notes': 300000}, {'cassette': 2, 'notes': 100000}],
                      steady_history(), NOW)
    rows = {r['cassette']: r for r in out['per_cassette']}
    assert rows[1]['runway_days'] > rows[2]['runway_days'], 'more notes, more runway'
    assert abs(sum(r['runway_days'] for r in out['per_cassette'])
               - out['runway_days']) < 1.0


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
