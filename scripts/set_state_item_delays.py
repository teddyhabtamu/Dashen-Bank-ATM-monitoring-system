#!/usr/bin/env python3
"""Set the polling cadence of the two ATM state masters in Zabbix.

The state pipeline can only be as fast as the item that carries the evidence:

  * GRG XFS: ``atm.xfs.cdm`` on "Dashen Bank ATM Hardware - GRG XFS"
  * NCR WMI: ``atm.parts`` on "Dashen Bank ATM WMI Parts"

Both run a PowerShell probe that opens several devices. Zabbix 6.4 does not
allow a per-item timeout for Zabbix agent checks: the item Timeout field is
only editable for HTTP agent and Script items, and ``item.update`` on an agent
item with a different timeout is rejected with ``value must be "3s"``. The
check timeout is therefore controlled by the Zabbix server/proxy global
``Timeout``:

    docker compose exec -T zabbix-server \\
        grep -nE '^[[:space:]]*Timeout[[:space:]]*=' /etc/zabbix/zabbix_server.conf

It must be at least 15s (the compose file sets ``ZBX_TIMEOUT=15``). The ATM
agent's own ``Timeout`` must also be large enough; the portal agent profile
sets ``Timeout=30``.

Changing the template in this repository does not change a running Zabbix.
Run this once after deploying (or after re-importing either template):

    docker compose cp scripts/set_state_item_delays.py report-portal:/tmp/
    docker compose exec -T report-portal python3 /tmp/set_state_item_delays.py

It is idempotent and only updates the master item delay. It does not create,
delete or relink anything.

Environment (the report-portal container already has these):
    ZABBIX_URL or ZBX_URL, ZABBIX_USER or ZBX_USER, ZABBIX_PASS or ZBX_PASS
"""
import argparse
import json
import os
import sys
import urllib.request

WMI_TEMPLATE = "Dashen Bank ATM WMI Parts"
WMI_KEY = "atm.parts"
XFS_TEMPLATE = "Dashen Bank ATM Hardware - GRG XFS"
XFS_KEY = "atm.xfs.cdm"

_URL = (os.environ.get("ZABBIX_URL") or os.environ.get("ZBX_URL")
        or "http://zabbix-web:8080/api_jsonrpc.php")
_USER = os.environ.get("ZABBIX_USER") or os.environ.get("ZBX_USER", "Admin")
_PASS = os.environ.get("ZABBIX_PASS") or os.environ.get("ZBX_PASS", "")
_id = 0


def call(method, params, auth=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": _id}
    if auth:
        payload["auth"] = auth
    request = urllib.request.Request(
        _URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json-rpc"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode())
    if body.get("error"):
        raise RuntimeError("%s: %s" % (method, body["error"]))
    return body.get("result")


def set_delay(template_name, item_key, delay, auth):
    templates = call("template.get", {
        "output": ["templateid", "host"],
        "filter": {"host": [template_name]},
    }, auth)
    if not templates:
        print("  %-46s not found; skipped" % template_name)
        return False
    template_id = templates[0]["templateid"]
    items = call("item.get", {
        "output": ["itemid", "key_", "delay", "timeout"],
        "templateids": [template_id],
        "filter": {"key_": [item_key]},
    }, auth)
    if not items:
        print("  %-46s item %s not found; skipped" % (template_name, item_key))
        return False
    item = items[0]
    call("item.update", {"itemid": item["itemid"], "delay": delay}, auth)
    print("  %-46s %s: delay %s -> %s (timeout %s is server-global)" % (
        template_name, item_key, item.get("delay"), delay,
        item.get("timeout") or "-"))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wmi-delay", default="1m",
                        help="NCR atm.parts delay (default: 1m)")
    parser.add_argument("--xfs-delay", default="1m",
                        help="GRG atm.xfs.cdm delay (default: 1m)")
    parser.add_argument("--skip-wmi", action="store_true")
    parser.add_argument("--skip-xfs", action="store_true")
    args = parser.parse_args()

    auth = call("user.login", {"username": _USER, "password": _PASS})
    print("Updating state item delays via %s" % _URL)
    changed = 0
    if not args.skip_wmi:
        changed += bool(set_delay(WMI_TEMPLATE, WMI_KEY, args.wmi_delay, auth))
    if not args.skip_xfs:
        changed += bool(set_delay(XFS_TEMPLATE, XFS_KEY, args.xfs_delay, auth))
    call("user.logout", [], auth)
    print("Done: %d item(s) updated." % changed)
    print("Agent check timeout is the Zabbix server/proxy global Timeout; "
          "verify it is >= 15s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
