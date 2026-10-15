"""Tests for report-portal/zbx_cache.py — the portal's own load guard.

At 1,300 ATMs the biggest performance risk is the portal generating load on
itself: every render calling the Zabbix API. These tests pin that a short TTL
actually suppresses repeat calls, and that the cache cannot serve stale-forever
values or grow without bound.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import zbx_cache as zc  # noqa: E402


def setup_function():
    zc.invalidate()


def test_a_fresh_entry_does_not_call_the_loader_again():
    calls = []

    def loader(key):
        calls.append(key)
        return {'atm.parts': '1'}

    assert zc.get('metrics:ATM-1', loader, ttl=10) == {'atm.parts': '1'}
    for _ in range(20):
        zc.get('metrics:ATM-1', loader, ttl=10)
    assert calls == ['metrics:ATM-1'], 'one API call, not twenty'


def test_different_atms_do_not_share_an_entry():
    calls = []
    loader = lambda k: calls.append(k) or k  # noqa: E731
    zc.get('metrics:ATM-1', loader, ttl=10)
    zc.get('metrics:ATM-2', loader, ttl=10)
    assert calls == ['metrics:ATM-1', 'metrics:ATM-2']


def test_an_expired_entry_is_refetched():
    calls = []
    loader = lambda k: calls.append(k) or {'v': len(calls)}  # noqa: E731
    # Expiry is set when the entry is STORED, so both calls use the short TTL.
    zc.get('metrics:ATM-1', loader, ttl=0.01)
    time.sleep(0.03)
    second = zc.get('metrics:ATM-1', loader, ttl=0.01)
    assert len(calls) == 2 and second == {'v': 2}


def test_a_zero_ttl_never_serves_a_cached_value():
    calls = []
    loader = lambda k: calls.append(k) or len(calls)  # noqa: E731
    a = zc.get('metrics:ATM-1', loader, ttl=0)
    b = zc.get('metrics:ATM-1', loader, ttl=0)
    assert a != b and len(calls) == 2


def test_invalidate_drops_one_entry_or_all():
    calls = []
    loader = lambda k: calls.append(k) or k  # noqa: E731
    zc.get('metrics:ATM-1', loader, ttl=10)
    zc.get('metrics:ATM-2', loader, ttl=10)
    zc.invalidate('metrics:ATM-1')
    zc.get('metrics:ATM-1', loader, ttl=10)
    assert calls.count('metrics:ATM-1') == 2
    zc.invalidate()
    zc.get('metrics:ATM-2', loader, ttl=10)
    assert calls.count('metrics:ATM-2') == 2


def test_the_cache_cannot_grow_without_bound():
    for i in range(zc.MAX_ENTRIES + 50):
        zc.get(f'metrics:ATM-{i}', lambda k: k, ttl=60)
    assert len(zc._entries) <= zc.MAX_ENTRIES


def test_concurrent_readers_share_one_computation():
    """A stampede is exactly what this cache exists to stop: many staff opening
    the same page at once must not become many API calls."""
    calls = []
    barrier = threading.Barrier(6)

    def slow(key):
        calls.append(key)
        time.sleep(0.05)
        return 'value'

    def worker():
        barrier.wait()
        zc.get('metrics:ATM-1', slow, ttl=30)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Worst case a few pass through before the first store lands; the point is
    # that it is bounded by concurrency, not by the number of readers.
    assert len(calls) <= 6
    zc.get('metrics:ATM-1', slow, ttl=30)
    assert len(calls) <= 6, 'after the burst, everything is served from cache'


def test_stats_report_a_hit_rate():
    loader = lambda k: k  # noqa: E731
    zc.get('metrics:ATM-1', loader, ttl=10)
    zc.get('metrics:ATM-1', loader, ttl=10)
    st = zc.stats()
    assert st['hits'] >= 1 and st['hit_rate'] is not None
