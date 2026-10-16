"""Which transaction producers the portal is allowed to show.

The database enforces the rule (see scripts/migrate_txn_sources.sql and the
atm_transactions_visible view); this module is what the UI and the CLI read so
that a person can always answer "is this number real?".

The distinction this module exists to protect: EJ and the ATM switch are real
bank data. The simulator is not, and the only thing that used to separate them
was a banner - which stops being a control the moment anyone exports a CSV.
"""
import datetime
import logging
import time

logger = logging.getLogger(__name__)

# Sources that produce real bank data. Mirrors txn_source_policy.trusted, and is
# duplicated here as the fallback for a host where the migration has not been
# applied yet: an un-migrated deployment must hide the simulator by default
# rather than failing open.
TRUSTED_SOURCES = ('EJ', 'ISO8583')

DEMO_SOURCES = ('SIMULATOR', 'ISO8583_SIM', 'ISO8583_REAL')

# Fixed +3, so the banner can print an expiry in the wall clock the demo audience reads.
EAT = datetime.timezone(datetime.timedelta(hours=3), 'EAT')


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


def _demo_live(row_expires, now):
    """Mirror of the view's demo predicate. Both must agree or the banner lies."""
    return row_expires is not None and row_expires > now


def load(conn):
    """Return (visible, hidden, using_fallback) as lists of (source, label)."""
    rows = _rows(conn, "SELECT source, label, trusted, enabled, demo_expires_at "
                       "FROM txn_source_policy ORDER BY source")
    if rows is None:
        visible = [(s, 'EJ journal' if s == 'EJ' else 'ATM switch') for s in TRUSTED_SOURCES]
        hidden = [(s, 'Simulated / test data') for s in DEMO_SOURCES]
        return visible, hidden, True
    now = datetime.datetime.now(datetime.timezone.utc)
    visible = [(r[0], r[1]) for r in rows
               if r[3] and (r[2] or _demo_live(r[4], now))]
    hidden = [(r[0], r[1]) for r in rows if (r[0], r[1]) not in visible]
    return visible, hidden, False


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
        until = _rows(conn, "SELECT min(demo_expires_at) FROM txn_source_policy "
                            "WHERE NOT trusted AND enabled AND demo_expires_at > NOW()")
        expires = ''
        if until and until[0][0]:
            e = until[0][0].astimezone(EAT)
            expires = ' until %s EAT' % e.strftime('%H:%M')
        return ('DEMONSTRATION DATA IS SHOWN (%s%s) alongside %s. These are not real '
                'transactions and must not drive a cash or fraud decision.'
                % (', '.join(demo_on), expires, shown), 'demo')
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


# Canonical DDL. Mirrored by scripts/migrate_txn_sources.sql for operators who
# prefer to apply it by hand, and run at startup by ensure_schema_best_effort()
# so a code deploy can never land before its own schema.
#
# That failure is not hypothetical: shipping this change once took the whole
# portal down, because nearly every page queries atm_transactions_visible and the
# view had not been created. A unit test suite cannot catch a missing migration -
# only applying it at boot can.
#
# No statement here may UPDATE policy rows: startup runs on every container
# start, so re-asserting the seed would silently revert an operator's choice and
# make the audit log a lie. ON CONFLICT DO NOTHING seeds only new sources.
POLICY_LOCK = 999888778

# Bumped whenever POLICY_DDL changes meaning. Checked-then-skipped schema work is
# only safe with a version marker: without it, an already-existing view would
# never pick up a corrected definition, which is precisely how the three-hour
# timezone bug and the missing view both survived.
SCHEMA_VERSION = 2

POLICY_DDL = [
    """CREATE TABLE IF NOT EXISTS portal_schema_version (
        version integer NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS txn_source_policy (
        source  varchar(20) PRIMARY KEY,
        label   varchar(60) NOT NULL,
        trusted boolean NOT NULL DEFAULT false,
        enabled boolean NOT NULL DEFAULT false,
        note    text
    )""",
    # Demo data carries an expiry. NULL means "no demo window", so a simulated
    # source cannot become visible without a deliberate end time - the failure
    # mode of a demo that outlives itself.
    "ALTER TABLE txn_source_policy ADD COLUMN IF NOT EXISTS demo_expires_at timestamptz",
    """INSERT INTO txn_source_policy (source, label, trusted, enabled, note) VALUES
        ('EJ',           'EJ journal (real ATM)',  true,  true,
         'System of record for what the terminal did.'),
        ('ISO8583',      'ATM switch (ISO 8583)',  true,  true,
         'System of record for what the bank authorised.'),
        ('SIMULATOR',    'Transaction simulator',  false, false,
         'simulators/txn_engine.py invents transactions.'),
        ('ISO8583_SIM',  'Switch simulator',       false, false,
         'scripts/fake_iso8583_switch.py --sim output.'),
        ('ISO8583_REAL', 'Switch test generator',  false, false,
         'Fabricated despite the name.')
       ON CONFLICT (source) DO NOTHING""",
    # Trusted sources show when enabled. Untrusted ones show only while their
    # demo window is open. --no-expiry stores a far-future timestamp rather than
    # NULL, so NULL unambiguously means "not visible as demo data".
    """CREATE OR REPLACE VIEW atm_transactions_visible AS
        SELECT t.* FROM atm_transactions t
         WHERE t.source IN (
               SELECT p.source FROM txn_source_policy p
                WHERE p.enabled
                  AND (p.trusted
                       OR (p.demo_expires_at IS NOT NULL AND p.demo_expires_at > NOW())))""",
    "DELETE FROM portal_schema_version",
    "INSERT INTO portal_schema_version (version) VALUES (2)",
]


def ensure_schema_best_effort():
    """Create the policy table and view if they are missing. Returns True/False.

    Checked before acting, so a normal restart issues no DDL at all: three
    gunicorn workers booting together would otherwise each take an ACCESS
    EXCLUSIVE lock on the view to re-create something identical.

    Uses a transaction-scoped advisory lock, not the session-scoped one, because
    get_db() pools connections: a session lock would go back into the pool still
    held and block the next worker indefinitely. A transaction lock is released
    automatically at commit or rollback.
    """
    try:
        from db import get_db
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (POLICY_LOCK,))
                cur.execute("SELECT to_regclass('public.atm_transactions_visible')")
                have_view = cur.fetchone()[0] is not None
                cur.execute("SELECT to_regclass('public.portal_schema_version')")
                if cur.fetchone()[0]:
                    cur.execute("SELECT max(version) FROM portal_schema_version")
                    if (cur.fetchone()[0] or 0) >= SCHEMA_VERSION and have_view:
                        conn.commit()
                        return True
                for stmt in POLICY_DDL:
                    cur.execute(stmt)
                conn.commit()
                logger.info('Transaction source policy schema created')
                return True
            finally:
                cur.close()
    except Exception as e:
        # Must not stop the app from booting, but must be loud: without this
        # schema every transaction page fails.
        logger.error('Could not create the transaction source policy schema (%s). '
                     'Apply scripts/migrate_txn_sources.sql - until then '
                     'transaction pages will error.', e)
        return False


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
