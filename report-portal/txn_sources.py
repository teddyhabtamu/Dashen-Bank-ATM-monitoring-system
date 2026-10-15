"""Which transaction producers the portal is allowed to show.

The database enforces the rule (see scripts/migrate_txn_sources.sql and the
atm_transactions_visible view); this module is what the UI and the CLI read so
that a person can always answer "is this number real?".

The distinction this module exists to protect: EJ and the ATM switch are real
bank data. The simulator is not, and the only thing that used to separate them
was a banner - which stops being a control the moment anyone exports a CSV.
"""
import logging
import time

logger = logging.getLogger(__name__)

# Sources that produce real bank data. Mirrors txn_source_policy.trusted, and is
# duplicated here as the fallback for a host where the migration has not been
# applied yet: an un-migrated deployment must hide the simulator by default
# rather than failing open.
TRUSTED_SOURCES = ('EJ', 'ISO8583')

DEMO_SOURCES = ('SIMULATOR', 'ISO8583_SIM', 'ISO8583_REAL')


def _rows(conn, sql, args=None):
    """Run a query, rolling back on failure.

    The rollback is not defensive tidiness: get_db() hands out POOLED
    connections, and psycopg2 leaves a transaction aborted after a failed
    statement. Returning that connection to the pool without rolling back makes
    every later query on it fail with "current transaction is aborted".
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:
        conn.rollback()
        logger.warning('txn source policy unavailable (%s)', e)
        return None
    finally:
        try:
            cur.close()
        except Exception:
            pass


def load(conn):
    """Return (visible, hidden, using_fallback) as lists of (source, label)."""
    rows = _rows(conn, "SELECT source, label, trusted, enabled "
                       "FROM txn_source_policy ORDER BY source")
    if rows is None:
        visible = [(s, 'EJ journal' if s == 'EJ' else 'ATM switch') for s in TRUSTED_SOURCES]
        hidden = [(s, 'Simulated / test data') for s in DEMO_SOURCES]
        return visible, hidden, True
    return ([(r[0], r[1]) for r in rows if r[3]],
            [(r[0], r[1]) for r in rows if not r[3]],
            False)


def status(conn):
    """Return (note, level) for the UI. level is 'demo' or 'info'.

    Replaces db.sim_feed_active(), which detected simulator activity instead of
    describing policy - and which failed in the unsafe direction: a DB hiccup
    returned False, the banner vanished, and invented transactions rendered
    unlabelled. Policy cannot "fail open" like that, because the view decides
    what a query returns, not the banner.
    """
    visible, hidden, _ = load(conn)
    demo_on = [src for src, _ in visible if src not in TRUSTED_SOURCES]
    if demo_on:
        shown = ', '.join(src for src, _ in visible)
        return ('DEMONSTRATION DATA IS SHOWN (%s) alongside %s. These are not real '
                'transactions and must not drive a cash or fraud decision.'
                % (', '.join(demo_on), shown), 'demo')
    if not visible:
        return ('No transaction source is enabled, so every transaction view is '
                'empty by policy.', 'info')
    shown = ', '.join(src for src, _ in visible)
    if not hidden:
        return f'Transaction data: {shown} only.', 'info'
    rows = _rows(conn, "SELECT count(*) FROM atm_transactions t WHERE NOT EXISTS ("
                       "SELECT 1 FROM txn_source_policy p WHERE p.enabled AND p.source = t.source)")
    suffix = f' ({rows[0][0]:,} simulated rows hidden)' if rows else ''
    return f'Transaction data: {shown} only{suffix}.', 'info'


_cache = {'at': 0.0, 'note': '', 'level': 'info'}


def status_cached(ttl=300):
    """Cached status for the per-request context processor.

    A DB failure must not take the portal down, but it must not silently claim
    everything is fine either - so the fallback text says exactly that.
    """
    now = time.time()
    if now - _cache['at'] < ttl:
        return _cache['note'], _cache['level']
    try:
        from db import get_db
        with get_db() as conn:
            note, level = status(conn)
    except Exception as e:
        logger.warning('txn source status unavailable (%s)', e)
        note, level = ('Transaction source status could not be confirmed - treat '
                       'transaction figures with caution.', 'info')
    _cache.update(at=now, note=note, level=level)
    return note, level
