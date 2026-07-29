"""
Shared helpers for the multi-tenant simulation engines.

Replaces the old per-ATM hard-coded ATM_PORTS maps. Every ATM in
`atm_locations` is auto-simulated; its SNMP/metrics port is stored in the
`sim_port` column (assigned automatically). This lets a newly registered ATM
start behaving like the others with zero manual wiring.
"""
import os
import time
import psycopg2

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

# Maximum number of ATMs to simulate (0 = unlimited)
MAX_SIMULATED_ATMS = int(os.environ.get('MAX_SIMULATED_ATMS', '0'))

# Starting port for the global (vendor-agnostic) allocation pool.
PORT_MIN = int(os.environ.get('SIM_PORT_MIN', '1161'))
PORT_MAX = int(os.environ.get('SIM_PORT_MAX', '2500'))

# Lock timeout for deadlock avoidance (seconds)
LOCK_TIMEOUT = 5


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                             user=DB_USER, password=DB_PASS)


DDL_LOCK_KEY = 999888776

def ensure_sim_port_col(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (DDL_LOCK_KEY,))
        got_lock = cur.fetchone()[0]
        if not got_lock:
            return
        try:
            cur.execute("SET lock_timeout TO '3s'")
            cur.execute("ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS sim_port INTEGER")
            conn.commit()
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (DDL_LOCK_KEY,))


def _used_ports(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id, sim_port FROM atm_locations WHERE sim_port IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}


def _free_port(used_ports):
    used_ports = set(used_ports)
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p not in used_ports:
            return p
    return None


def assign_ports(conn):
    """Allocate a sim_port to every active ATM, with deadlock retry."""
    for attempt in range(3):
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout TO %s", (LOCK_TIMEOUT * 1000,))
                cur.execute("UPDATE atm_locations SET sim_port = NULL WHERE status <> 'active'")
                cur.execute("SELECT atm_id FROM atm_locations WHERE sim_port IS NULL AND status = 'active' ORDER BY atm_id")
                pending = cur.fetchall()
                used = set(_used_ports(conn).values())
                for (aid,) in pending:
                    free = _free_port(used)
                    if free is None:
                        cur.execute("""
                            SELECT atm_id, sim_port FROM atm_locations
                            WHERE status <> 'active' AND sim_port IS NOT NULL
                            ORDER BY sim_port LIMIT 1
                        """)
                        reclaim = cur.fetchone()
                        if reclaim:
                            old_aid, old_port = reclaim
                            cur.execute("UPDATE atm_locations SET sim_port = NULL WHERE atm_id = %s", (old_aid,))
                            used.discard(old_port)
                            free = _free_port(used)
                        if free is None:
                            print(f"[PORTS] OUT OF PORTS: range {PORT_MIN}-{PORT_MAX} exhausted")
                            break
                    cur.execute("UPDATE atm_locations SET sim_port = %s WHERE atm_id = %s", (free, aid))
                    used.add(free)
            conn.commit()
            return
        except psycopg2.errors.DeadlockDetected:
            print(f"[PORTS] Deadlock on assign_ports (attempt {attempt+1}/3), retrying...")
            time.sleep(1)
            try:
                conn.rollback()
            except Exception:
                conn = get_db()
        except Exception as e:
            print(f"[PORTS] assign_ports error: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            break


def load_atms(conn):
    """Return only active ATMs that have a sim_port assigned."""
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id, vendor, branch, terminal_id, sim_port "
                    "FROM atm_locations "
                    "WHERE sim_port IS NOT NULL AND status = 'active' "
                    "ORDER BY atm_id")
        cols = ['atm_id', 'vendor', 'branch', 'terminal_id', 'port']
        atms = [dict(zip(cols, r)) for r in cur.fetchall()]
    if MAX_SIMULATED_ATMS > 0:
        atms = atms[:MAX_SIMULATED_ATMS]
    return atms


def refresh(conn):
    """Ensure schema + allocate ports for any new ATMs, then return the list."""
    ensure_sim_port_col(conn)
    assign_ports(conn)
    return load_atms(conn)
