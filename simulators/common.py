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
# Global allocation pool. The whole fleet shares one contiguous port range so
# NCR and GRG ports can never collide. This MUST match the published port range
# in docker-compose.yml (atm-sim-engine `ports`).
PORT_MIN = int(os.environ.get('SIM_PORT_MIN', '1161'))
PORT_MAX = int(os.environ.get('SIM_PORT_MAX', '2500'))


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                             user=DB_USER, password=DB_PASS)


def ensure_sim_port_col(conn):
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS sim_port INTEGER")
    conn.commit()


def _used_ports(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id, sim_port FROM atm_locations WHERE sim_port IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}


def _free_port(used_ports):
    """First port in [PORT_MIN, PORT_MAX] not in `used_ports` (a set/collection
    of port numbers)."""
    used_ports = set(used_ports)
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p not in used_ports:
            return p
    return None


def repair_duplicate_ports(conn):
    """Guarantee every sim_port is unique; reassign duplicates to free ports.

    assign_ports() never reuses a port, but a manual edit or a CSV/SQL import
    that writes sim_port can introduce collisions. Two ATMs sharing a port
    means only one can ever bind -> the other reads AGENT_DISCONNECTED forever.
    """
    used = _used_ports(conn)
    seen = set()
    duplicates = []
    for aid, port in used.items():
        if port in seen:
            duplicates.append(aid)   # second+ ATM on the same port -> reassign
        else:
            seen.add(port)
    for aid in duplicates:
        free = _free_port(used.values())
        if free is None:
            print(f"[PORTS] No free port in range {PORT_MIN}-{PORT_MAX} for duplicate {aid}")
            continue
        with conn.cursor() as cur:
            cur.execute("UPDATE atm_locations SET sim_port = %s WHERE atm_id = %s", (free, aid))
        used[aid] = free
        print(f"[PORTS] Reassigned duplicate {aid} -> {free}")


def assign_ports(conn):
    """Allocate a sim_port to any ATM lacking one, using the first free port in
    [PORT_MIN, PORT_MAX] so ports stay unique and within the docker-published
    range regardless of fleet size.

    Inactive/retired ATMs keep their port (so reactivation is instant) but do
    not consume the pool permanently: if the range is exhausted when a new ATM
    needs a port, a port is reclaimed from the oldest inactive ATM first. This
    prevents a slow port leak from long-term fleet churn without needing a
    manual rebuild to reactivate a recently retired ATM.
    """
    repair_duplicate_ports(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id FROM atm_locations WHERE sim_port IS NULL ORDER BY atm_id")
        pending = cur.fetchall()
        if not pending:
            return
        used = set(_used_ports(conn).values())
        for (aid,) in pending:
            free = _free_port(used)
            if free is None:
                # Pool exhausted — reclaim a port from an inactive ATM.
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
                    print(f"[PORTS] Reclaimed port {old_port} from inactive {old_aid} for new {aid}")
                if free is None:
                    print(f"[PORTS] OUT OF PORTS: cannot allocate sim_port for {aid} "
                          f"(range {PORT_MIN}-{PORT_MAX} exhausted)")
                    break
            cur.execute("UPDATE atm_locations SET sim_port = %s WHERE atm_id = %s", (free, aid))
            used.add(free)
    conn.commit()


def load_atms(conn):
    """Return only active ATMs that have a sim_port assigned.

    Retired/inactive ATMs must not generate traffic (no metrics, no
    transactions), so they are excluded here — this filters both the
    hardware simulator and the transaction feed.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id, vendor, branch, terminal_id, sim_port "
                    "FROM atm_locations "
                    "WHERE sim_port IS NOT NULL AND status = 'active' "
                    "ORDER BY atm_id")
        cols = ['atm_id', 'vendor', 'branch', 'terminal_id', 'port']
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def refresh(conn):
    """Ensure schema + allocate ports for any new ATMs, then return the list."""
    ensure_sim_port_col(conn)
    assign_ports(conn)
    return load_atms(conn)
