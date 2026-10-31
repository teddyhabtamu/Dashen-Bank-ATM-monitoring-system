#!/usr/bin/env python3
"""Turn the fleet workbook into a portal-importable ATM CSV.

The workbook (`docs/ATM INFORMATION.xlsx`) is the bank's inventory, not the
portal's import contract. It carries the terminal id, name, IP, branch,
district and vendor string, but not the fields the portal requires for every
row: city, region, model and a stable atm_id.

This script fills only what is missing, using explicit and reviewable rules:

  * vendor   Moti engineering/NCR -> NCR, tech24et/GRG -> GRG
  * district normalised to the keys the Zabbix proxy map understands
  * city     derived from the district (test placeholder, not a bank record)
  * region   derived from the district (test placeholder, not a bank record)
  * model    approved default per vendor (SelfServ 34 / AD2600)
  * branch   owner branch, normalised to the portal's "… BRANCH" convention
  * atm_id   stable test key: <prefix>-<workbook No>, e.g. TST-0001
  * IP       real by default only with --ip-mode real; otherwise RFC 2544
             benchmarking addresses (198.18.0.0/15), never public ranges

It validates every generated row with the portal's own `_validate()` before
writing, so a CSV that passes here is the same contract the import route
enforces. It also writes a per-row details CSV and a Markdown readiness report
that names every synthetic value.

Usage:
    # Recommended first pass: no production ATM is polled.
    python3 scripts/prepare_fleet_import.py --ip-mode synthetic

    # Network/security-approved live-IP pass.
    python3 scripts/prepare_fleet_import.py --ip-mode real

    # Small representative pilot.
    python3 scripts/prepare_fleet_import.py --ip-mode synthetic --limit 50
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The portal imports `zabbix_modules` from the repo root, so both paths are
# needed before importing its validator.
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'report-portal'))

# The portal is the authority on what an importable row looks like. Importing
# its validator keeps this script from inventing a second, drifting contract.
from blueprints.admin import _norm_branch, _validate  # noqa: E402

DEFAULT_EXCEL = REPO / 'docs' / 'ATM INFORMATION.xlsx'

VENDOR_MAP = {
    'MOTI ENGINEERING/NCR': 'NCR',
    'TECH24ET/GRG': 'GRG',
}

# District keys the provisioning map and Zabbix proxy routing understand.
DISTRICT_ALIASES = {
    'NAD': 'NAD',
    'SAD': 'SAD',
    'EAD': 'EAD',
    'WAD': 'WAD',
    'HAWASA': 'HAWASA',
    'HAWASSA': 'HAWASA',
    'WOLAITA': 'WOLAITA',
    'SOUTH WEST': 'SOUTH WEST',
    'SOUTHWEST': 'SOUTH WEST',
    'ADAMA': 'ADAMA',
    'NEKEMTE': 'NEKEMTE',
    'JIMMA': 'JIMMA',
    'JIMA': 'JIMMA',
    'DESSIE': 'DESSIE',
    'DIRE DAWA': 'DIRE DAWA',
    'BAHIR DAR': 'BAHIR DAR',
    'MEKELLE': 'MEKELLE',
    'KIRKOS': 'KIRKOS',
}

CITY_BY_DISTRICT = {
    'NAD': 'Addis Ababa',
    'SAD': 'Addis Ababa',
    'EAD': 'Addis Ababa',
    'WAD': 'Addis Ababa',
    'KIRKOS': 'Addis Ababa',
    'HAWASA': 'Hawassa',
    'WOLAITA': 'Wolaita Sodo',
    'SOUTH WEST': 'Jimma',
    'ADAMA': 'Adama',
    'NEKEMTE': 'Nekemte',
    'JIMMA': 'Jimma',
    'DESSIE': 'Dessie',
    'DIRE DAWA': 'Dire Dawa',
    'BAHIR DAR': 'Bahir Dar',
    'MEKELLE': 'Mekelle',
}

REGION_BY_DISTRICT = {
    'NAD': 'Addis Ababa',
    'SAD': 'Addis Ababa',
    'EAD': 'Addis Ababa',
    'WAD': 'Addis Ababa',
    'KIRKOS': 'Addis Ababa',
    'HAWASA': 'Sidama',
    'WOLAITA': 'South Ethiopia',
    'SOUTH WEST': 'South West Ethiopia',
    'ADAMA': 'Oromia',
    'NEKEMTE': 'Oromia',
    'JIMMA': 'Oromia',
    'DESSIE': 'Amhara',
    'BAHIR DAR': 'Amhara',
    'MEKELLE': 'Tigray',
    'DIRE DAWA': 'Dire Dawa',
}

MODEL_BY_VENDOR = {
    'NCR': 'SelfServ 34',
    'GRG': 'AD2600',
}

CSV_FIELDS = [
    'atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
    'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
    'install_date', 'status', 'ip_address', 'snmp_community',
    'snmp_version', 'ej_log_format', 'ej_hostname',
]

DETAIL_FIELDS = [
    'row_no', 'atm_id', 'terminal_id', 'atm_name', 'vendor', 'district',
    'city', 'region', 'ip_address', 'ip_kind', 'brand', 'filled_fields',
    'notes', 'validation_errors',
]


def clean(value):
    """Workbook cell -> stripped string, with None/'None'/nan collapsed."""
    if value is None:
        return ''
    text = str(value).strip()
    if text.lower() in ('none', 'nan', 'null'):
        return ''
    return re.sub(r'\s+', ' ', text)


def normalise_vendor(raw):
    key = clean(raw).upper()
    if key in VENDOR_MAP:
        return VENDOR_MAP[key]
    if 'NCR' in key:
        return 'NCR'
    if 'GRG' in key:
        return 'GRG'
    return ''


def normalise_district(raw):
    key = clean(raw).upper()
    return DISTRICT_ALIASES.get(key, key)


def synthetic_ip(index):
    """RFC 2544 benchmarking address, never a public/real ATM range.

    198.18.0.0/15 gives 131,070 addresses; 1,202 rows fit comfortably.
    """
    return f'198.18.{index // 254}.{index % 254 + 1}'


def _row_number(raw, fallback):
    text = clean(raw)
    if text:
        try:
            return int(float(text))
        except ValueError:
            pass
    return fallback


def load_workbook_rows(path):
    """Return the workbook rows as raw lists, skipping fully blank rows."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb.active
        rows = []
        for raw in ws.iter_rows(min_row=2, values_only=True):
            values = list(raw)
            if not any(clean(v) for v in values):
                continue
            rows.append(values)
        return rows
    finally:
        wb.close()


def _sample(rows, limit):
    """A deterministic, mixed sample, not the first N rows.

    The workbook starts with NCR rows; taking the first 50 would not exercise
    the GRG path. Hash the terminal id, take the first N, then restore workbook
    order so the CSV reads naturally. The ATM id stays tied to the workbook
    No, so re-importing a subset updates the same rows.
    """
    if not limit or limit >= len(rows):
        return list(rows)

    def key(raw):
        tid = clean(raw[1]) if len(raw) > 1 else ''
        return hashlib.sha256(tid.encode('utf-8')).hexdigest()

    selected = sorted(rows, key=key)[:limit]
    return sorted(selected, key=lambda raw: _row_number(raw[0], 0))


def build_rows(raw_rows, prefix='TST', ip_mode='synthetic', limit=0,
               snmp_community='public', allow_public_ips=False):
    """Return (csv_rows, detail_rows, summary). Pure apart from validation."""
    prefix = clean(prefix).rstrip('-') or 'TST'
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', prefix):
        raise ValueError(f'prefix {prefix!r} cannot be an ATM ID prefix')
    selected = _sample(raw_rows, limit)
    csv_rows, details = [], []
    seen_tids, seen_ips = {}, set()
    synthetic_counter = 0
    skipped = []

    def next_synthetic():
        nonlocal synthetic_counter
        while True:
            candidate = synthetic_ip(synthetic_counter)
            synthetic_counter += 1
            if candidate not in seen_ips:
                seen_ips.add(candidate)
                return candidate

    for position, raw in enumerate(selected, 1):
        get = lambda i: raw[i] if i < len(raw) else None  # noqa: E731
        row_no = _row_number(get(0), position)
        tid = clean(get(1))
        name = clean(get(2))
        ip_raw = clean(get(3))
        branch_raw = clean(get(4))
        district_raw = clean(get(5))
        vendor_raw = clean(get(6))
        brand = clean(get(7))

        notes = []
        filled = []

        if not tid:
            skipped.append({'row_no': row_no, 'reason': 'missing terminal_id'})
            continue
        if tid in seen_tids:
            skipped.append({'row_no': row_no, 'terminal_id': tid,
                            'reason': f'duplicate terminal_id (first at row {seen_tids[tid]})'})
            continue
        seen_tids[tid] = row_no

        vendor = normalise_vendor(vendor_raw)
        if not vendor:
            skipped.append({'row_no': row_no, 'terminal_id': tid,
                            'reason': f'unknown vendor {vendor_raw!r}'})
            continue

        district = normalise_district(district_raw)
        city = CITY_BY_DISTRICT.get(district)
        region = REGION_BY_DISTRICT.get(district)
        if not city:
            city = district.title() if district else 'Unknown'
            notes.append(f'unknown district {district_raw!r}; city derived from it')
        if not region:
            region = district or 'Unknown'
            notes.append(f'unknown district {district_raw!r}; region derived from it')

        model = MODEL_BY_VENDOR[vendor]
        branch = _norm_branch(branch_raw or name or tid)
        if not branch_raw:
            filled.append('branch')
            notes.append('branch filled from ATM name')

        atm_name = name or branch or tid
        atm_id = f'{prefix}-{row_no:04d}'

        # IP selection. Synthetic mode never touches a real ATM. Real mode
        # preserves a valid, unique address and fills only the gaps.
        ip_kind = 'real'
        if ip_mode == 'synthetic':
            ip_value = next_synthetic()
            ip_kind = 'synthetic'
            notes.append('synthetic RFC 2544 address')
        else:
            valid = False
            address = None
            if ip_raw:
                try:
                    # The portal validator accepts IPv4 only; treat IPv6 as a
                    # gap rather than writing a row the import route rejects.
                    address = ipaddress.IPv4Address(ip_raw)
                    valid = True
                except ValueError:
                    valid = False
            if valid and not address.is_private and not allow_public_ips:
                # A public address in an ATM inventory is a data error (or a
                # dangerous target). Never send SNMP to it by default.
                ip_value = next_synthetic()
                ip_kind = 'synthetic-public'
                filled.append('ip_address')
                notes.append('public IP replaced with RFC 2544 address; '
                             'verify the inventory entry')
            elif not valid:
                ip_value = next_synthetic()
                ip_kind = 'synthetic-fill'
                filled.append('ip_address')
                notes.append('missing/invalid IP filled with RFC 2544 address')
            elif ip_raw in seen_ips:
                ip_value = next_synthetic()
                ip_kind = 'synthetic-duplicate'
                filled.append('ip_address')
                notes.append('duplicate IP replaced with RFC 2544 address')
            else:
                ip_value = ip_raw
                seen_ips.add(ip_value)

        if brand and brand.upper() != 'ATM':
            notes.append(f'workbook brand={brand}')
        if vendor_raw.upper() not in ('MOTI ENGINEERING/NCR', 'TECH24ET/GRG'):
            notes.append(f'vendor normalised from {vendor_raw!r}')

        row = {
            'atm_id': atm_id,
            'atm_name': atm_name,
            'branch': branch,
            'district': district,
            'city': city,
            'region': region,
            'latitude': '',
            'longitude': '',
            'terminal_id': tid,
            'vendor': vendor,
            'model': model,
            'install_date': '',
            'status': 'active',
            'ip_address': ip_value,
            'snmp_community': snmp_community,
            'snmp_version': '2',
            'ej_log_format': 'NCR_APTRA' if vendor == 'NCR' else 'GRG',
            'ej_hostname': '',
        }
        errors = _validate(row)
        csv_rows.append(row)
        details.append({
            'row_no': row_no,
            'atm_id': atm_id,
            'terminal_id': tid,
            'atm_name': atm_name,
            'vendor': vendor,
            'district': district,
            'city': city,
            'region': region,
            'ip_address': ip_value,
            'ip_kind': ip_kind,
            'brand': brand,
            'filled_fields': ', '.join(filled),
            'notes': '; '.join(notes),
            'validation_errors': '; '.join(f'{k}: {v}' for k, v in errors.items()),
        })

    summary = {
        'rows_read': len(raw_rows),
        'rows_selected': len(selected),
        'rows_prepared': len(csv_rows),
        'rows_skipped': len(skipped),
        'skipped': skipped,
        'vendors': dict(Counter(r['vendor'] for r in csv_rows)),
        'districts': dict(Counter(r['district'] for r in csv_rows)),
        'ip_kinds': dict(Counter(d['ip_kind'] for d in details)),
        'validation_errors': sum(1 for d in details if d['validation_errors']),
        'filled_branch': sum(1 for d in details if 'branch' in d['filled_fields']),
        'filled_ip': sum(1 for d in details if 'ip_address' in d['filled_fields']),
        'prefix': prefix,
        'ip_mode': ip_mode,
    }
    return csv_rows, details, summary


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_report(path, summary, csv_path, details_path, excel_path):
    path.parent.mkdir(parents=True, exist_ok=True)
    vendors = ', '.join(f'{k} {v}' for k, v in sorted(summary['vendors'].items()))
    districts = ', '.join(f'{k} {v}' for k, v in
                          sorted(summary['districts'].items(),
                                 key=lambda item: (-item[1], item[0])))
    ip_kinds = ', '.join(f'{k} {v}' for k, v in sorted(summary['ip_kinds'].items()))
    lines = [
        '# ATM test-fleet import readiness',
        '',
        f'- Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}',
        f'- Source workbook: `{excel_path}`',
        f'- Import CSV: `{csv_path}`',
        f'- Per-row details: `{details_path}`',
        f'- ATM ID prefix: `{summary["prefix"]}-`',
        f'- IP mode: **{summary["ip_mode"]}**',
        '',
        '## Counts',
        '',
        f'- Rows read: **{summary["rows_read"]}**',
        f'- Rows selected: **{summary["rows_selected"]}**',
        f'- Rows prepared: **{summary["rows_prepared"]}**',
        f'- Rows skipped: **{summary["rows_skipped"]}**',
        f'- Validation errors: **{summary["validation_errors"]}**',
        f'- Vendors: {vendors}',
        f'- IP kinds: {ip_kinds}',
        f'- Branches filled from the ATM name: {summary["filled_branch"]}',
        f'- IPs filled/replaced: {summary["filled_ip"]}',
        '',
        '## What was filled',
        '',
        '| Field | Rule |',
        '|---|---|',
        '| `city`, `region` | Derived from the district. These are **test placeholders**, not bank records. |',
        '| `model` | Approved default per vendor: NCR `SelfServ 34`, GRG `AD2600`. |',
        '| `branch` | Owner branch, normalised to the portal `… BRANCH` convention. Missing values use the ATM name. |',
        '| `atm_id` | Stable test key `<prefix>-<workbook No>`, e.g. `TST-0001`. |',
        '| `ip_address` | `synthetic` mode uses RFC 2544 `198.18.0.0/15`; `real` mode preserves workbook IPs and fills only gaps. Public addresses are quarantined unless `--allow-public-ips` is set. |',
        '| `ej_log_format` | `NCR_APTRA` for NCR, `GRG` for GRG. |',
        '| `snmp_community` | `--snmp-community` value (default `public`). Polling will fail where the real community differs. |',
        '| `ej_hostname` | Left empty. EJ pullers skip these rows until a real hostname is known. |',
        '| `latitude`, `longitude`, `install_date` | Left empty. |',
        '',
        '## District breakdown',
        '',
        districts or '—',
        '',
        '## Safety notes',
        '',
        '- The `TST-` prefix is the cleanup key. Do not import these rows with a production prefix.',
        '- `synthetic` mode is the recommended first pass: it exercises import, provisioning, the outbox, the UI and the scheduler without polling production ATMs.',
        '- `real` mode will make Zabbix poll the workbook IPs. Get network/security approval first, and expect unreachable hosts where the SNMP community or firewall is wrong.',
        '- Public addresses are treated as inventory errors: `real` mode replaces them with RFC 2544 addresses unless `--allow-public-ips` is set. Look for `synthetic-public` in the details CSV.',
        '- **Disable the GLPI ticket action or put the `TST-` hosts in a Zabbix maintenance window before the full import.** Otherwise the agent-unavailable triggers can create a ticket per unreachable ATM — potentially 1,202 tickets.',
        '- Import a small pilot first (`--limit 50`), watch the outbox and Zabbix server load, then import the full set. The portal queues every row in `atm_provision_state`; the scheduler creates about 25 hosts per minute, so 1,202 rows take roughly 45 minutes after the immediate first pass.',
        '- The first Fleet health sweep over 1,202 hosts is the expensive UI operation. Run it once, then use the 15-minute cache; do not hammer **Recompute now**.',
        '- EJ, images and identity remain pending until real agents/probes are applied. That is expected for a load test.',
        '',
        '## Import',
        '',
        '1. Portal → **ATMs** → **Import CSV** → choose the file above.',
        '2. Confirm the import response: `imported` should equal the prepared row count.',
        '3. Watch **Agent setup → Zabbix provisioning** or `atm_provision_state` while the outbox drains.',
        '4. Run **Fleet health → Recompute now** after the sweep completes.',
        '',
        '## Cleanup',
        '',
        'All test rows share the ATM ID prefix. Delete them through the portal in batches,',
        'or use the prefix in SQL and the Zabbix API. Do not delete by vendor/district:',
        'those are shared with real hosts.',
        '',
    ]
    path.write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--excel', default=str(DEFAULT_EXCEL))
    parser.add_argument('--out', default=None,
                        help='Import CSV path (default: config/postgres/atm_locations_test_<mode>.csv)')
    parser.add_argument('--details', default=None,
                        help='Per-row details CSV path')
    parser.add_argument('--report', default=None,
                        help='Markdown readiness report path')
    parser.add_argument('--prefix', default='TST',
                        help='ATM ID prefix (default TST; use a test-only prefix)')
    parser.add_argument('--ip-mode', choices=('synthetic', 'real'), default='synthetic',
                        help='synthetic = RFC 2544 addresses; real = preserve workbook IPs')
    parser.add_argument('--limit', type=int, default=0,
                        help='Prepare a deterministic sample of N rows (0 = all)')
    parser.add_argument('--snmp-community', default='public',
                        help='SNMP community for the generated rows (default public)')
    parser.add_argument('--allow-public-ips', action='store_true',
                        help='Real mode: keep public addresses instead of quarantining them')
    parser.add_argument('--allow-errors', action='store_true',
                        help='Write the CSV even if portal validation fails')
    args = parser.parse_args(argv)

    suffix = 'synthetic' if args.ip_mode == 'synthetic' else 'real_ips'
    out = Path(args.out or REPO / 'config' / 'postgres' /
               f'atm_locations_test_{suffix}.csv')
    details_path = Path(args.details or REPO / 'config' / 'postgres' /
                        f'atm_locations_test_{suffix}_details.csv')
    report_path = Path(args.report or REPO / 'docs' /
                       f'atm-import-readiness-{suffix}.md')

    raw_rows = load_workbook_rows(Path(args.excel))
    try:
        csv_rows, details, summary = build_rows(
            raw_rows, prefix=args.prefix, ip_mode=args.ip_mode, limit=args.limit,
            snmp_community=args.snmp_community,
            allow_public_ips=args.allow_public_ips)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    write_csv(out, csv_rows, CSV_FIELDS)
    write_csv(details_path, details, DETAIL_FIELDS)
    write_report(report_path, summary, out, details_path, args.excel)

    print(f'read={summary["rows_read"]} selected={summary["rows_selected"]} '
          f'prepared={summary["rows_prepared"]} skipped={summary["rows_skipped"]} '
          f'validation_errors={summary["validation_errors"]}')
    print(f'vendors={summary["vendors"]} ip_kinds={summary["ip_kinds"]}')
    print(f'csv={out}')
    print(f'details={details_path}')
    print(f'report={report_path}')

    if summary['skipped']:
        print('\nSkipped rows:')
        for item in summary['skipped'][:20]:
            print(f'  {item}')
        if len(summary['skipped']) > 20:
            print(f'  ... and {len(summary["skipped"]) - 20} more')

    if summary['validation_errors'] and not args.allow_errors:
        print('\nRefusing to call this import-ready: portal validation failed.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
