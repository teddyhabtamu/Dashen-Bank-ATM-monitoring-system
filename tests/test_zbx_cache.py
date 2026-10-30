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


def test_a_failed_read_is_retried_instead_of_being_served_as_fact():
    """The ATM detail page flapped between live data and "ATM not reachable" on
    a hard refresh, with no error in the log anywhere.

    One cause was here: the cache stored whatever the loader returned, so a
    single failed Zabbix read was held as an answer for the full TTL. Because
    this cache is per process and gunicorn runs three workers, consecutive
    reloads that landed on different workers disagreed about the same ATM.
    A failure is the absence of an answer, not an answer.
    """
    calls = []

    def loader(key):
        calls.append(key)
        return None                     # the Zabbix read failed

    kw = dict(ttl=10, cacheable=lambda v: v is not None, retry_after=0.01)
    zc.get('metrics:ATM-1', loader, **kw)
    time.sleep(0.03)
    zc.get('metrics:ATM-1', loader, **kw)
    assert len(calls) == 2, 'a failure must be retried, not cached for 10s'


def test_a_successful_read_is_still_cached_for_the_full_ttl():
    """The predicate must not disable caching — the whole reason this module
    exists is to stop the portal stampeding the Zabbix API."""
    calls = []
    loader = lambda k: calls.append(k) or {'atm.parts': '1'}  # noqa: E731

    kw = dict(ttl=10, cacheable=lambda v: v is not None, retry_after=0.01)
    for _ in range(5):
        zc.get('metrics:ATM-1', loader, **kw)
    assert len(calls) == 1, 'a good value is still served from cache'


def test_a_failure_is_still_coalesced_briefly():
    """Not caching failures at all would send every concurrent reader at the
    API during an outage — a stampede exactly when Zabbix is least able to
    take one. The failure is held for retry_after instead."""
    calls = []

    def loader(key):
        calls.append(key)
        return None

    kw = dict(ttl=10, cacheable=lambda v: v is not None, retry_after=10)
    zc.get('metrics:ATM-1', loader, **kw)
    for _ in range(5):
        zc.get('metrics:ATM-1', loader, **kw)
    assert len(calls) == 1, 'concurrent readers must not stampede a failing API'


def test_the_default_still_caches_everything():
    """No predicate means the old behaviour, so this is opt-in per caller."""
    calls = []
    loader = lambda k: calls.append(k) or None  # noqa: E731
    zc.get('metrics:ATM-1', loader, ttl=10)
    zc.get('metrics:ATM-1', loader, ttl=10)
    assert len(calls) == 1
