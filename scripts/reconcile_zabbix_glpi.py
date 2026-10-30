#!/usr/bin/env python3
"""
Zabbix → GLPI ticket reconciler (SYSTEM_REVIEW C7, short-term fix).

Problem: when GLPI is down, the Zabbix webhook media step fails and no
ticket is ever created — the problem stays visible in Zabbix only. There
is no retry and no queue, so alerts are silently lost.

This script closes that gap: it fetches recent high-severity Zabbix
problems and creates the GLPI tickets the webhook would have created for
any problem that has none. Ticket names carry the Zabbix event id
(`[ZABBIX-<eventid>] ...`), which is what makes the match reliable.

Usage (run daily via cron, or manually after a GLPI outage):
    python3 scripts/reconcile_zabbix_glpi.py [--hours 24] [--dry-run]
    python3 scripts/reconcile_zabbix_glpi.py --hours 72 --limit 50

Env (same names as the rest of the repo):
    ZBX_URL / ZABBIX_URL        default http://localhost:8080/api_jsonrpc.php
    ZBX_USER / ZABBIX_USER      default Admin
    ZBX_PASS / ZABBIX_PASS      default zabbix
    GLPI_URL (full apirest.php URL) — or GLPI_UI_URL (base, /apirest.php appended)
    GLPI_APP_TOKEN, GLPI_USER (default glpi), GLPI_API_PASSWORD

Exit codes: 0 = all problems have tickets, 1 = some creates failed,
2 = configuration / connectivity error (nothing reconciled).

Routing table below mirrors config/zabbix/zbx_export_mediatypes.xml —
if you change routing there, update it here too.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required (pip install requests).", file=sys.stderr)
    sys.exit(2)

# .env fills whatever the shell didn't export (GLPI_* included).
from zbx_env import ensure_loaded as _ensure_env
_ensure_env()

ZBX_URL = (os.environ.get("ZBX_URL") or os.environ.get("ZABBIX_URL")
           or "http://localhost:8080/api_jsonrpc.php")
ZBX_USER = (os.environ.get("ZBX_USER") or os.environ.get("ZABBIX_USER") or "Admin")
ZBX_PASS = (os.environ.get("ZBX_PASS") or os.environ.get("ZABBIX_PASS") or "zabbix")

_glpi_raw = os.environ.get("GLPI_URL", "").strip()
if not _glpi_raw:
    _glpi_raw = os.environ.get("GLPI_UI_URL", "http://localhost:8082").rstrip("/") + "/apirest.php"
GLPI_URL = _glpi_raw.rstrip("/")
GLPI_APP_TOKEN = os.environ.get("GLPI_APP_TOKEN", "")
GLPI_USER = os.environ.get("GLPI_USER", "glpi")
GLPI_API_PASSWORD = os.environ.get("GLPI_API_PASSWORD", "")

# [keyword, category_id, group_id, sla_id] — first match wins. Keep in sync
# with the webhook (config/zabbix/zbx_export_mediatypes.xml).
ROUTES = [
    ('cash empty', 7, 3, 1), ('cash out', 7, 3, 1), ('cash low', 6, 3, 3),
    ('cash jam', 8, 1, 2), ('card reader error', 8, 1, 2),
    ('card shutter failure', 8, 1, 2), ('shutter failure', 8, 1, 2),
    ('journal printer fault', 8, 1, 2), ('journal printer', 8, 1, 2),
    ('receipt printer fault', 8, 1, 2), ('receipt printer', 8, 1, 2),
    ('receipt paper out', 8, 1, 3), ('receipt paper low', 8, 1, 3),
    ('reject bin', 8, 1, 3), ('printer fault', 8, 1, 2),
    ('critical temperature', 8, 1, 2), ('high temperature', 8, 1, 2),
    ('camera 1 fault', 11, 1, 3), ('camera 2 fault', 11, 1, 3),
    ('camera fault', 11, 1, 3), ('camera storage', 11, 1, 3),
    ('network link down', 9, 4, 2), ('network link', 9, 4, 2),
    ('packet loss', 9, 4, 2), ('high network latency', 9, 4, 2),
    ('latency', 9, 4, 2), ('main power failure', 12, 1, 2),
    ('main power', 12, 1, 2), ('power failure', 12, 1, 2),
    ('ups battery critical', 12, 1, 2), ('ups battery low', 12, 1, 3),
    ('ups on battery', 12, 1, 3), ('card capture', 14, 2, 1),
    ('high card capture', 14, 2, 1), ('cabinet door open', 14, 2, 1),
    ('safe door open', 14, 2, 1), ('safe door', 14, 2, 1),
    ('intrusion alert', 14, 2, 1), ('vibration alert', 14, 2, 1),
    ('out of service', 5, 1, 1), ('partial service', 5, 1, 3),
    ('supervisor', 5, 1, 3), ('maintenance mode', 5, 1, 3),
    ('transaction failure', 10, 2, 2), ('transaction anomaly', 10, 2, 2),
    ('software error', 13, 1, 2),
]
DEFAULT_ROUTE = (10, 2, 3)  # Transaction Anomaly / Ops Center / Medium SLA
SEVERITY_NAMES = {'0': 'Not classified', '1': 'Information', '2': 'Warning',
                  '3': 'Average', '4': 'High', '5': 'Disaster'}
PRIOR_MAP = {0: 2, 1: 2, 2: 2, 3: 3, 4: 4, 5: 5}
URG_IMP_MAP = {0: 2, 1: 2, 2: 2, 3: 3, 4: 4, 5: 4}


class Zabbix:
    def __init__(self):
        self.auth = None
        self._id = 0

    def call(self, method, params, auth=True):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method,
                   "params": params, "id": self._id}
        if auth and self.auth:
            payload["auth"] = self.auth
        r = requests.post(ZBX_URL, json=payload,
                          headers={"Content-Type": "application/json-rpc"},
                          timeout=30)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Zabbix {method}: {data['error'].get('data', data['error'])}")
        return data.get("result")

    def login(self):
        self.auth = self.call("user.login",
                              {"username": ZBX_USER, "password": ZBX_PASS},
                              auth=False)

    def logout(self):
        try:
            if self.auth:
                self.call("user.logout", [], auth=True)
        except Exception:
            pass


def glpi_headers(token):
    return {"App-Token": GLPI_APP_TOKEN, "Session-Token": token}


def glpi_login(session):
    r = session.get(f"{GLPI_URL}/initSession",
                    headers={"App-Token": GLPI_APP_TOKEN},
                    auth=(GLPI_USER, GLPI_API_PASSWORD), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"GLPI initSession HTTP {r.status_code}: {r.text[:200]}")
    token = r.json().get("session_token")
    if not token:
        raise RuntimeError(f"GLPI initSession gave no token: {r.text[:200]}")
    return token


def glpi_search_id(session, token, entity, name):
    """Resolve a GLPI object id by exact name (same approach as the webhook)."""
    try:
        r = session.get(f"{GLPI_URL}/{entity}/",
                        params={"searchText": name, "range": "0-5"},
                        headers=glpi_headers(token), timeout=15)
        if r.status_code != 200:
            return 0
        for row in (r.json() or []):
            if isinstance(row, dict) and row.get("name") == name:
                return int(row.get("id", 0))
    except Exception:
        pass
    return 0


def ticket_exists(session, token, event_id):
    """True if any ticket already references this Zabbix event id."""
    tag = f"ZABBIX-{event_id}"
    try:
        r = session.get(f"{GLPI_URL}/search/Ticket/",
                        params={"criteria[0][field]": 1,
                                "criteria[0][searchtype]": "contains",
                                "criteria[0][value]": tag,
                                "range": "0-5"},
                        headers=glpi_headers(token), timeout=15)
        if r.status_code == 200:
            data = r.json() or {}
            if int(data.get("totalcount", 0) or 0) > 0:
                return True
            rows = data.get("data") or []
            return len(rows) > 0 if isinstance(rows, list) else bool(rows)
    except Exception:
        pass
    return False


def route_for(subject, vendor, session, token):
    low = (subject or "").lower()
    cat_id, grp_id, sla_id = DEFAULT_ROUTE
    for keyword, c, g, s in ROUTES:
        if keyword in low:
            cat_id, grp_id, sla_id = c, g, s
            break
    if (vendor or "").upper() in ("NCR", "GRG") and grp_id == 1:
        vcat = glpi_search_id(session, token, "ITILCategory", vendor.upper() + " ATM Hardware")
        vgrp = glpi_search_id(session, token, "Group", vendor.upper() + " Field Engineers")
        if vcat > 0 and vgrp > 0:
            cat_id, grp_id = vcat, vgrp
    return cat_id, grp_id, sla_id


def create_ticket(session, token, problem, vendor):
    event_id = str(problem["eventid"])
    subject = problem.get("name", "ATM alert")
    host = ((problem.get("hosts") or [{}])[0].get("host")
            or (problem.get("hosts") or [{}])[0].get("name") or "?")
    sev = int(problem.get("severity", 0) or 0)
    try:
        clock = datetime.fromtimestamp(int(problem.get("clock", 0)),
                                       tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        clock = "?"
    cat_id, grp_id, sla_id = route_for(subject, vendor, session, token)
    body = {"input": {
        "name": f"[ZABBIX-{event_id}] {subject} (reconciled)",
        "content": (f"Host: {host}\nVendor: {vendor}\nProblem: {subject}\n"
                    f"Severity: {SEVERITY_NAMES.get(str(sev), 'Unknown')}\n"
                    f"Time: {clock}\nEvent ID: {event_id}\n\n"
                    "Created by the Zabbix-GLPI reconciler — the original "
                    "webhook delivery failed (GLPI was unreachable)."),
        "urgency": URG_IMP_MAP.get(sev, 3), "impact": URG_IMP_MAP.get(sev, 3),
        "priority": PRIOR_MAP.get(sev, 3), "type": 1, "status": 1,
        "itilcategories_id": cat_id, "slas_id_ttr": sla_id,
        "_groups_id_assign": grp_id,
    }}
    r = session.post(f"{GLPI_URL}/Ticket/", json=body,
                     headers={**glpi_headers(token),
                              "Content-Type": "application/json"},
                     timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Ticket create HTTP {r.status_code}: {r.text[:200]}")
    new_id = (r.json() or {}).get("id")
    if not new_id:
        raise RuntimeError(f"Ticket create gave no id: {r.text[:200]}")
    return new_id


def main():
    ap = argparse.ArgumentParser(description="Create GLPI tickets missed during outages.")
    ap.add_argument("--hours", type=float, default=24,
                    help="Look back this many hours (default 24)")
    ap.add_argument("--limit", type=int, default=100,
                    help="Max Zabbix problems to check (default 100)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report missing tickets without creating them")
    args = ap.parse_args()

    if not (GLPI_APP_TOKEN and GLPI_API_PASSWORD):
        print("ERROR: GLPI_APP_TOKEN and GLPI_API_PASSWORD must be set.", file=sys.stderr)
        return 2

    zbx = Zabbix()
    try:
        zbx.login()
    except Exception as e:
        print(f"ERROR: Zabbix login failed: {e}", file=sys.stderr)
        return 2
    try:
        time_from = int(time.time() - args.hours * 3600)
        problems = zbx.call("problem.get", {
            "output": ["eventid", "clock", "name", "severity"],
            "selectHosts": ["hostid", "host", "name"],
            "severities": [4, 5],
            "time_from": time_from,
            "sortfield": "eventid", "sortorder": "DESC",
            "limit": args.limit,
        }) or []
    except Exception as e:
        print(f"ERROR: Zabbix problem.get failed: {e}", file=sys.stderr)
        return 2
    finally:
        zbx.logout()

    # Vendor per host (feeds the same vendor routing the webhook uses).
    vendors = {}
    host_ids = {h.get("hostid") for p in problems for h in (p.get("hosts") or [])
                if h.get("hostid")}
    if host_ids:
        z2 = Zabbix()
        try:
            z2.login()
            for h in z2.call("host.get", {"output": ["hostid"],
                                          "selectMacros": ["macro", "value"],
                                          "hostids": sorted(host_ids)}) or []:
                macros = {m.get("macro"): m.get("value")
                          for m in (h.get("macros") or [])}
                vendors[h["hostid"]] = macros.get("{$ATM_VENDOR}", "Unknown") or "Unknown"
        except Exception as e:
            print(f"WARN: host macro lookup failed ({e}) — vendor defaults to Unknown")
        finally:
            z2.logout()

    session = requests.Session()
    try:
        token = glpi_login(session)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    checked = exists = created = failed = 0
    try:
        for p in problems:
            checked += 1
            eid = str(p.get("eventid"))
            host = (p.get("hosts") or [{}])[0]
            vendor = vendors.get(host.get("hostid"), "Unknown")
            try:
                if ticket_exists(session, token, eid):
                    exists += 1
                    print(f"  EXISTS event {eid} ({p.get('name', '')[:60]})")
                    continue
                if args.dry_run:
                    created += 1  # counted as "would create"
                    print(f"  MISSING event {eid} ({p.get('name', '')[:60]}) — dry run")
                    continue
                new_id = create_ticket(session, token, p, vendor)
                created += 1
                print(f"  CREATED ticket {new_id} for event {eid} ({p.get('name', '')[:60]})")
            except Exception as e:
                failed += 1
                print(f"  FAILED event {eid}: {e}", file=sys.stderr)
    finally:
        try:
            session.get(f"{GLPI_URL}/killSession", headers=glpi_headers(token),
                        timeout=10)
        except Exception:
            pass

    mode = "dry-run, nothing created" if args.dry_run else "done"
    print(f"Reconcile {mode}: checked={checked} exists={exists} "
          f"{'would-create' if args.dry_run else 'created'}={created} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
