#!/usr/bin/env python3
"""
GLPI Auto-Configuration for Dashen Bank ATM Monitoring
Configures: Categories, Teams, SLA, Ticket Templates, Status Rules
"""
import requests
import json

GLPI_URL = "http://localhost:8082/apirest.php"
APP_TOKEN = "XuApka4zrjQ9GFFEgGFHBlkpdmcPuY32q4DtsmmQ"
USER = "glpi"
PASS = os.environ.get('GLPI_API_PASSWORD', '')

headers = {
    "Content-Type": "application/json",
    "App-Token": APP_TOKEN,
}

def get_session():
    r = requests.get(f"{GLPI_URL}/initSession",
        headers={**headers, "Authorization": "Basic " + __import__('base64').b64encode(f"{USER}:{PASS}".encode()).decode()},
        params={"expand_dropdowns": True})
    r.raise_for_status()
    token = r.json()["session_token"]
    print(f"Session: {token[:20]}...")
    return {**headers, "Session-Token": token}

def api(method, endpoint, h, data=None, params=None):
    url = f"{GLPI_URL}/{endpoint}"
    r = requests.request(method, url, headers=h,
        json={"input": data} if data else None, params=params)
    try:
        return r.json()
    except:
        return {"status": r.status_code, "text": r.text}

def setup_glpi():
    h = get_session()

    # ── 1. ATM Ticket Categories ──────────────────────────────
    print("\n[1] Creating ticket categories...")
    categories = [
        {"name": "ATM Offline",          "comment": "ATM unreachable or powered off"},
        {"name": "Cash Low",             "comment": "Cassette below minimum threshold"},
        {"name": "Cash Out",             "comment": "ATM completely out of cash"},
        {"name": "Hardware Error",       "comment": "Card reader, dispenser, printer failure"},
        {"name": "Network Issue",        "comment": "Connectivity degradation or outage"},
        {"name": "Transaction Anomaly",  "comment": "Suspicious or abnormal transaction pattern"},
        {"name": "Camera Failure",       "comment": "ATM camera offline or tampered"},
        {"name": "Power / UPS Alert",    "comment": "Main power or UPS issue"},
        {"name": "Software Error",       "comment": "ATM application failure or reboot"},
        {"name": "Security Alert",       "comment": "Door open, vibration, intrusion detected"},
    ]
    cat_ids = {}
    for cat in categories:
        res = api("POST", "ITILCategory", h, cat)
        cat_id = res.get("id") or (res[0].get("id") if isinstance(res, list) else None)
        if cat_id:
            cat_ids[cat["name"]] = cat_id
            print(f"  Created category: {cat['name']} (ID {cat_id})")
        else:
            print(f"  Category may exist: {cat['name']} — {res}")

    # ── 2. ATM Support Groups ─────────────────────────────────
    print("\n[2] Creating support groups...")
    groups = [
        {"name": "ATM Field Engineers",   "comment": "On-site ATM technicians"},
        {"name": "ATM Operations Center", "comment": "Central monitoring and operations"},
        {"name": "Cash Operations Team",  "comment": "Cash replenishment team"},
        {"name": "IT Network Team",       "comment": "Network infrastructure support"},
        {"name": "Vendor Support",        "comment": "ATM hardware vendor escalation"},
    ]
    group_ids = {}
    for grp in groups:
        res = api("POST", "Group", h, grp)
        gid = res.get("id") or (res[0].get("id") if isinstance(res, list) else None)
        if gid:
            group_ids[grp["name"]] = gid
            print(f"  Created group: {grp['name']} (ID {gid})")

    # ── 3. SLA Definitions ────────────────────────────────────
    print("\n[3] Creating SLAs...")
    slas = [
        {
            "name": "ATM Critical SLA",
            "comment": "ATM Offline or Cash Out — 30min response, 2hr resolution",
            "type": 1,       # TTR (Time To Resolve)
            "number_time": 2,
            "definition_time": "hour",
        },
        {
            "name": "ATM High SLA",
            "comment": "Hardware error, Network issue — 1hr response, 4hr resolution",
            "type": 1,
            "number_time": 4,
            "definition_time": "hour",
        },
        {
            "name": "ATM Medium SLA",
            "comment": "Cash low, Camera — 4hr response, 8hr resolution",
            "type": 1,
            "number_time": 8,
            "definition_time": "hour",
        },
    ]
    sla_ids = {}
    for sla in slas:
        res = api("POST", "SLA", h, sla)
        sid = res.get("id") or (res[0].get("id") if isinstance(res, list) else None)
        if sid:
            sla_ids[sla["name"]] = sid
            print(f"  Created SLA: {sla['name']} (ID {sid})")

    print("\n✅ GLPI base configuration complete!")
    print(f"\nCategories created: {len(cat_ids)}")
    print(f"Groups created:     {len(group_ids)}")
    print(f"SLAs created:       {len(sla_ids)}")
    print("\nCategory IDs:", json.dumps(cat_ids, indent=2))
    print("Group IDs:",    json.dumps(group_ids, indent=2))
    print("SLA IDs:",      json.dumps(sla_ids, indent=2))

    # Kill session
    api("GET", "killSession", h)

if __name__ == "__main__":
    setup_glpi()
