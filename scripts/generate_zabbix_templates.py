#!/usr/bin/env python3
"""Remap the Zabbix SNMP templates from the simulator OID root to real vendor OIDs.

The shipped templates (config/zabbix/template_ncr_snmp.xml,
template_grg_snmp.xml) poll the simulator OID tree 1.3.6.1.4.1.99999.<sim_oid>.
config/snmp/oid_mapping.csv maps each <sim_oid> to the real vendor enterprise OID
for a given vendor. This script rewrites every <snmp_oid> in the templates that
still points at the simulator root, using the mapping.

Run during UAT Phase 0 after filling oid_mapping.csv from a real SNMP walk:

    python3 scripts/generate_zabbix_templates.py --dry-run
    python3 scripts/generate_zabbix_templates.py

The original template is copied to <file>.simulator.bak once (the backup is never
overwritten), then the template is rewritten in place so sync_atms_to_zabbix.py
keeps importing the same filenames.
"""
import csv
import os
import re
import shutil
import sys
from pathlib import Path

SIM_PREFIX = "1.3.6.1.4.1.99999"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE_DIR = os.path.join(HERE, "..", "config", "zabbix")
DEFAULT_MAPPING = os.path.join(HERE, "..", "config", "snmp", "oid_mapping.csv")

TEMPLATES = {
    "NCR": "template_ncr_snmp.xml",
    "GRG": "template_grg_snmp.xml",
}

OID_RE = re.compile(r"<snmp_oid>(.*?)</snmp_oid>", re.DOTALL)


def _regular_file(path, label):
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise SystemExit(f"{label} must be a regular file: {path}")
    return candidate.resolve(strict=True)


def load_mapping(path):
    by_vendor = {}
    source = Path(path).expanduser()
    if not source.exists():
        print(f"  WARNING: mapping file not found: {path}")
        return by_vendor
    source = _regular_file(source, "OID mapping")
    # NOSONAR: resolved regular CSV file, not a symlink or directory.
    with source.open(newline="", encoding="utf-8") as f:  # NOSONAR
        for row in csv.DictReader(f):
            vendor = (row.get("vendor") or "").strip().upper()
            sim = (row.get("sim_oid") or "").strip().lstrip(".")
            real = (row.get("real_oid") or "").strip().lstrip(".")
            if not vendor or not sim or not real:
                continue
            by_vendor.setdefault(vendor, {})[sim] = real
    return by_vendor


def remap_text(text, vendor, mappings):
    mapped = 0
    unmapped = []
    used = mappings.get(vendor, {})

    def sub(match):
        nonlocal mapped
        oid = match.group(1).strip()
        if not oid.startswith(SIM_PREFIX):
            return match.group(0)
        sim_oid = oid[len(SIM_PREFIX):].lstrip(".")
        real = used.get(sim_oid)
        if not real:
            unmapped.append(sim_oid)
            return match.group(0)
        mapped += 1
        return f"<snmp_oid>{real}</snmp_oid>"

    new_text = OID_RE.sub(sub, text)
    return new_text, mapped, unmapped


def process(template_dir, mapping_path, dry_run):
    by_vendor = load_mapping(mapping_path)
    total_mapped = 0
    total_unmapped = 0
    results = []  # (path, new_text) collected before any write
    base = Path(template_dir).expanduser()
    if not base.is_dir():
        print(f"  SKIP: template directory not found: {template_dir}")
        return False
    base = base.resolve(strict=True)
    for vendor, fname in TEMPLATES.items():
        path = base / fname
        if not path.is_file():
            print(f"  SKIP {vendor}: {path} not found")
            continue
        # NOSONAR: resolved regular template inside the validated directory.
        with path.open(encoding="utf-8") as f:  # NOSONAR
            text = f.read()
        new_text, mapped, unmapped = remap_text(text, vendor, by_vendor)
        total_mapped += mapped
        total_unmapped += len(unmapped)
        print(f"  {vendor} ({fname}): {mapped} OID(s) remapped, {len(unmapped)} unmapped")
        for u in sorted(set(unmapped)):
            print(f"      unmapped sim_oid: {u}")
        results.append((path, new_text))

    # Refuse to write a partially-remapped template: if any OID still points at the
    # simulator root, abort BEFORE touching any file so nothing is left unsupported.
    if not dry_run and total_unmapped > 0:
        print(f"  ABORT: {total_unmapped} OID(s) still point at the simulator root "
              f"1.3.6.1.4.1.99999.* and have no real-OID row in {mapping_path}.")
        print("         Fill those rows in oid_mapping.csv first; refusing to write a "
              "partially-remapped template so no items are left silently unsupported.")
        return False

    if not dry_run:
        for path, new_text in results:
            bak = Path(str(path) + ".simulator.bak")
            if not bak.exists():
                shutil.copy2(path, bak)  # NOSONAR
                print(f"      backup -> {bak}")
            # NOSONAR: resolved regular template inside the validated directory.
            with path.open("w", encoding="utf-8") as f:  # NOSONAR
                f.write(new_text)
    print(f"  Done. Total remapped: {total_mapped}, still unmapped: {total_unmapped}")
    if dry_run:
        print("  (dry-run: no files written)")
    return True


def main():
    import argparse
    p = argparse.ArgumentParser(description="Remap Zabbix templates to real vendor OIDs")
    p.add_argument("--template-dir", default=DEFAULT_TEMPLATE_DIR)
    p.add_argument("--mapping", default=DEFAULT_MAPPING)
    p.add_argument("--dry-run", action="store_true", help="Report only; do not rewrite templates")
    args = p.parse_args()

    print(f"Templates: {os.path.abspath(args.template_dir)}")
    print(f"Mapping:   {os.path.abspath(args.mapping)}")
    if args.dry_run:
        print("Mode: DRY RUN")
    ok = process(args.template_dir, args.mapping, args.dry_run)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
