#!/usr/bin/env python3
"""Generate config/zabbix/template_atm_wmi.xml — Zabbix agent items for the
WMI part-state collector (scripts/atm-wmi-collector.ps1).

One master item (atm.parts, full JSON) + JSONPath dependents per part:
part_status for every part, replenishment_status for replenishable ones.
No VALUE triggers: PartStatus/ReplenishmentStatus code semantics are not yet
calibrated against observed states — alerting on unknown codes would page
on healthy ATMs, and even disabled guessed triggers are banned (they become
pages the day anyone enables template triggers wholesale). Value triggers
come after RDP fault drills prove the mappings (see runbook). The single
allowed trigger — master nodata(10m), semantics-free collector health — is
ensured by import_wmi_template_api.py, not here.

Part list = live union from ATM-TEST-NCR 2026-10-01. Rerun to regenerate;
UUIDs are deterministic (uuid5), so re-imports update instead of duplicating.
"""
import uuid
import xml.etree.ElementTree as ET
from xml.dom import minidom

TEMPLATE_NAME = "Dashen Bank ATM WMI Parts"
# Must be a pre-existing group: API-created groups come back unreferenceable
# (proven on UAT against both nested and flat names). Cosmetic only.
TEMPLATE_GROUP = "Templates"

# (part name, watch_replenishment)
# Proven vendor legend (NCR_DevDevicePart qualifiers, TESTMACHINE 2026-10-09):
#   PartStatus: 0 OK, 1 Warning, 2 Error, 3 Not Configured, 4 Unknown, 5 Accessed
#   PMStatus: 0 OK, 1 PM Required
#   ReplenishmentStatus ValueMap {0,1,2,3} = OK, Warning, Error, Unknown
#   (provider Description text wrongly says "4 - Unknown" — trust ValueMap)
# Duplicate names are physical pairs sharing one WMI Name (Control Board x2,
# Transport x2, differing DeviceInstance) — the collector binds the bare name
# to the lowest (DeviceInstance,PartId) and suffixes the rest " (2)", so the
# template must carry the same suffixed keys or they go unmonitored.
PARTS = [
    ("Capture Bin", False), ("Card Capture Bin", False), ("Card Reader", False),
    ("Carriage", False), ("Control Board", False), ("Control Board (2)", False),
    ("Divert Area", False),
    ("Electronics", False), ("Exit Shutter", False), ("FDKs", False),
    ("First Cassette", True), ("First Pick Module", False),
    ("Fourth Cassette", True), ("Fourth Pick Module", False), ("Knife", False),
    ("Mechanism", False), ("Media Aligner", False), ("Paper", True),
    ("PinPad", False), ("Presenter", False), ("Print Head", False),
    ("Purge Bin", True), ("Read/Write Magnetic Head", False),
    ("Reject Area", True), ("Retract Area", True),
    ("Second Cassette", True), ("Second Pick Module", False),
    ("Single Note Transport", False), ("Smart Card Reader", False),
    ("Suction Cups", False), ("Third Cassette", True),
    ("Third Pick Module", False), ("Transport", False), ("Transport (2)", False),
    ("Vacuum System", False),
]

# Device union from TESTMACHINE 2026-10-12 (NCR_Device, 14 instances;
# DeviceStatus 0 OK, 1 Warning, 2 Unavailable). Names are model-canonical
# but fleet variance is expected across models — names absent on a host are
# simply never emitted, and their items sit dataless (grey, never red).
# Per-device discovery (LLD) is the fleet-scale follow-up, not this list.
DEVICES = [
    "ALRM-01-USBAlarms", "ALT_-01-USBAdvertLight", "CASH-01-UsbMediaDispenser",
    "FLT_-01-USBFasciaLight", "KEYB-01-PcQwertyKeyboardPS2", "KEYB-01-USBEPP2",
    "MCRW-01-USBMotorised", "MDSW-01-MODESWITCH", "MEEI-01-USBMediaEntryExitInd",
    "MISC-01-USBMiscInterface", "RPNT-01-UsbThermal", "RRLY-01-USBRemoteRelay",
    "RSI_-01-USBRemoteStatusInd", "SND_-01-PcSound",
]

NS = "http://www.w3.org/2001/XMLSchema-instance"


def _uid(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          "dashen-atm-wmi:" + "|".join(parts)))


def _el(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _dependent(items_el, part, field, label):
    item = ET.SubElement(items_el, "item")
    _el(item, "uuid", _uid("item", part, field))
    _el(item, "name", f"ATM Part — {part} {label}")
    _el(item, "type", "DEPENDENT")
    _el(item, "key", f"atm.part.{field}[{part}]")
    _el(item, "delay", "0")
    _el(item, "history", "7d")
    _el(item, "trends", "0")
    _el(item, "value_type", "UNSIGNED")
    master = ET.SubElement(item, "master_item")
    _el(master, "key", "atm.parts")
    pre = ET.SubElement(item, "preprocessing")
    step = ET.SubElement(pre, "step")
    _el(step, "type", "JSONPATH")
    # Single-quote bracket notation — proven live 2026-10-09: double-quoted
    # brackets die in Zabbix's parser with "invalid control character" on
    # spaced part names, single quotes extract fine (Capture Bin Status live).
    # NOTE: live template currently runs JavaScript preprocessing (see
    # import_wmi_template_api.js_for) pending post-observation convergence
    # back to this native form — do not "fix" this back to double quotes.
    _el(step, "params", f"$['parts']['{part}']['{field}']")
    return item


def main():
    zbx = ET.Element("zabbix_export")
    ET.SubElement(zbx, "version").text = "6.4"
    # NOTE: no <date> element — Zabbix 6.4's importer rejects it
    # (Invalid tag "/zabbix_export": unexpected tag "date").
    groups = ET.SubElement(zbx, "template_groups")
    g = ET.SubElement(groups, "template_group")
    _el(g, "uuid", _uid("group"))
    _el(g, "name", TEMPLATE_GROUP)
    templates = ET.SubElement(zbx, "templates")
    tpl = ET.SubElement(templates, "template")
    _el(tpl, "uuid", _uid("template"))
    _el(tpl, "template", TEMPLATE_NAME)
    _el(tpl, "name", TEMPLATE_NAME)
    grp = ET.SubElement(tpl, "groups")
    gg = ET.SubElement(grp, "group")
    _el(gg, "name", TEMPLATE_GROUP)
    items_el = ET.SubElement(tpl, "items")
    master = ET.SubElement(items_el, "item")
    _el(master, "uuid", _uid("item", "master"))
    _el(master, "name", "ATM Parts — full WMI tree")
    _el(master, "type", "ZABBIX_AGENT")
    _el(master, "key", "atm.parts")
    _el(master, "delay", "1m")
    _el(master, "history", "7d")
    _el(master, "trends", "0")
    _el(master, "value_type", "TEXT")

    # `atm.identity` deliberately does NOT live here. It is owned by the
    # shared "Dashen Bank ATM Agent" template: a host that links both this
    # WMI template and the GRG XFS template would otherwise inherit the same
    # key twice, which Zabbix rejects at template-import time.
    n_dep = 0
    for part, repl in PARTS:
        _dependent(items_el, part, "part_status", "Status")
        n_dep += 1
        if repl:
            _dependent(items_el, part, "replenishment_status", "Replenishment")
            n_dep += 1
    for dev in DEVICES:
        # Device dependents address the devices tree; reference-only (live
        # runs JavaScript via the API importer — see import script).
        item = ET.SubElement(items_el, "item")
        _el(item, "uuid", _uid("item", "dev", dev))
        _el(item, "name", f"ATM Device — {dev} Status")
        _el(item, "type", "DEPENDENT")
        _el(item, "key", f"atm.device.status[{dev}]")
        _el(item, "delay", "0")
        _el(item, "history", "7d")
        _el(item, "trends", "0")
        _el(item, "value_type", "UNSIGNED")
        master = ET.SubElement(item, "master_item")
        _el(master, "key", "atm.parts")
        pre = ET.SubElement(item, "preprocessing")
        step = ET.SubElement(pre, "step")
        _el(step, "type", "JSONPATH")
        _el(step, "params", f"$['devices']['{dev}']['device_status']")
        n_dep += 1
    xml = minidom.parseString(ET.tostring(zbx, encoding="unicode")).toprettyxml(indent="  ")
    with open("config/zabbix/template_atm_wmi.xml", "w") as f:
        f.write(xml)
    print(f"Wrote template_atm_wmi.xml: 1 master + {n_dep} dependents")


if __name__ == "__main__":
    main()
