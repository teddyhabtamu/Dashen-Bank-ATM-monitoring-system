"""report-portal/zbx_cache.py — short-TTL cache for Zabbix reads.

Why: the portal calls the Zabbix API on every page render (detail pages, lists,
fleet views). At three ATMs that is nothing. At 1,300, with several staff
watching dashboards, it is the portal generating the load that makes it slow —
the classic self-inflicted stampede.

Telemetry arrives every 10-30s (BRD 7.2 budget), so a cache shorter than the
poll interval returns the same answer the API would have. Default 10s keeps the
portal inside the 5s render target without ever serving stale-by-a-minute
numbers.

Per-process by design: gunicorn runs 3 workers, so each keeps its own small
cache. That is fine for a few dozen hot hosts and costs nothing to get wrong -
unlike the fleet report, which every worker must agree on, so that one lives in
the database.
"""
import os
import threading
import time

DEFAULT_TTL_SECONDS = float(os.environ.get('ZABBIX_METRICS_CACHE_TTL', '10'))
MAX_ENTRIES = 512          # bound the memory; ATMs are far fewer than this

_lock = threading.Lock()
_entries = {}              # key -> (expires_at, value)
_hits = 0
_misses = 0


def _now():
    return time.time()


def get(key, loader, ttl=DEFAULT_TTL_SECONDS, cacheable=None, retry_after=1.0):
    """Return loader(key) from cache when fresh, else compute and store.

    cacheable(value) -> False marks the result a FAILURE: it is then held for
    retry_after seconds instead of the full ttl. Without that distinction a
    single API blip was stored as fact for the whole TTL, and because this cache
    is per worker, consecutive reloads that landed on different workers
    disagreed about whether the same ATM was reachable - the page flapped
    between live data and "ATM not reachable" on a hard refresh with no error
    logged anywhere. A failure is not an answer; it is the absence of one.
    """
    global _hits, _misses
    with _lock:
        hit = _entries.get(key)
        if hit and hit[0] > _now():
            _hits += 1
            return hit[1]
    value = loader(key)          # outside the lock: a slow API call must not
    ok = cacheable(value) if cacheable else True   # block every other reader
    window = max(ttl, 0) if ok else max(retry_after, 0)
    with _lock:
        if len(_entries) >= MAX_ENTRIES:
            _entries.clear()      # cheap bounded reset, no LRU bookkeeping
        _entries[key] = (_now() + window, value)
        _misses += 1
    return value


def invalidate(key=None):
    """Drop one entry (after a save) or everything (on refresh)."""
    with _lock:
        if key is None:
            _entries.clear()
        else:
            _entries.pop(key, None)


def stats():
    with _lock:
        total = _hits + _misses
        return {'entries': len(_entries), 'hits': _hits, 'misses': _misses,
                'hit_rate': round(_hits / total, 2) if total else None,
                'ttl_seconds': DEFAULT_TTL_SECONDS}
