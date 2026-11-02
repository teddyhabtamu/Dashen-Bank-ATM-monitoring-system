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

FRESH_SECONDS = 120
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
            print("  %-24s age=%-8s delay=%-5s iface=%-4s %s" % (
                item.get("key_"), age_text(item.get("lastclock"), now),
                item.get("delay") or "-", item.get("interfaceid") or "-",
                (item.get("error") or "").replace("\n", " ")[:120]))


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
        cur.execute("SELECT count(*) FROM hosts WHERE status = 0")
        enabled_hosts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM items WHERE status = 0 AND state = 0")
        enabled_items = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM items
             WHERE status = 0 AND state = 0 AND nextcheck > 0
               AND nextcheck < extract(epoch FROM now()) - 120
        """)
        overdue = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM items
             WHERE status = 0 AND state = 0 AND type <> 18
               AND nextcheck = 0
        """)
        unscheduled = cur.fetchone()[0]

        print("\n=== Queue ===")
        print("  enabled hosts=%s enabled items=%s overdue=%s nextcheck=0=%s"
              % (enabled_hosts, enabled_items, overdue, unscheduled))

        cur.execute("""
            SELECT h.host, count(*) AS overdue,
                   min(to_timestamp(i.lastclock)) AS oldest
              FROM items i
              JOIN hosts h ON h.hostid = i.hostid
             WHERE i.status = 0 AND i.state = 0 AND i.nextcheck > 0
               AND i.nextcheck < extract(epoch FROM now()) - 120
             GROUP BY h.host
             ORDER BY overdue DESC
             LIMIT 15
        """)
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
                SELECT h.host, i.key_, i.status, i.state, i.delay,
                       to_timestamp(i.lastclock) AS last_seen,
                       to_timestamp(i.nextcheck) AS next_check,
                       i.interfaceid, left(i.error, 160) AS error
                  FROM items i
                  JOIN hosts h ON h.hostid = i.hostid
                 WHERE i.itemid = ANY(%s)
                 ORDER BY h.host, i.key_
            """, (list(item_ids),))
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


def wait_for_fresh(auth, item_ids, wait):
    if not item_ids:
        return {}, True
    deadline = time.time() + max(0, wait)
    latest = {}
    while True:
        items = call("item.get", {
            "output": ["itemid", "key_", "lastclock"],
            "itemids": list(item_ids),
        }, auth) or []
        latest = {i["itemid"]: i for i in items}
        now = time.time()
        if all(age(i.get("lastclock"), now) is not None
               and age(i.get("lastclock"), now) <= FRESH_SECONDS
               for i in items):
            return latest, True
        if time.time() >= deadline:
            return latest, False
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

    latest, fresh = wait_for_fresh(auth, item_ids, args.wait)
    now = time.time()
    print("\n=== State masters after ===")
    for item in items:
        current = latest.get(item["itemid"], {})
        print("  %-24s age=%s" % (
            item.get("key_"),
            age_text(current.get("lastclock"), now)))
    if items:
        print("  %s" % ("FRESH" if fresh else
                        "STILL STALE - the check was queued but did not run"))
    call("user.logout", [], auth)
    return 0 if (fresh or not items) else 1


if __name__ == "__main__":
    sys.exit(main())
