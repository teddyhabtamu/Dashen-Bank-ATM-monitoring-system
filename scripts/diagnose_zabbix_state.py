#!/usr/bin/env python3
"""Show why Zabbix has (or has not) fresh state for one or more ATM hosts.

The state manager reads Zabbix's last stored value, not the ATM directly.
A successful manual ``zabbix_get`` proves the agent answered that request; it
does not prove the Zabbix server is storing fresh values. This script prints
the other half of the picture:

  * host enabled/disabled status and any active maintenance window
  * host interface types and ``available`` verdicts
  * whether the host is behind a proxy
  * each state item's last clock, delay, timeout, enabled/unsupported state
  * the item's next scheduled check (an overdue master is not being polled)
  * the item error text, which is where a timeout or a bad UserParameter shows

Run it inside the portal container (it already has the Zabbix env):

    docker compose cp scripts/diagnose_zabbix_state.py report-portal:/tmp/
    docker compose exec -T report-portal python3 /tmp/diagnose_zabbix_state.py \
        ATM-TEST-GRG ATM-TEST-NCR

No database access is needed.
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
_id = 0

INTERFACE_TYPES = {0: "unknown", 1: "agent", 2: "snmp", 3: "ipmi", 4: "jmx"}
AVAILABILITY = {0: "unknown", 1: "available", 2: "unavailable"}
ITEM_STATE = {0: "normal", 1: "not supported"}
ITEM_STATUS = {0: "enabled", 1: "disabled"}
HOST_STATUS = {0: "enabled", 1: "disabled"}
MAINTENANCE_TYPE = {0: "data-collection", 1: "NO-DATA"}
ITEM_TYPES = {
    0: "agent", 1: "snmp-v1", 2: "trapper", 3: "simple", 4: "snmp-v2",
    5: "internal", 6: "snmp-v3", 7: "agent-active", 8: "aggregate",
    9: "web", 10: "external", 11: "db", 12: "ipmi", 13: "ssh",
    14: "telnet", 15: "calculated", 16: "jmx", 17: "snmp-trap",
    18: "dependent", 19: "http-agent", 20: "snmp", 21: "script",
}
AGENT_ITEM_TYPES = {0, 7}


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


def _age(clock, now):
    try:
        clock = int(clock or 0)
    except (TypeError, ValueError):
        clock = 0
    return max(0, int(now - clock)) if clock else None


def _schedule(item, now):
    """Return a short text describing when the item is next due."""
    if (item.get("delay") or "0") == "0":
        return ""
    try:
        nextcheck = int(item.get("nextcheck") or 0)
    except (TypeError, ValueError):
        nextcheck = 0
    if not nextcheck:
        return ""
    delta = int(nextcheck - now)
    if delta < -60:
        return "OVERDUE=%ds" % -delta
    return "next=%+ds" % delta


def describe_item(item, now, expected_interface=None):
    key = item.get("key_", "")
    age = _age(item.get("lastclock"), now)
    status = int(item.get("status") or 0)
    state = int(item.get("state") or 0)
    flags = []
    if status:
        flags.append(ITEM_STATUS.get(status, "status %s" % status))
    if state:
        flags.append(ITEM_STATE.get(state, "state %s" % state))
    if expected_interface:
        interface_id = str(item.get("interfaceid") or "")
        if interface_id and interface_id != str(expected_interface):
            flags.append("WRONG-IFACE")
    age_text = "never" if age is None else "%ss" % age
    item_type = int(item.get("type") or 0)
    print("  %-44s age=%-8s type=%-12s delay=%-6s timeout=%-4s %-14s %s" % (
        key, age_text, ITEM_TYPES.get(item_type, str(item_type)),
        item.get("delay") or "-", item.get("timeout") or "-",
        _schedule(item, now), " ".join(flags)))
    error = (item.get("error") or "").strip()
    if error:
        print("      error: %s" % error.replace("\n", " ")[:300])
    value = item.get("lastvalue")
    if value not in (None, ""):
        text = str(value).replace("\n", " ")
        print("      value: %s" % text[:180])


def show_queue(auth, now, limit=15):
    """Print the oldest items that are past their next scheduled check.

    If the ATM masters appear here, Zabbix is not ignoring them: its pollers
    are behind, usually because unreachable synthetic/test hosts are consuming
    every poller with slow SNMP or agent timeouts.
    """
    items = call("item.get", {
        "output": ["itemid", "key_", "nextcheck", "delay", "lastclock",
                   "status", "state"],
        "selectHosts": ["host"],
        "filter": {"status": 0},
        "sortfield": "nextcheck",
        "sortorder": "ASC",
        "limit": 500,
    }, auth) or []
    overdue = []
    for item in items:
        try:
            nextcheck = int(item.get("nextcheck") or 0)
        except (TypeError, ValueError):
            continue
        if nextcheck and nextcheck < now - 60:
            overdue.append(item)
    overdue.sort(key=lambda entry: int(entry["nextcheck"]))
    print("\n=== Oldest due items (Zabbix poller queue) ===")
    if not overdue:
        print("  none (all enabled items are within 60s of their due time)")
        return
    for item in overdue[:limit]:
        host = (item.get("hosts") or [{}])[0].get("host", "?")
        age = _age(item.get("lastclock"), now)
        print("  %-28s %-42s delay=%-5s overdue=%ss age=%s" % (
            str(host)[:28], str(item.get("key_", ""))[:42],
            item.get("delay") or "-",
            int(now - int(item["nextcheck"])),
            "never" if age is None else "%ss" % age))


def active_maintenances(maintenances, host, now):
    host_id = str(host.get("hostid"))
    group_ids = {str(g.get("groupid")) for g in host.get("hostgroups") or []}
    active = []
    for maint in maintenances:
        assigned_hosts = {str(h.get("hostid")) for h in maint.get("hosts") or []}
        assigned_groups = {str(g.get("groupid")) for g in maint.get("groups") or []}
        if host_id not in assigned_hosts and not (group_ids & assigned_groups):
            continue
        try:
            since = int(maint.get("active_since") or 0)
            till = int(maint.get("active_till") or 0)
        except (TypeError, ValueError):
            continue
        if since <= now < till:
            active.append(maint)
    return active


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*",
                        default=["ATM-TEST-GRG", "ATM-TEST-NCR"],
                        help="Zabbix technical host names")
    parser.add_argument("--queue", action="store_true",
                        help="also show the oldest overdue items in Zabbix's "
                             "poller queue")
    args = parser.parse_args()

    auth = call("user.login", {"username": USER, "password": PASSWORD})
    print("Zabbix: %s" % URL)
    now = time.time()
    try:
        maintenances = call("maintenance.get", {
            "output": ["maintenanceid", "name", "maintenance_type",
                       "active_since", "active_till"],
            "selectHosts": ["hostid"],
            "selectGroups": ["groupid"],
        }, auth) or []
    except RuntimeError as exc:
        print("maintenance.get failed: %s" % exc)
        maintenances = []

    for host_name in args.hosts:
        hosts = call("host.get", {
            "output": ["hostid", "host", "name", "proxy_hostid", "status"],
            "selectInterfaces": ["interfaceid", "type", "main", "ip", "port",
                                 "available"],
            "selectHostGroups": ["groupid", "name"],
            "filter": {"host": [host_name]},
        }, auth)
        print("\n=== %s ===" % host_name)
        if not hosts:
            print("  host not found in Zabbix")
            continue
        host = hosts[0]
        proxy = host.get("proxy_hostid")
        if not proxy or proxy == "0":
            proxy = "none (server polls directly)"
        host_status = int(host.get("status") or 0)
        print("  hostid=%s name=%s proxy=%s status=%s" % (
            host.get("hostid"), host.get("name"), proxy,
            HOST_STATUS.get(host_status, host_status)))

        agent_interface = None
        snmp_interface = None
        for iface in host.get("interfaces") or []:
            iface_type = int(iface.get("type") or 0)
            if iface_type == 1:
                agent_interface = iface.get("interfaceid")
            elif iface_type == 2:
                snmp_interface = iface.get("interfaceid")
            print("  interface: type=%s %s:%s available=%s main=%s id=%s" % (
                INTERFACE_TYPES.get(iface_type, iface_type),
                iface.get("ip"), iface.get("port"),
                AVAILABILITY.get(int(iface.get("available") or 0),
                                 iface.get("available")),
                iface.get("main"), iface.get("interfaceid")))

        active = active_maintenances(maintenances, host, now)
        if active:
            for maint in active:
                kind = MAINTENANCE_TYPE.get(
                    int(maint.get("maintenance_type") or 0),
                    maint.get("maintenance_type"))
                print("  maintenance: %s type=%s until=%s" % (
                    maint.get("name"), kind, maint.get("active_till")))
        else:
            print("  maintenance: none active")

        items = call("item.get", {
            "output": ["key_", "lastclock", "lastvalue", "status", "state",
                       "error", "delay", "timeout", "type", "nextcheck",
                       "interfaceid"],
            "hostids": [host["hostid"]],
            "search": {"key_": "atm."},
            "sortfield": "key_",
        }, auth) or []
        wanted = []
        for item in items:
            key = item.get("key_", "")
            if key in ("atm.xfs.cdm", "atm.parts", "atm.identity",
                       "atm.status", "atm.net.link", "atm.cash.cassette1"):
                wanted.append(item)
            elif key.startswith(("atm.xfs.", "atm.part.")):
                wanted.append(item)
        if not wanted:
            print("  no ATM state items found on this host")
        for item in wanted:
            expected = None
            item_type = int(item.get("type") or 0)
            if item_type in AGENT_ITEM_TYPES:
                expected = agent_interface
            elif item_type == 20:
                expected = snmp_interface
            describe_item(item, now, expected)

    if args.queue:
        show_queue(auth, now)

    call("user.logout", [], auth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
