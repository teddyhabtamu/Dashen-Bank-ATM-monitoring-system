#!/usr/bin/env python3
"""Show why Zabbix has (or has not) fresh state for one or more ATM hosts.

The state manager reads Zabbix's last stored value, not the ATM directly.
A successful manual ``zabbix_get`` proves the agent answered that request; it
does not prove the Zabbix server is storing fresh values. This script prints
the other half of the picture:

  * host interface types and ``available`` verdicts
  * whether the host is behind a proxy
  * each state item's last clock, delay, timeout, enabled/unsupported state
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
ITEM_TYPES = {0: "agent", 7: "agent-active", 18: "dependent",
              4: "snmp-v1", 5: "snmp-v2", 6: "snmp-v3"}


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


def describe_item(item, now):
    key = item.get("key_", "")
    try:
        clock = int(item.get("lastclock") or 0)
    except (TypeError, ValueError):
        clock = 0
    age = max(0, int(now - clock)) if clock else None
    status = int(item.get("status") or 0)
    state = int(item.get("state") or 0)
    flags = []
    if status:
        flags.append("DISABLED")
    if state:
        flags.append(ITEM_STATE.get(state, "state %s" % state))
    age_text = "never" if age is None else "%ss" % age
    item_type = int(item.get("type") or 0)
    print("  %-44s age=%-8s type=%-12s delay=%-6s timeout=%-4s %s" % (
        key, age_text, ITEM_TYPES.get(item_type, str(item_type)),
        item.get("delay") or "-", item.get("timeout") or "-",
        " ".join(flags)))
    error = (item.get("error") or "").strip()
    if error:
        print("      error: %s" % error.replace("\n", " ")[:300])
    value = item.get("lastvalue")
    if value not in (None, ""):
        text = str(value).replace("\n", " ")
        print("      value: %s" % text[:180])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*",
                        default=["ATM-TEST-GRG", "ATM-TEST-NCR"],
                        help="Zabbix technical host names")
    args = parser.parse_args()

    auth = call("user.login", {"username": USER, "password": PASSWORD})
    print("Zabbix: %s" % URL)
    now = time.time()
    for host_name in args.hosts:
        hosts = call("host.get", {
            "output": ["hostid", "host", "name", "proxy_hostid"],
            "selectInterfaces": ["type", "main", "ip", "port", "available"],
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
        print("  hostid=%s name=%s proxy=%s" % (
            host.get("hostid"), host.get("name"), proxy))
        for iface in host.get("interfaces") or []:
            print("  interface: type=%s %s:%s available=%s main=%s" % (
                INTERFACE_TYPES.get(int(iface.get("type") or 0),
                                    iface.get("type")),
                iface.get("ip"), iface.get("port"),
                AVAILABILITY.get(int(iface.get("available") or 0),
                                 iface.get("available")),
                iface.get("main")))

        items = call("item.get", {
            "output": ["key_", "lastclock", "lastvalue", "status", "state",
                       "error", "delay", "timeout", "type"],
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
            describe_item(item, now)

    call("user.logout", [], auth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
