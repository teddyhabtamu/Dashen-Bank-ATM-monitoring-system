"""Import templates, create host groups, and register all 1,202 ATMs in Zabbix.

Usage:
    python3 scripts/sync_atms_to_zabbix.py                     # preview
    python3 scripts/sync_atms_to_zabbix.py --apply              # create groups + hosts
    python3 scripts/sync_atms_to_zabbix.py --apply --import-templates  # also import templates

Reads ATM data from config/postgres/atm_locations.csv.
Connects to Zabbix API at localhost:8080.
"""

import csv, json, os, sys, time

# .env fills whatever the shell didn't export (no more per-shell exports;
# explicit environment still wins). Must run before the constants below.
from zbx_env import ensure_loaded as _ensure_env
_ensure_env()

ZBX_URL = os.environ.get("ZBX_URL", "http://localhost:8080/api_jsonrpc.php")
ZBX_USER = os.environ.get("ZBX_USER", "Admin")
ZBX_PASS = os.environ.get("ZBX_PASS", "zabbix")
CSV_PATH = "config/postgres/atm_locations.csv"
TEMPLATE_NCR_PATH = "config/zabbix/template_ncr_snmp.xml"
TEMPLATE_GRG_PATH = "config/zabbix/template_grg_snmp.xml"
TEMPLATE_WMI_PATH = "config/zabbix/template_atm_wmi.xml"

TEMPLATE_NCR_NAME = "Dashen Bank ATM Hardware"
TEMPLATE_GRG_NAME = "Dashen Bank ATM Hardware - GRG"
TEMPLATE_GRG_XFS_NAME = "Dashen Bank ATM Hardware - GRG XFS"
# Must match generate_wmi_template.TEMPLATE_NAME (note: shared "Dashen" typo
# is deliberate consistency — renaming is post-UAT cleanup, see runbook).
TEMPLATE_WMI_NAME = "Dashen Bank ATM WMI Parts"
TEMPLATE_AGENT_NAME = "Dashen Bank ATM Agent"

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


def parse_snmp_version(raw):
    """Coerce a DB-or-CSV snmp_version to int (default 2).

    DB returns int, CSV returns str, both may be empty/None/garbage.
    Pure function — covered by tests/test_sync_atms.py (bulk day must
    never die mid-loop on a column type again).
    """
    if raw is None:
        return 2
    text = str(raw).strip()
    try:
        return int(text or 2)
    except ValueError:
        return 2


def resolve_host_key(row):
    """Zabbix technical name: atm_id, falling back to terminal_id."""
    return (row.get('atm_id', '') or '').strip() or \
           (row.get('terminal_id', '') or '').strip()


def select_templates(vendor, ncr_tid, grg_tid, wmi_tid, grg_xfs_tid=None,
                     agent_tid=None):
    """(template_ids, vendor_group_name) for a row, or (None, None) to skip.

    NCR hosts take the WMI Parts template alongside SNMP (agent items).
    GRG prefers the XFS template and falls back to SNMP while old hosts are
    being migrated. Both vendors also take the shared Agent template when it
    exists; that template is the single owner of `atm.identity`.
    Unknown vendor or missing vendor template → skip.
    """
    if vendor == 'NCR' and ncr_tid:
        ids = [ncr_tid] + ([wmi_tid] if wmi_tid else [])
        if agent_tid:
            ids.append(agent_tid)
        return (ids, 'ATM-NCR')
    if vendor == 'GRG':
        grg = grg_xfs_tid or grg_tid
        if grg:
            ids = [grg] + ([agent_tid] if agent_tid else [])
            return (ids, 'ATM-GRG')
    return (None, None)


def csv_atms():
    atms = []
    with open(CSV_PATH, newline='') as f:
        for row in csv.DictReader(f):
            tid = row.get('terminal_id', '').strip()
            if tid:
                atms.append(row)
    return atms


def db_atms():
    """Read ATM inventory from the postgres atm_locations table (single source of truth)."""
    try:
        import psycopg2
    except ImportError:
        print("  psycopg2 not installed - run 'pip install psycopg2-binary' or use --csv")
        return None
    try:
        conn = psycopg2.connect(
            host=os.environ.get('DB_HOST', 'postgres'),
            dbname=_require_db_name(),
            user=os.environ.get('DB_USER', 'zabbix'),
            password=os.environ.get('DB_PASS', ''),
        )
        cur = conn.cursor()
        cur.execute("""SELECT atm_id, atm_name, vendor, ip_address, snmp_community,
                              snmp_version, district, terminal_id, branch, status
                       FROM atm_locations""")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"  DB read failed: {e}")
        return None


class Zabbix:
    def __init__(self):
        self.token = None
        self.session = __import__('requests').Session()

    def call(self, method, params, retries=2, timeout=30):
        if method != 'user.login' and not self.token:
            self.login()
        for attempt in range(retries):
            try:
                r = self.session.post(ZBX_URL, json={
                    'jsonrpc': '2.0', 'method': method,
                    'params': params, 'auth': self.token, 'id': 1
                }, timeout=timeout)
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
                'template_groups': {'createMissing': True, 'updateExisting': True},
                'items': {'createMissing': True, 'updateExisting': True},
                'triggers': {'createMissing': True, 'updateExisting': True},
                'discoveryRules': {'createMissing': True, 'updateExisting': True},
                'valueMaps': {'createMissing': True, 'updateExisting': True},
            },
            'source': xml_data,
        }, retries=3, timeout=300)

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
                    proxy_id=None, community='dashen_sim', version=2, snmp_v3=None,
                    vendor='', agent_interface=False):
        details = {'version': version, 'bulk': 0}
        if version == 3 and snmp_v3:
            details.update({
                'securityname': snmp_v3.get('user'),
                'authpassphrase': snmp_v3.get('auth_pass'),
                'privpassphrase': snmp_v3.get('priv_pass'),
                'authprotocol': snmp_v3.get('auth_proto', 'SHA'),
                'privprotocol': snmp_v3.get('priv_proto', 'AES'),
                'securitylevel': snmp_v3.get('level', 'authPriv'),
                'contextname': '',
            })
        else:
            details['community'] = community
        interface = {
            'type': 2,  # SNMP
            'main': 1,
            'useip': 1,
            'ip': ip,
            'dns': '',
            'port': '161',
            'details': details,
        }
        interfaces = [interface]
        if agent_interface:
            # WMI/XFS/identity items are agent items — without this interface
            # they sit unsupported forever (proven on the test host, which
            # needed the agent row added by hand before Latest Data flowed).
            interfaces.append({
                'type': 1,  # Zabbix agent
                'main': 1,
                'useip': 1,
                'ip': ip,
                'dns': '',
                'port': '10050',
            })
        params = {
            'host': host,
            'name': visible_name,
            'templates': [{'templateid': tid} for tid in template_ids],
            'groups': [{'groupid': gid} for gid in group_ids],
            'interfaces': interfaces,
            'macros': [{'macro': '{$ATM_VENDOR}', 'value': vendor or 'Unknown'}],
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

    # Step 1: Import templates if requested (each template embeds its own
    # value maps, so no separate valuemap step is needed).
    # NOTE: WMI and shared Agent templates intentionally NOT imported here —
    # 6.4's configuration.import rejects generated XML tag-by-tag (proven:
    # <date>, <groups>, ZABBIX_AGENT constant). They are owned by
    # import_wmi_template_api.py and import_agent_template_api.py. Importing
    # them here would only print an API error and continue.
    if import_templates:
        z.import_template(TEMPLATE_NCR_PATH)
        z.import_template(TEMPLATE_GRG_PATH)
        print("  Templates imported, waiting 3s for indexing...")
        time.sleep(3)

    # Step 2: Verify templates exist
    ncr_tid = z.get_template_id(TEMPLATE_NCR_NAME)
    grg_tid = z.get_template_id(TEMPLATE_GRG_NAME)
    grg_xfs_tid = z.get_template_id(TEMPLATE_GRG_XFS_NAME)
    wmi_tid = z.get_template_id(TEMPLATE_WMI_NAME)
    agent_tid = z.get_template_id(TEMPLATE_AGENT_NAME)
    print(f"  NCR template: {'found (' + ncr_tid + ')' if ncr_tid else 'NOT FOUND'}")
    print(f"  GRG template: {'found (' + grg_tid + ')' if grg_tid else 'NOT FOUND'}")
    print(f"  GRG XFS template: {'found (' + grg_xfs_tid + ')' if grg_xfs_tid else 'NOT FOUND (run import_grg_xfs_template_api.py)'}")
    print(f"  WMI template: {'found (' + wmi_tid + ')' if wmi_tid else 'NOT FOUND (run import_wmi_template_api.py)'}")
    print(f"  Agent template: {'found (' + agent_tid + ')' if agent_tid else 'NOT FOUND (run import_agent_template_api.py)'}")

    if not ncr_tid and not grg_tid and not grg_xfs_tid:
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

    # Step 4: Resolve proxies by name to numeric IDs
    # (fresh installs have no proxies — those ATMs get monitored directly)
    proxy_ids = {}
    missing_proxies = set()
    for d, p in DISTRICT_PROXY.items():
        pid = z.get_or_create_proxy(p)
        if pid:
            proxy_ids[d] = pid
        else:
            missing_proxies.add(p)
    if missing_proxies:
        print("  WARNING: proxies not found in this Zabbix (hosts will be monitored"
              f" directly): {', '.join(sorted(missing_proxies))}")

    # Step 5: Create hosts
    created = 0
    skipped = 0
    no_ip = 0
    for a in atms:
        host = resolve_host_key(a)
        name = (a.get('atm_name', '') or '').strip() or host
        ip = (a.get('ip_address', '') or '').strip()
        vendor = (a.get('vendor', '') or '').strip()
        community = (a.get('snmp_community', '') or '').strip() or os.environ.get('ATM_SNMP_COMMUNITY', 'dashen_sim')
        # DB returns snmp_version as int, CSV as str — coerce via helper
        # (bulk day must never die mid-run after partial creates).
        version = parse_snmp_version(a.get('snmp_version', ''))

        snmp_v3 = None
        if version == 3:
            snmp_v3 = {
                'user': a.get('snmp_v3_user') or os.environ.get('SNMPV3_USER'),
                'auth_pass': a.get('snmp_v3_auth_pass') or os.environ.get('SNMPV3_AUTH_PASS'),
                'priv_pass': a.get('snmp_v3_priv_pass') or os.environ.get('SNMPV3_PRIV_PASS'),
                'auth_proto': a.get('snmp_v3_auth_proto') or os.environ.get('SNMPV3_AUTH_PROTO', 'SHA'),
                'priv_proto': a.get('snmp_v3_priv_proto') or os.environ.get('SNMPV3_PRIV_PROTO', 'AES'),
                'level': a.get('snmp_v3_level') or os.environ.get('SNMPV3_LEVEL', 'authPriv'),
            }

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

        # Select template and vendor group (pure helper — see tests).
        # NCR hosts also take the WMI Parts template (agent items) — without
        # it bulk-created NCR hosts would silently miss all 41 part items.
        template_ids, vgroup_name = select_templates(
            vendor, ncr_tid, grg_tid, wmi_tid, grg_xfs_tid, agent_tid)
        if template_ids is None:
            skipped += 1
            continue
        vgroup = grp_ncr if vgroup_name == 'ATM-NCR' else grp_grg

        d = a.get('district', '') or 'UNKNOWN'
        group_ids = [grp_all, vgroup, district_groups.get(d, grp_all)]

        proxy_id = proxy_ids.get(d)

        ok = z.create_host(host, name, template_ids, group_ids, ip, proxy_id,
                           community, version, snmp_v3, vendor,
                           agent_interface=(vendor in ('NCR', 'GRG')))
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



def _require_db_name():
    """DB_NAME must be explicit. Do not guess.

    Run inside a container it is set correctly; run on the host it is unset, and
    defaulting to 'zabbix' meant the script quietly operated on the pre-split
    database - the one the portal no longer reads - while reporting success.
    Syncing ATMs into the old copy is worse than not syncing at all, because the
    portal would then show a fleet that does not match the one it is serving.
    """
    name = os.environ.get('DB_NAME')
    if not name:
        sys.stderr.write(
            'DB_NAME is not set, so this cannot tell which database to write to.\n'
            'Run it inside a container that has it, or set DB_NAME explicitly.\n'
            'Refusing to assume.\n')
        sys.exit(2)
    return name


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Register ATMs in Zabbix")
    parser.add_argument("--apply", action="store_true", help="Execute changes")
    parser.add_argument("--import-templates", action="store_true",
                        help="Also import NCR/GRG templates from XML files")
    parser.add_argument("--csv", action="store_true",
                        help="Read ATM inventory from config/postgres/atm_locations.csv instead of the DB")
    parser.add_argument("--pilot", type=int, default=0, metavar="N",
                        help="Limit the run to the first N ATMs (pilot wave); 0 = all")
    args = parser.parse_args()

    if args.csv:
        atms = csv_atms()
        src = "CSV"
    else:
        atms = db_atms()
        src = "DB (atm_locations)"
        if not atms:
            print("  Falling back to CSV...")
            atms = csv_atms()
            src = "CSV (DB unavailable)"
    print(f"Loaded {len(atms)} ATMs from {src}")
    if args.pilot and args.pilot > 0:
        atms = atms[:args.pilot]
        print(f"Pilot mode: limited to first {len(atms)} ATM(s)")

    if not args.apply:
        dry_run(atms)
        return

    do_apply(atms, import_templates=args.import_templates)


if __name__ == "__main__":
    main()
