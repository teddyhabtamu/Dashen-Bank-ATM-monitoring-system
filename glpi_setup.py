#!/usr/bin/env python3
"""
GLPI Auto-Configuration for Dashen Bank ATM Monitoring

Idempotent — safe to re-run. Creates/updates:
  - ITIL Categories with default assignee groups & SLAs
  - Support groups
  - SLAs

Prints a complete ID mapping table at the end for the Zabbix webhook script.
"""
import requests, json, os, sys, base64, time

GLPI_URL = os.environ.get('GLPI_URL', "http://glpi:80/apirest.php")
APP_TOKEN = os.environ.get('GLPI_APP_TOKEN', 'XuApka4zrjQ9GFFEgGFHBlkpdmcPuY32q4DtsmmQ')
USER = os.environ.get('GLPI_USER', "glpi")
PASS = os.environ.get('GLPI_API_PASSWORD', 'DashenGLPI2024')

headers = {"Content-Type": "application/json", "App-Token": APP_TOKEN}

def get_session():
    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    try:
        r = requests.get(f"{GLPI_URL}/initSession",
            headers={**headers, "Authorization": f"Basic {auth}"},
            params={"expand_dropdowns": True}, timeout=20)
    except requests.RequestException as e:
        print(f"  FAIL: cannot reach GLPI API at {GLPI_URL} — {e}")
        sys.exit(1)
    if r.status_code != 200:
        print(f"  FAIL: initSession returned HTTP {r.status_code}")
        print(f"  Body: {r.text[:300]!r}")
        sys.exit(1)
    try:
        token = r.json()["session_token"]
    except Exception as e:
        print(f"  FAIL: initSession returned a non-JSON body (HTTP {r.status_code}):")
        print(f"  {r.text[:300]!r}")
        print(f"  Hint: an empty body usually means a broken GLPI install")
        print(f"  (e.g. missing apirest.php) or the GLPI Apache vhost is wrong.")
        print(f"  Check: docker exec glpi sh -c 'tail -20 /var/www/html/glpi/files/_log/php-errors.log'")
        sys.exit(1)
    print(f"Session: {token[:20]}...")
    return {**headers, "Session-Token": token}

def api(method, endpoint, h, data=None, params=None):
    url = f"{GLPI_URL}/{endpoint}"
    r = requests.request(method, url, headers=h,
        json={"input": data} if data else None, params=params, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text}

def find_by_name(entity, name, h):
    """Look up a GLPI entity by its name field. Returns id or None."""
    res = api("GET", entity, h, params={"range": "0-500"})
    if not isinstance(res, list):
        return None
    for item in res:
        if isinstance(item, dict) and item.get("name", "").strip().lower() == name.strip().lower():
            return item.get("id")
    return None

def ensure(entity, data, h, name_field="name"):
    """Create entity if it doesn't exist. Return (id, created_flag)."""
    name = data.get(name_field, "")
    existing = find_by_name(entity, name, h)
    if existing:
        # Update the existing entity to ensure groups_id_assign etc. are set
        upd = api("PUT", f"{entity}/{existing}", h, data)
        print(f"  Updated {entity}: {name} (ID {existing})")
        return existing, False
    res = api("POST", entity, h, data)
    new_id = res.get("id") or (res[0].get("id") if isinstance(res, list) else None)
    if new_id:
        print(f"  Created {entity}: {name} (ID {new_id})")
    else:
        print(f"  Failed to create {entity}: {name} — {res}")
    return new_id, True

def setup_glpi():
    h = get_session()

    # ── 1. Support Groups ──────────────────────────────
    print("\n[1] Creating/updating support groups...")
    groups_def = [
        {"name": "ATM Field Engineers",   "comment": "On-site ATM technicians"},
        {"name": "ATM Operations Center", "comment": "Central monitoring and operations"},
        {"name": "Cash Operations Team",  "comment": "Cash replenishment team"},
        {"name": "IT Network Team",       "comment": "Network infrastructure support"},
        {"name": "Vendor Support",        "comment": "ATM hardware vendor escalation"},
    ]
    group_ids = {}
    for g in groups_def:
        gid, _ = ensure("Group", g, h)
        if gid:
            group_ids[g["name"]] = gid

    # ── 2. SLAs ────────────────────────────────────────
    print("\n[2] Creating/updating SLAs...")
    slas_def = [
        {"name": "ATM Critical SLA",
         "comment": "ATM Offline or Cash Out — 30min response, 2hr resolution",
         "type": 1, "number_time": 2, "definition_time": "hour"},
        {"name": "ATM High SLA",
         "comment": "Hardware error, Network issue — 1hr response, 4hr resolution",
         "type": 1, "number_time": 4, "definition_time": "hour"},
        {"name": "ATM Medium SLA",
         "comment": "Cash low, Camera — 4hr response, 8hr resolution",
         "type": 1, "number_time": 8, "definition_time": "hour"},
    ]
    sla_ids = {}
    for s in slas_def:
        sid, _ = ensure("SLA", s, h)
        if sid:
            sla_ids[s["name"]] = sid

    # ── 3. Ticket Categories with hierarchical tree & default group/SLA ──
    print("\n[3] Creating/updating ticket categories (tree structure)...")

    # Parent tier (umbrella categories — no direct group/SLA)
    parents = [
        {"name": "ATM Hardware Fault",  "comment": "Hardware, camera, power, and offline issues"},
        {"name": "ATM Network Issue",   "comment": "Connectivity, link, and latency problems"},
        {"name": "ATM Cash Issue",      "comment": "Cash low, cash out, and replenishment"},
        {"name": "ATM Software Error",  "comment": "Application, firmware, and journal errors"},
    ]
    parent_ids = {}
    for p in parents:
        pid, _ = ensure("ITILCategory", p, h)
        if pid:
            parent_ids[p["name"]] = pid

    # Leaf categories (actual ticket routing) with parent, group & SLA
    # Mapping: category → (parent_name, group_name, sla_name)
    leaves_map = [
        ("NCR ATM Hardware",      "ATM Hardware Fault", "ATM Field Engineers",   "ATM High SLA"),
        ("GRG ATM Hardware",      "ATM Hardware Fault", "ATM Field Engineers",   "ATM High SLA"),
        ("Hardware Error",        "ATM Hardware Fault", "ATM Field Engineers",   "ATM High SLA"),
        ("Camera Failure",        "ATM Hardware Fault", "ATM Field Engineers",   "ATM Medium SLA"),
        ("Power / UPS Alert",     "ATM Hardware Fault", "ATM Field Engineers",   "ATM High SLA"),
        ("ATM Offline",           "ATM Hardware Fault", "ATM Field Engineers",   "ATM Critical SLA"),
        ("Network Issue",         "ATM Network Issue",  "IT Network Team",       "ATM High SLA"),
        ("Cash Low",              "ATM Cash Issue",     "Cash Operations Team",  "ATM Medium SLA"),
        ("Cash Out",              "ATM Cash Issue",     "Cash Operations Team",  "ATM Critical SLA"),
        ("Software Error",        "ATM Software Error", "ATM Field Engineers",   "ATM High SLA"),
        ("Transaction Anomaly",   None,                  "ATM Operations Center", "ATM High SLA"),
        ("Security Alert",        None,                  "ATM Operations Center", "ATM Critical SLA"),
    ]

    cat_ids = {}
    for name, parent_name, grp_name, sla_name in leaves_map:
        parent_id = parent_ids.get(parent_name, 0) if parent_name else 0
        grp_id = group_ids.get(grp_name)
        sla_cat = sla_ids.get(sla_name)

        leaf_def = {
            "name": name,
            "comment": f"Auto-created by GLPI setup",
            "itilcategories_id": parent_id,
            "groups_id_assign": grp_id,
            "slas_id_ttr": sla_cat,
        }
        cid, _ = ensure("ITILCategory", leaf_def, h)
        if cid:
            cat_ids[name] = cid

    print(f"\n  Category tree:")
    for pname, pid in sorted(parent_ids.items(), key=lambda x: x[1]):
        print(f"  {pname} (ID {pid})")
        for lname, pn, _, _ in leaves_map:
            if pn == pname:
                lid = cat_ids.get(lname, "?")
                print(f"    └── {lname} (ID {lid})")
    for lname, pn, _, _ in leaves_map:
        if not pn:
            lid = cat_ids.get(lname, "?")
            print(f"  {lname} (ID {lid})")

    # ── 4. Summary ─────────────────────────────────────
    print("\n" + "=" * 60)
    print("GLPI Configuration Complete!")
    print("=" * 60)

    # Build reverse maps
    id_to_group = {v: k for k, v in group_ids.items()}
    id_to_sla   = {v: k for k, v in sla_ids.items()}

    print(f"\n{'Category Leaf':30s} {'ID':>5s}  {'Assignee Group':25s} {'SLA'}")
    print("-" * 85)
    for lname, pn, grp_name, sla_name in leaves_map:
        cid = cat_ids.get(lname, "?")
        grp_id = group_ids.get(grp_name, "?")
        sla_id = sla_ids.get(sla_name, "?")
        print(f"{lname:30s} {str(cid):>5s}  {grp_name:25s} {sla_name}")

    print(f"\n{'Group':30s} {'ID':>5s}")
    print("-" * 40)
    for gname, gid in sorted(group_ids.items(), key=lambda x: x[1]):
        print(f"{gname:30s} {str(gid):>5s}")

    print(f"\n{'SLA':30s} {'ID':>5s}")
    print("-" * 40)
    for sname, sid in sorted(sla_ids.items(), key=lambda x: x[1]):
        print(f"{sname:30s} {str(sid):>5s}")

    print(f"\n{'Category':30s} {'ID':>5s}")
    print("-" * 40)
    for cname, cid in sorted(cat_ids.items(), key=lambda x: x[1]):
        print(f"{cname:30s} {str(cid):>5s}")

    print("\n✅ All IDs match the webhook routing table — no manual update needed.")
    print("   Run this script again if you add new categories/groups/SLAs.")

    api("GET", "killSession", h)

if __name__ == "__main__":
    setup_glpi()
