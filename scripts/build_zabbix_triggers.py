#!/usr/bin/env python3
"""Render the authoritative trigger set into the ATM SNMP templates.

Reads config/zabbix/triggers.json (thresholds, severities, descriptions) and
config/status_map.json (vendor status/fault codes), then rewrites the
<triggers> of template_ncr_snmp.xml / template_grg_snmp.xml.

Zabbix export layout notes (do not "simplify"):
- Triggers live INSIDE their item (<templates><template><items><item>
  <triggers>), one block per item. The single exception (multi-item
  expressions such as the txn failure rate) lives in the root-level
  <triggers> block — the builder moves such defs with their `key` item, so
  the root block is always left empty.
- A definition applies to a template only if its item key exists there, so
  vendor differences (GRG has no cam2/shutter/journal/power.main/cassette4/
  intrusion/vibration) resolve automatically — no per-vendor maintenance.
- Existing trigger UUIDs are preserved by trigger name (stable re-imports,
  no duplicates). New triggers get fresh UUIDs; reruns are stable because
  generated UUIDs are read back from the file.
- Idempotent and remap-safe: triggers reference item KEYS, never OIDs, so
  run this any time — before or after generate_zabbix_templates.py.

Usage:
    python3 scripts/build_zabbix_triggers.py            # rewrite both templates
    python3 scripts/build_zabbix_triggers.py --dry-run  # report only
"""
import argparse
import json
import os
import sys
import uuid
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE_DIR = os.path.join(HERE, "..", "config", "zabbix")
DEFAULT_DEFS = os.path.join(HERE, "..", "config", "zabbix", "triggers.json")
STATUS_MAP = os.path.normpath(os.path.join(HERE, "..", "config", "status_map.json"))

TEMPLATES = {
    "NCR": "template_ncr_snmp.xml",
    "GRG": "template_grg_snmp.xml",
}


def load_json(path):
    with open(path) as f:
        data = json.load(f)
    return data


def resolve_value(spec, vendor, smap):
    if isinstance(spec, dict) and "map" in spec:
        key = spec["map"]
        return smap.get(vendor, {}).get(key, smap.get("default", {}).get(key))
    return spec


def build_expression(tpl_name, d, vendor, smap):
    func, key = d["func"], d["key"]
    op = d["op"]
    val = resolve_value(d["value"], vendor, smap)
    if isinstance(val, dict) and "expr" in val:
        rhs = val["expr"].replace("{T}", tpl_name)
    else:
        rhs = str(val)
    if func == "last":
        return f"last(/{tpl_name}/{key}){op}{rhs}"
    param = d.get("func_param", "")
    return f"{func}(/{tpl_name}/{key},{param}){op}{rhs}"


def tag_el(tag, value):
    el = ET.Element("tag")
    t = ET.SubElement(el, "tag")
    t.text = tag
    v = ET.SubElement(el, "value")
    v.text = value
    return el


def trigger_el(d, tpl_name, vendor, smap, uid):
    tr = ET.Element("trigger")
    for tag_name, text in (
        ("uuid", uid),
        ("expression", build_expression(tpl_name, d, vendor, smap)),
        ("name", d["name"]),
        ("priority", d["priority"]),
    ):
        el = ET.SubElement(tr, tag_name)
        el.text = text
    if d.get("description"):
        el = ET.SubElement(tr, "description")
        el.text = d["description"]
    tags = ET.SubElement(tr, "tags")
    tags.append(tag_el("component", d.get("component", "system")))
    tags.append(tag_el("scope", "atm"))
    return tr


def process(template_dir, defs_path, dry_run):
    defs = [d for d in load_json(defs_path) if isinstance(d, dict) and "key" in d]
    with open(STATUS_MAP) as f:
        smap = json.load(f)
    ok = True
    for vendor, fname in TEMPLATES.items():
        path = os.path.join(template_dir, fname)
        if not os.path.exists(path):
            print(f"  SKIP {vendor}: {path} not found")
            continue
        tree = ET.parse(path)
        root = tree.getroot()
        tpl_el = root.find("./templates/template")
        if tpl_el is None:
            print(f"  SKIP {vendor}: no template element in {fname}")
            ok = False
            continue
        tpl_name = tpl_el.findtext("name").strip()
        # uuid stability: map every existing trigger name -> uuid
        old_uuids = {}
        for tr in root.iter("trigger"):
            n = (tr.findtext("name") or "").strip()
            u = (tr.findtext("uuid") or "").strip()
            if n and u:
                old_uuids[n] = u
        # item key -> item element
        items = {}
        for item in tpl_el.findall("./items/item"):
            k = (item.findtext("key") or "").strip()
            if k:
                items[k] = item
        # group defs by key, preserving file order
        by_key = {}
        for d in defs:
            by_key.setdefault(d["key"], []).append(d)
        emitted, skipped = 0, []
        for key, dlist in by_key.items():
            item = items.get(key)
            if item is None:
                skipped.extend(dd["name"] for dd in dlist)
                continue
            tr_block = item.find("triggers")
            if tr_block is None:
                tr_block = ET.SubElement(item, "triggers")
            else:
                for child in list(tr_block):
                    tr_block.remove(child)
            for d in dlist:
                uid = old_uuids.get(d["name"]) or uuid.uuid4().hex
                tr_block.append(trigger_el(d, tpl_name, vendor, smap, uid))
                emitted += 1
        # root-level block (multi-item defs used to live here): always empty —
        # every def now attaches to its key item, so leftovers are stale.
        stale = []
        for tr_block in root.findall("./triggers"):
            for tr in list(tr_block):
                stale.append((tr.findtext("name") or "").strip())
                tr_block.remove(tr)
        # warn about items whose triggers were cleared but got no defs
        orphaned = []
        for key, item in items.items():
            tr_block = item.find("triggers")
            if tr_block is not None and len(list(tr_block)) == 0 and key not in by_key:
                orphaned.append(key)
        print(f"  {vendor} ({fname}): {emitted} trigger(s) rendered, "
              f"{len(skipped)} def(s) skipped (key absent)")
        if stale:
            print(f"      removed {len(stale)} stale root-level trigger(s): {stale}")
        if orphaned:
            print(f"      WARN items left triggerless: {orphaned}")
        verify = sorted({d["name"] for key, dl in by_key.items()
                         if key in items for d in dl if d.get("verify_at_walk")})
        if verify:
            print(f"      verify-against-walk ({len(verify)}):")
            for n in verify:
                print(f"        - {n}")
        if not dry_run:
            ET.indent(tree, space="    ")
            tree.write(path, encoding="UTF-8", xml_declaration=True)
            print("      written")
    if dry_run:
        print("  (dry-run: no files written)")
    return ok


def main():
    p = argparse.ArgumentParser(description="Render trigger set into ATM SNMP templates")
    p.add_argument("--template-dir", default=DEFAULT_TEMPLATE_DIR)
    p.add_argument("--defs", default=DEFAULT_DEFS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(f"Templates: {os.path.abspath(args.template_dir)}")
    print(f"Defs:      {os.path.abspath(args.defs)}")
    if not process(args.template_dir, args.defs, args.dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()
