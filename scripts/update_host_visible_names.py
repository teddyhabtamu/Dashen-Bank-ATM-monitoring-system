"""Update Zabbix host visible names from CSV atm_name field.

Usage:
    python3 scripts/update_host_visible_names.py --apply

Reads config/postgres/atm_locations.csv and updates the `name`
(visible name) for each Zabbix host whose `host` matches terminal_id.
"""

import csv
import requests

ZBX_URL = "http://localhost:8080/api_jsonrpc.php"
ZBX_USER = "Admin"
ZBX_PASS = "zabbix"
CSV_PATH = "config/postgres/atm_locations.csv"


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    # Load CSV data
    csv_data = {}
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            tid = row.get("terminal_id", "").strip()
            if tid:
                csv_data[tid] = row

    print(f"Loaded {len(csv_data)} ATMs from CSV")

    # Connect to Zabbix
    s = requests.Session()
    r = s.post(ZBX_URL, json={
        'jsonrpc': '2.0', 'method': 'user.login',
        'params': {'username': ZBX_USER, 'password': ZBX_PASS},
        'id': 1
    }, timeout=10)
    auth = r.json().get('result')
    if not auth:
        print("Login failed")
        return

    # Get all hosts with terminal_id-like names
    r = s.post(ZBX_URL, json={
        'jsonrpc': '2.0', 'method': 'host.get',
        'params': {'output': ['hostid', 'host', 'name'],
                   'limit': 2000},
        'auth': auth, 'id': 2
    }, timeout=30)
    hosts = r.json().get('result', [])
    print(f"Total hosts in Zabbix: {len(hosts)}")

    # Match CSV data to Zabbix hosts by terminal_id
    updated = 0
    no_change = 0
    not_found = 0
    for h in hosts:
        host_name = h['host']
        if host_name not in csv_data:
            continue
        row = csv_data[host_name]
        visible_name = (row.get('atm_name') or '').strip()
        if not visible_name:
            visible_name = row.get('branch', '').strip()
        if not visible_name:
            continue

        current_name = h.get('name', '')
        if current_name == visible_name:
            no_change += 1
            continue

        if args.apply:
            r2 = s.post(ZBX_URL, json={
                'jsonrpc': '2.0', 'method': 'host.update',
                'params': {'hostid': h['hostid'], 'name': visible_name},
                'auth': auth, 'id': 3
            }, timeout=10)
            result = r2.json().get('result')
            if result:
                updated += 1
            else:
                print(f"  FAILED {host_name}: {r2.json().get('error', {}).get('data', '')}")
        else:
            updated += 1  # counting in dry-run

    print(f"Would update: {updated} (no-change: {no_change})")
    if not args.apply:
        print("Dry run — use --apply to execute")
    else:
        print(f"Updated: {updated}")


if __name__ == "__main__":
    main()
