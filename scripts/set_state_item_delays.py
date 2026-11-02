#!/usr/bin/env python3
"""Set the polling cadence and timeout of the two ATM state masters in Zabbix.

The state pipeline can only be as fast as the item that carries the evidence:

  * GRG XFS: ``atm.xfs.cdm`` on "Dashen Bank ATM Hardware - GRG XFS"
  * NCR WMI: ``atm.parts`` on "Dashen Bank ATM WMI Parts"

Both run a PowerShell probe that opens several devices. The Zabbix item default
of 3s is shorter than a normal run (measured 3.69s on UAT) and silently leaves
the last value frozen. This sets the item timeout as well as the delay.

Changing the template in this repository does not change a running Zabbix.
Run this once after deploying (or after re-importing either template):

    docker compose cp scripts/set_state_item_delays.py report-portal:/tmp/
    docker compose exec -T report-portal python3 /tmp/set_state_item_delays.py

The Zabbix server/proxy global "Timeout for item checks" must also be at least
15s, matching the compose default. The item-level timeout cannot exceed the
global maximum.

It is idempotent and only updates the master item. It does not create, delete
or relink anything.

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


def set_item(template_name, item_key, delay, item_timeout, auth):
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
    payload = {"itemid": item["itemid"], "delay": delay, "timeout": item_timeout}
    try:
        call("item.update", payload, auth)
    except RuntimeError as exc:
        text = str(exc).lower()
        if "timeout" not in text:
            raise
        print("  %-46s item timeout rejected: %s" % (template_name, exc))
        print("      raising the delay only; raise the global Timeout first")
        call("item.update", {"itemid": item["itemid"], "delay": delay}, auth)
    print("  %-46s %s: delay %s -> %s, timeout %s -> %s" % (
        template_name, item_key, item.get("delay"), delay,
        item.get("timeout"), item_timeout))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wmi-delay", default="1m",
                        help="NCR atm.parts delay (default: 1m)")
    parser.add_argument("--xfs-delay", default="1m",
                        help="GRG atm.xfs.cdm delay (default: 1m)")
    parser.add_argument("--timeout", default="15s",
                        help="item timeout for both masters (default: 15s)")
    parser.add_argument("--skip-wmi", action="store_true")
    parser.add_argument("--skip-xfs", action="store_true")
    args = parser.parse_args()

    auth = call("user.login", {"username": _USER, "password": _PASS})
    print("Updating state item cadence via %s" % _URL)
    changed = 0
    if not args.skip_wmi:
        changed += bool(set_item(WMI_TEMPLATE, WMI_KEY, args.wmi_delay,
                                 args.timeout, auth))
    if not args.skip_xfs:
        changed += bool(set_item(XFS_TEMPLATE, XFS_KEY, args.xfs_delay,
                                 args.timeout, auth))
    call("user.logout", [], auth)
    print("Done: %d item(s) updated." % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
