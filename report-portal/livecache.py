"""livecache.py — tiny TTL cache for dashboard aggregates.

Why: every dashboard viewer polls every 15–180s. Without this, N viewers
× poll rate = N identical query bursts against Postgres. With this, each
interval costs exactly ONE computation per process no matter how many
viewers are watching (gunicorn runs 3 workers, so 3× per interval worst
case — still a ~10× reduction at 10 viewers, zero new infrastructure).

Tradeoffs, stated plainly:
- Numbers may differ by up to TTL seconds between consecutive loads
  (and between workers, since each holds its own copy — no sticky
  sessions). Dashboards tolerate this; reports and tickets never use it.
- Thundering herd on expiry is accepted: viewer polls aren't synchronized,
  so a miss is usually served by a single recompute; concurrent misses
  last-write-win harmlessly (pure function of DB state).
- Redis would make it cross-process-exact. Revisit if viewers × fleet
  ever make 3×/interval expensive — not before.
"""
import time
import threading

_store = {}
_lock = threading.Lock()


def get_or_compute(key, ttl_seconds, fn):
    now = time.time()
    with _lock:
        hit = _store.get(key)
        if hit is not None and now - hit[0] < ttl_seconds:
            return hit[1]
    val = fn()
    with _lock:
        _store[key] = (time.time(), val)
    return val


def invalidate(prefix=None):
    """Drop entries (used in tests; production never needs it)."""
    with _lock:
        if prefix is None:
            _store.clear()
        else:
            for k in [k for k in _store if str(k).startswith(prefix)]:
                del _store[k]
