"""Import templates, create host groups, and register all 1,202 ATMs in Zabbix.

Usage:
    python3 scripts/sync_atms_to_zabbix.py                     # preview
    python3 scripts/sync_atms_to_zabbix.py --apply              # create groups + hosts
    python3 scripts/sync_atms_to_zabbix.py --apply --import-templates  # also import templates

Reads ATM data from config/postgres/atm_locations.csv.
Connects to Zabbix API at localhost:8080.
"""

import csv, json, os, sys, time

ZBX_URL = "http://localhost:8080/api_jsonrpc.php"
ZBX_USER = "Admin"
ZBX_PASS = "zabbix"
CSV_PATH = "config/postgres/atm_locations.csv"
TEMPLATE_NCR_PATH = "config/zabbix/template_ncr_snmp.xml"
TEMPLATE_GRG_PATH = "config/zabbix/template_grg_snmp.xml"

TEMPLATE_NCR_NAME = "Dashen Bank ATM Hardware"
TEMPLATE_GRG_NAME = "Dashen Bank ATM Hardware - GRG"

# Proxy assignment per district (see docs/proxy-topology.md)
# None = no proxy (monitored directly by Zabbix server)
DISTRICT_PROXY = {
    'NAD':     'zabbix-proxy-addis-north',
    'SAD':     'zabbix-proxy-addis-south',
    'EAD':     'zabbix-proxy-addis-east',
    'WAD':     'zabbix-proxy-addis-west',
    'HAWASA':  'zabbix-proxy-hawassa',
    'WOLAITA': 'zabbix-proxy-hawassa',
    'SOUTH WEST': 'zabbix-proxy-hawassa',
    'ADAMA':   'zabbix-proxy-adama',
    'NEKEMTE': 'zabbix-proxy-adama',
    'JIMMA':   'zabbix-proxy-adama',
    'DESSIE':  'zabbix-proxy-dessie',
    'DIRE DAWA': 'zabbix-proxy-dessie',
    'BAHIR DAR': 'zabbix-proxy-bahirdar',
    'MEKELLE': 'zabbix-proxy-bahirdar',
}


def csv_atms():
    atms = []
    with open(CSV_PATH, newline='') as f:
        for row in csv.DictReader(f):
            tid = row.get('terminal_id', '').strip()
            if tid:
                atms.append(row)
    return atms


class Zabbix:
    def __init__(self):
        self.token = None
        self.session = __import__('requests').Session()

    def call(self, method, params, retries=2):
        if method != 'user.login' and not self.token:
            self.login()
        for attempt in range(retries):
            try:
                r = self.session.post(ZBX_URL, json={
                    'jsonrpc': '2.0', 'method': method,
                    'params': params, 'auth': self.token, 'id': 1
                }, timeout=30)
                data = r.json()
                if 'error' in data:
                    print(f"  API error [{method}]: {data['error']['data']}")
                    return None
                return data.get('result')
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                print(f"  API call failed [{method}]: {e}")
                return None

    def login(self):
        r = self.session.post(ZBX_URL, json={
            'jsonrpc': '2.0', 'method': 'user.login',
            'params': {'username': ZBX_USER, 'password': ZBX_PASS},
            'id': 1
        }, timeout=10)
        self.token = r.json().get('result')
        if self.token:
            print("  Authenticated")
        else:
            print(f"  Login failed: {r.text}")

    def import_template(self, xml_path):
        with open(xml_path) as f:
            xml_data = f.read()
        print(f"  Importing {os.path.basename(xml_path)}...")
        return self.call('configuration.import', {
            'format': 'xml',
            'rules': {
                'templates': {'createMissing': True, 'updateExisting': True},
                'templateGroups': {'createMissing': True, 'updateExisting': True},
                'items': {'createMissing': True, 'updateExisting': True},
                'triggers': {'createMissing': True, 'updateExisting': True},
                'discoveryRules': {'createMissing': True, 'updateExisting': True},
                'valueMaps': {'createMissing': True, 'updateExisting': True},
            },
            'source': xml_data,
        })

    def get_template_id(self, name):
        result = self.call('template.get', {
            'output': ['templateid'],
            'filter': {'name': [name]}
        })
        if result:
            return result[0]['templateid']
        return None

    def get_or_create_group(self, name):
        result = self.call('hostgroup.get', {
            'output': ['groupid'],
            'filter': {'name': [name]}
        })
        if result:
            return result[0]['groupid']
        result = self.call('hostgroup.create', {'name': name})
        if result:
            return result['groupids'][0]
        return None

    def get_or_create_proxy(self, name):
        result = self.call('proxy.get', {
            'output': ['proxyid'],
            'filter': {'host': [name]}
        })
        if result:
            return result[0]['proxyid']
        return None

    def create_host(self, host, visible_name, template_ids, group_ids, ip,
                    proxy_id=None):
        interface = {
            'type': 2,  # SNMP
            'main': 1,
            'useip': 1,
            'ip': ip,
            'dns': '',
            'port': '161',
            'details': {
                'version': 2,
                'bulk': 0,
                'community': 'dashen_sim',
            }
        }
        params = {
            'host': host,
            'name': visible_name,
            'templates': [{'templateid': tid} for tid in template_ids],
            'groups': [{'groupid': gid} for gid in group_ids],
            'interfaces': [interface],
            'status': 0,  # monitored
        }
        if proxy_id:
            params['proxy_hostid'] = proxy_id
        result = self.call('host.create', params)
        return result is not None


def dry_run(atms):
    vendors = {}
    districts = {}
    for a in atms:
        v = a.get('vendor', '')
        d = a.get('district', '') or 'UNKNOWN'
        vendors[v] = vendors.get(v, 0) + 1
        districts[d] = districts.get(d, 0) + 1

    print(f"  ATMs to register: {len(atms)}")
    print(f"  Vendors: {vendors}")
    print(f"  Districts: {len(districts)}")
    for d, c in sorted(districts.items(), key=lambda x: -x[1])[:15]:
        print(f"    {d or 'UNKNOWN'}: {c}")
    print(f"  No changes made (dry run). Use --apply to execute.")


def do_apply(atms, import_templates=False):
    z = Zabbix()
    z.login()

    # Step 1: Import templates if requested
    if import_templates:
        z.import_template(TEMPLATE_NCR_PATH)
        z.import_template(TEMPLATE_GRG_PATH)
        print("  Templates imported, waiting 3s for indexing...")
        time.sleep(3)

    # Step 2: Verify templates exist
    ncr_tid = z.get_template_id(TEMPLATE_NCR_NAME)
    grg_tid = z.get_template_id(TEMPLATE_GRG_NAME)
    print(f"  NCR template: {'found (' + ncr_tid + ')' if ncr_tid else 'NOT FOUND'}")
    print(f"  GRG template: {'found (' + grg_tid + ')' if grg_tid else 'NOT FOUND'}")

    if not ncr_tid and not grg_tid:
        print("  ERROR: No ATM templates found. Use --import-templates or import manually.")
        return

    # Step 3: Create host groups
    grp_all = z.get_or_create_group('ATMs')
    grp_ncr = z.get_or_create_group('ATM-NCR')
    grp_grg = z.get_or_create_group('ATM-GRG')
    print(f"  Groups: ATMs={grp_all}, NCR={grp_ncr}, GRG={grp_grg}")

    # Per-district groups
    district_groups = {}
    for a in atms:
        d = a.get('district', '') or 'UNKNOWN'
        if d not in district_groups:
            gid = z.get_or_create_group(f'ATM-{d}')
            district_groups[d] = gid

    # Step 4: Create hosts
    created = 0
    skipped = 0
    no_ip = 0
    for a in atms:
        host = a.get('terminal_id', '').strip()
        name = a.get('atm_name', '').strip() or host
        ip = a.get('ip_address', '').strip()
        vendor = a.get('vendor', '').strip()

        if not ip:
            no_ip += 1
            continue
        if not host:
            skipped += 1
            continue

        # Check if already exists
        existing = z.call('host.get', {
            'output': ['hostid'],
            'filter': {'host': [host]}
        })
        if existing:
            skipped += 1
            continue

        # Select template and vendor group
        if vendor == 'NCR' and ncr_tid:
            template_ids = [ncr_tid]
            vgroup = grp_ncr
        elif vendor == 'GRG' and grg_tid:
            template_ids = [grg_tid]
            vgroup = grp_grg
        else:
            skipped += 1
            continue

        d = a.get('district', '') or 'UNKNOWN'
        group_ids = [grp_all, vgroup, district_groups.get(d, grp_all)]

        proxy_id = DISTRICT_PROXY.get(d)

        ok = z.create_host(host, name, template_ids, group_ids, ip, proxy_id)
        if ok:
            created += 1
        else:
            print(f"  Failed to create host {host}")
            skipped += 1

        # Rate limit: Zabbix API can handle ~100 hosts/min
        if created % 50 == 0:
            print(f"  Progress: {created} created...")
            time.sleep(1)

    print(f"\n  Done: {created} created, {skipped} skipped, {no_ip} without IP")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Register ATMs in Zabbix")
    parser.add_argument("--apply", action="store_true", help="Execute changes")
    parser.add_argument("--import-templates", action="store_true",
                        help="Also import NCR/GRG templates from XML files")
    args = parser.parse_args()

    atms = csv_atms()
    print(f"Loaded {len(atms)} ATMs from CSV")

    if not args.apply:
        dry_run(atms)
        return

    do_apply(atms, import_templates=args.import_templates)


if __name__ == "__main__":
    main()
