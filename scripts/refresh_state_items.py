#!/usr/bin/env python3
"""Force the Zabbix items that carry ATM state to run now, then verify they did.

An enabled item with no error can still be stale because the Zabbix poller
queue is behind.  A manual ``zabbix_get`` proves the ATM answers; it does not
prove the server has a free poller to ask it.  ``task.create`` type 6 asks
Zabbix to execute the item immediately, and this script then reads the stored
``lastclock`` back to show whether the request actually ran.

``--queue`` reads the Zabbix database directly (Zabbix 6.4 does not expose
``nextcheck`` through the API) and prints:

  * enabled hosts, enabled items, and how many are overdue
  * enabled items with ``nextcheck = 0`` (never scheduled)
  * the hosts with the largest overdue backlog
  * the target items' own schedule and interface binding

Run it inside the portal container, which already has the Zabbix and database
environment:

    docker compose cp scripts/refresh_state_items.py report-portal:/tmp/
    docker compose exec -T report-portal python3 /tmp/refresh_state_items.py \
        ATM-TEST-GRG ATM-TEST-NCR --queue
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

URL = (os.environ.get("ZABBIX_URL") or os.environ.get("ZBX_URL")
       or "http://zabbix-web:8080/api_jsonrpc.php")
USER = os.environ.get("ZABBIX_USER") or os.environ.get("ZBX_USER", "Admin")
PASSWORD = os.environ.get("ZABBIX_PASS") or os.environ.get("ZBX_PASS", "")

# The items that actually decide state when the agent path is present.
# Dependent items inherit these clocks, so only the masters need a check.
# The legacy SNMP keys (atm.status / atm.net.link / atm.cash.*) are not forced:
# when XFS/WMI exists they are not the state source, and forcing an SNMP check
# against an intentionally down interface only consumes a poller.
MASTER_KEYS = (
    "atm.xfs.cdm",       # GRG/NCR XFS master
    "atm.parts",         # NCR WMI master
)

# Keep this in step with zabbix_modules/state_freshness.py.  A 1m item gets
# 210s (two missed polls plus slack), not 120s; using a lower number here would
# report a healthy but busy poller as stale and send the operator chasing a
# state-manager bug that is not there.
MIN_BUDGET_SECONDS = 120
MAX_BUDGET_SECONDS = 900
_id = 0


def call(method, params, auth=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": _id}
    if auth:
        payload["auth"] = auth
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json-rpc"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    if body.get("error"):
        raise RuntimeError("%s: %s" % (method, body["error"]))
    return body.get("result")


def age(clock, now):
    try:
        clock = int(clock or 0)
    except (TypeError, ValueError):
        clock = 0
    return max(0, int(now - clock)) if clock else None


def age_text(clock, now):
    value = age(clock, now)
    return "never" if value is None else "%ss" % value


def item_delay_seconds(value):
    """Zabbix item delay ('30s', '1m', '5m', '1m/1m') -> seconds."""
    match = re.match(r'\s*(\d+)\s*([smhd]?)', str(value or ''))
    if not match:
        return 60
    number = int(match.group(1))
    unit = match.group(2) or 's'
    return max(1, number * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit])


def budget_seconds(delay):
    """How old an item may be before the state manager calls it stale."""
    cadence = item_delay_seconds(delay)
    return min(MAX_BUDGET_SECONDS,
               max(MIN_BUDGET_SECONDS, int(cadence * 2.5) + 60))


def within_budget(item, now):
    value = age(item.get("lastclock"), now)
    return value is not None and value <= budget_seconds(item.get("delay"))


def load_hosts(auth, names):
    """Return ``[(host, [enabled master items])]`` for the requested names."""
    out = []
    for name in names:
        hosts = call("host.get", {
            "output": ["hostid", "host", "status"],
            "filter": {"host": [name]},
        }, auth) or []
        if not hosts:
            print("%s: host not found in Zabbix" % name)
            continue
        host = hosts[0]
        items = call("item.get", {
            "output": ["itemid", "key_", "lastclock", "status", "state",
                       "error", "delay", "type", "interfaceid"],
            "hostids": [host["hostid"]],
            "filter": {"key_": list(MASTER_KEYS)},
        }, auth) or []
        enabled = [i for i in items
                   if int(i.get("status") or 0) == 0
                   and int(i.get("state") or 0) == 0]
        out.append((host, enabled))
    return out


def print_targets(hosts, now):
    print("\n=== State masters before ===")
    for host, items in hosts:
        status = "enabled" if int(host.get("status") or 0) == 0 else "disabled"
        print("%s (hostid=%s, %s)" % (host.get("host"), host.get("hostid"),
                                      status))
        if not items:
            print("  no enabled, supported master item")
            continue
        for item in items:
            budget = budget_seconds(item.get("delay"))
            print("  %-24s age=%-8s budget=%-5ss delay=%-5s iface=%-4s %s" % (
                item.get("key_"), age_text(item.get("lastclock"), now),
                budget, item.get("delay") or "-",
                item.get("interfaceid") or "-",
                (item.get("error") or "").replace("\n", " ")[:120]))


def _runtime_columns(cur):
    """Return SQL expressions for the item runtime fields.

    Zabbix schema versions split these fields differently: some put all of
    them on ``items``, some put all on ``item_rtdata``, and some split them
    (configuration on ``items``, runtime on ``item_rtdata``).  Query the
    catalog and build one expression per field instead of assuming a layout.
    """
    cur.execute("""
        SELECT column_name, table_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name IN ('items', 'item_rtdata')
           AND column_name IN ('state', 'nextcheck', 'lastclock', 'error')
    """)
    found = {}
    for column, table in cur.fetchall():
        # item_rtdata is the runtime table; prefer it when both exist.
        if column not in found or table == "item_rtdata":
            found[column] = table

    missing = [c for c in ("state", "nextcheck", "lastclock")
               if c not in found]
    if missing:
        raise RuntimeError(
            "missing %s on items/item_rtdata (found: %s)" % (
                ", ".join(missing), ", ".join(sorted(found)) or "none"))

    join_sql = ("LEFT JOIN item_rtdata r ON r.itemid = i.itemid"
                if "item_rtdata" in found.values() else "")

    def expression(column):
        table = found.get(column)
        if table == "item_rtdata":
            return "r.%s" % column
        if table == "items":
            return "i.%s" % column
        return "NULL"

    return (join_sql, expression("state"), expression("nextcheck"),
            expression("lastclock"), expression("error"))


def show_queue(item_ids, db_name):
    """Print the Zabbix poller backlog straight from the database."""
    try:
        import psycopg2
    except ImportError:
        print("\n=== Queue ===\n  psycopg2 is not available; skipping")
        return

    try:
        conn = psycopg2.connect(
            host=os.environ.get("DB_HOST", "postgres"),
            dbname=db_name,
            user=os.environ.get("DB_USER", "zabbix"),
            password=os.environ.get("DB_PASS", ""),
            connect_timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not hide the rest
        print("\n=== Queue ===\n  database unavailable: %s" % exc)
        return
    try:
        cur = conn.cursor()
        join_sql, state_sql, next_sql, last_sql, error_sql = \
            _runtime_columns(cur)
        cur.execute("SELECT count(*) FROM hosts WHERE status = 0")
        enabled_hosts = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM items i %s
             WHERE i.status = 0 AND %s = 0
        """ % (join_sql, state_sql))
        enabled_items = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM items i %s
             WHERE i.status = 0 AND %s = 0 AND %s > 0
               AND %s < extract(epoch FROM now()) - 120
        """ % (join_sql, state_sql, next_sql, next_sql))
        overdue = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM items i %s
             WHERE i.status = 0 AND %s = 0 AND i.type <> 18
               AND %s = 0
        """ % (join_sql, state_sql, next_sql))
        unscheduled = cur.fetchone()[0]

        print("\n=== Queue ===")
        print("  enabled hosts=%s enabled items=%s overdue=%s nextcheck=0=%s"
              % (enabled_hosts, enabled_items, overdue, unscheduled))

        cur.execute("""
            SELECT h.host, count(*) AS overdue,
                   min(to_timestamp(%s)) AS oldest
              FROM items i %s
              JOIN hosts h ON h.hostid = i.hostid
             WHERE i.status = 0 AND %s = 0 AND %s > 0
               AND %s < extract(epoch FROM now()) - 120
             GROUP BY h.host
             ORDER BY overdue DESC
             LIMIT 15
        """ % (last_sql, join_sql, state_sql, next_sql, next_sql))
        rows = cur.fetchall()
        if rows:
            print("  hosts with the largest overdue backlog:")
            for host, count, oldest in rows:
                print("    %-32s overdue=%-5s oldest=%s" % (
                    str(host)[:32], count, oldest))
        else:
            print("  no enabled item is more than 120s overdue")

        if item_ids:
            cur.execute("""
                SELECT h.host, i.key_, i.status, %s, i.delay,
                       to_timestamp(%s) AS last_seen,
                       to_timestamp(%s) AS next_check,
                       i.interfaceid, left(%s, 160) AS error
                  FROM items i %s
                  JOIN hosts h ON h.hostid = i.hostid
                 WHERE i.itemid = ANY(%%s)
                 ORDER BY h.host, i.key_
            """ % (state_sql, last_sql, next_sql, error_sql, join_sql),
                (list(item_ids),))
            rows = cur.fetchall()
            if rows:
                print("  target schedule:")
                for (host, key, status, state, delay, last_seen, next_check,
                     interface_id, error) in rows:
                    print("    %-20s %-18s status=%s state=%s delay=%s "
                          "last=%s next=%s iface=%s %s" % (
                              host, key, status, state, delay, last_seen,
                              next_check, interface_id,
                              (error or "").replace("\n", " ")[:120]))
        cur.close()
    except Exception as exc:  # noqa: BLE001 - a missing table must not stop the refresh
        print("  queue query failed: %s" % exc)
    finally:
        conn.close()


def request_now(auth, items):
    tasks = [{"type": 6, "request": {"itemid": item["itemid"]}}
             for item in items]
    if not tasks:
        return []
    result = call("task.create", tasks, auth) or {}
    return result.get("taskids") or []


def wait_for_fresh(auth, item_ids, wait, before_clocks):
    if not item_ids:
        return {}, True, False
    deadline = time.time() + max(0, wait)
    latest = {}
    while True:
        items = call("item.get", {
            "output": ["itemid", "key_", "lastclock", "delay"],
            "itemids": list(item_ids),
        }, auth) or []
        latest = {i["itemid"]: i for i in items}
        now = time.time()
        if all(within_budget(i, now) for i in items):
            moved = any(i.get("lastclock") != before_clocks.get(i["itemid"])
                        for i in items)
            return latest, True, moved
        if time.time() >= deadline:
            moved = any(i.get("lastclock") != before_clocks.get(i["itemid"])
                        for i in items)
            return latest, False, moved
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*",
                        default=["ATM-TEST-GRG", "ATM-TEST-NCR"],
                        help="Zabbix technical host names")
    parser.add_argument("--queue", action="store_true",
                        help="also print the poller backlog from the database")
    parser.add_argument("--db-name",
                        default=os.environ.get("ZBX_DB_NAME", "zabbix"),
                        help="Zabbix database name for --queue (default zabbix)")
    parser.add_argument("--wait", type=int, default=30,
                        help="seconds to wait for the forced checks (default 30)")
    args = parser.parse_args()

    auth = call("user.login", {"username": USER, "password": PASSWORD})
    print("Zabbix: %s" % URL)
    now = time.time()
    hosts = load_hosts(auth, args.hosts)
    print_targets(hosts, now)

    items = [item for _host, host_items in hosts for item in host_items]
    item_ids = [item["itemid"] for item in items]
    before_clocks = {item["itemid"]: item.get("lastclock") for item in items}
    if args.queue:
        show_queue(item_ids, args.db_name)

    print("\n=== Execute now ===")
    task_ids = request_now(auth, items)
    if not items:
        print("  nothing to run")
    elif not task_ids:
        print("  Zabbix refused the task (the API user may not be Super-admin)")
    else:
        print("  queued %s task(s); waiting up to %ss"
              % (len(task_ids), args.wait))

    latest, fresh, moved = wait_for_fresh(
        auth, item_ids, args.wait, before_clocks)
    now = time.time()
    print("\n=== State masters after ===")
    for item in items:
        current = latest.get(item["itemid"], {})
        budget = budget_seconds(item.get("delay"))
        print("  %-24s age=%-8s budget=%ss" % (
            item.get("key_"),
            age_text(current.get("lastclock"), now), budget))
    if items:
        print("  %s" % ("WITHIN BUDGET" if fresh else
                        "OVERDUE - the state manager will treat this source "
                        "as stale"))
        print("  forced check moved lastclock: %s" % ("yes" if moved else "no"))
    call("user.logout", [], auth)
    return 0 if (fresh or not items) else 1


if __name__ == "__main__":
    sys.exit(main())
