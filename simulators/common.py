"""
Shared helpers for the multi-tenant simulation engines.

Replaces the old per-ATM hard-coded ATM_PORTS maps. Every ATM in
`atm_locations` is auto-simulated; its SNMP/metrics port is stored in the
`sim_port` column (assigned automatically). This lets a newly registered ATM
start behaving like the others with zero manual wiring.
"""
import os
import psycopg2

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

# Starting port for the global (vendor-agnostic) allocation pool. Legacy ATMs
# keep their historical ports (NCR 1161-1165, GRG 1166-1167); every newly
# registered ATM simply takes the next free port so pools never collide.
PORT_BASE = 1161


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                             user=DB_USER, password=DB_PASS)


def ensure_sim_port_col(conn):
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS sim_port INTEGER")
    conn.commit()


def assign_ports(conn):
    """Allocate a sim_port to any ATM that does not yet have one.

    Uses a single global sequence (MAX(sim_port)+1) so NCR and GRG ports never
    overlap, regardless of fleet size.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id FROM atm_locations WHERE sim_port IS NULL ORDER BY atm_id")
        rows = cur.fetchall()
        if not rows:
            return
        cur.execute("SELECT COALESCE(MAX(sim_port), %s - 1) FROM atm_locations", (PORT_BASE,))
        nxt = cur.fetchone()[0] + 1
        for (aid,) in rows:
            cur.execute("UPDATE atm_locations SET sim_port = %s WHERE atm_id = %s", (nxt, aid))
            nxt += 1
    conn.commit()


def load_atms(conn):
    """Return all ATMs that have a sim_port assigned, as dicts."""
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id, vendor, branch, terminal_id, sim_port "
                    "FROM atm_locations WHERE sim_port IS NOT NULL ORDER BY atm_id")
        cols = ['atm_id', 'vendor', 'branch', 'terminal_id', 'port']
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def refresh(conn):
    """Ensure schema + allocate ports for any new ATMs, then return the list."""
    ensure_sim_port_col(conn)
    assign_ports(conn)
    return load_atms(conn)
