#!/usr/bin/env python3
"""Generate config/zabbix/template_grg_xfs.xml — Zabbix agent items for GRG XFS
Cash Dispenser & Cassette telemetry (scripts/grg-xfs-probe.ps1).

Master item (atm.xfs.cdm, compact JSON) + JavaScript preprocessing dependents:
  - CDM Status: device_status, safe_door, dispenser, intermediate_stacker
  - CDM Summary: total_amount, total_notes, reject_count, retract_count, has_low, has_empty
  - Cassettes: count, status, reject for each physical & logical cash unit

Cassettes based on live discovery from TESTGRG:
  - Rej01 (Reject Cassette)
  - Rct01 (Retract Cassette)
  - 0050A (ETB 50 Dispense Cassette)
  - 0100A (ETB 100 Dispense Cassette A)
  - 0100B (ETB 100 Dispense Cassette B)
  - 0200A (ETB 200 Dispense Cassette)

UUIDs are deterministic (uuid5) so re-imports cleanly update without duplicates.
"""
import uuid
import xml.etree.ElementTree as ET
from xml.dom import minidom

TEMPLATE_NAME = "Dashen Bank ATM Hardware - GRG XFS"
TEMPLATE_GROUP = "Templates"

# Deterministic namespace for GRG XFS
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "grg.xfs.dashenbank.et")


def make_uuid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


CASSETTES = [
    ("Rej01", "Reject Cassette 1", "reject", 0, "ETB"),
    ("Rct01", "Retract Cassette 2", "retract", 0, "ETB"),
    ("0050A", "Dispense Cassette 3 (ETB 50)", "dispense", 50, "ETB"),
    ("0100A", "Dispense Cassette 4 (ETB 100)", "dispense", 100, "ETB"),
    ("0100B", "Dispense Cassette 5 (ETB 100)", "dispense", 100, "ETB"),
    ("0200A", "Dispense Cassette 6 (ETB 200)", "dispense", 200, "ETB"),
]


def _el(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = str(text)
    return e


def build_template():
    root = ET.Element("zabbix_export")
    _el(root, "version", "6.4")

    # Template groups
    t_groups = _el(root, "template_groups")
    tg = _el(t_groups, "template_group")
    _el(tg, "uuid", make_uuid(f"group:{TEMPLATE_GROUP}"))
    _el(tg, "name", TEMPLATE_GROUP)

    # Templates
    templates = _el(root, "templates")
    t = _el(templates, "template")
    _el(t, "uuid", make_uuid(f"template:{TEMPLATE_NAME}"))
    _el(t, "template", TEMPLATE_NAME)
    _el(t, "name", TEMPLATE_NAME)
    _el(t, "description",
        "Dashen Bank GRG ATM hardware monitoring via 32-bit CEN/XFS 3.0 API. "
        "Collects Cash Dispenser (CDM) status and cash inventory plus Card Reader, "
        "Printer, PIN Pad and SIU sensor status through isolated XFS calls.")

    groups = _el(t, "groups")
    g = _el(groups, "group")
    _el(g, "name", TEMPLATE_GROUP)

    items = _el(t, "items")

    # Master Item
    master_key = "atm.xfs.cdm"
    m = _el(items, "item")
    _el(m, "uuid", make_uuid(f"item:{master_key}"))
    _el(m, "name", "GRG XFS — full status & cassette inventory")
    _el(m, "type", "ZABBIX_AGENT")
    _el(m, "key", master_key)
    _el(m, "delay", "1m")
    _el(m, "history", "7d")
    _el(m, "trends", "0")
    _el(m, "value_type", "TEXT")
    _el(m, "description",
        "Raw single-line JSON payload from C:\\monitor\\grg-xfs-probe.ps1 -Json querying XFS msxfs.dll")

    # Helper for adding dependent items
    def add_dep(name, key, js_expr, val_type="UNSIGNED", units=None, valuemap=None, desc=""):
        it = _el(items, "item")
        _el(it, "uuid", make_uuid(f"item:{key}"))
        _el(it, "name", name)
        _el(it, "type", "DEPENDENT")
        _el(it, "key", key)
        _el(it, "delay", "0")
        _el(it, "history", "7d")
        _el(it, "trends", "30d" if val_type in ("UNSIGNED", "FLOAT") else "0")
        _el(it, "value_type", val_type)
        if units:
            _el(it, "units", units)
        if desc:
            _el(it, "description", desc)
        if valuemap:
            vm = _el(it, "valuemap")
            _el(vm, "name", valuemap)
        mi = _el(it, "master_item")
        _el(mi, "key", master_key)
        prep = _el(it, "preprocessing")
        step = _el(prep, "step")
        _el(step, "type", "JAVASCRIPT")
        _el(step, "params", f"var j = JSON.parse(value);\nreturn {js_expr};")

    # 1. CDM Device & Subsystem Statuses
    add_dep("CDM — Device Status", "atm.xfs.cdm.device_status",
            "j.cdm.status.device", valuemap="XFS Device Status",
            desc="CEN/XFS fwDevice: 0=Online, 1=Offline, 4=HwError")

    add_dep("CDM — Safe Door Status", "atm.xfs.cdm.safe_door",
            "j.cdm.status.safe_door", valuemap="XFS Safe Door Status",
            desc="CEN/XFS fwSafeDoor: 2=Door Open, 3=Door Closed, 4=Door Locked")

    add_dep("CDM — Dispenser State", "atm.xfs.cdm.dispenser",
            "j.cdm.status.dispenser", valuemap="XFS Dispenser Status",
            desc="CEN/XFS fwDispenser: 0=OK, 2=Stop")

    add_dep("CDM — Intermediate Stacker", "atm.xfs.cdm.stacker",
            "j.cdm.status.intermediate_stacker",
            desc="CEN/XFS fwIntermediateStacker: 0=Empty, 1=NotEmpty")

    # 2. CDM Summaries
    add_dep("CDM Summary — Total Dispensable Cash", "atm.xfs.cdm.total_amount",
            "j.cdm.summary.total_amount", val_type="FLOAT", units="ETB",
            desc="Total currency value in all active dispense cassettes")

    add_dep("CDM Summary — Total Notes", "atm.xfs.cdm.total_notes",
            "j.cdm.summary.total_notes",
            desc="Total banknote count across all dispense cassettes")

    add_dep("CDM Summary — Reject Notes Count", "atm.xfs.cdm.reject_count",
            "j.cdm.summary.reject_count",
            desc="Current rejected note count in reject bin")

    add_dep("CDM Summary — Retract Notes Count", "atm.xfs.cdm.retract_count",
            "j.cdm.summary.retract_count",
            desc="Current retracted note count in retract bin")

    add_dep("CDM Summary — Has Low Cassette", "atm.xfs.cdm.has_low",
            "j.cdm.summary.has_low",
            desc="1 if any active dispense cassette is below its minimum threshold, else 0")

    add_dep("CDM Summary — Has Empty Cassette", "atm.xfs.cdm.has_empty",
            "j.cdm.summary.has_empty",
            desc="1 if any active dispense cassette count is 0, else 0")

    # 3. Per-Cassette Items
    for cid, cname, ctype, denom, cur in CASSETTES:
        add_dep(f"Cassette {cid} — {cname} Count",
                f"atm.xfs.cdm.cassette.count[{cid}]",
                f'j.cdm.cassettes["{cid}"] ? j.cdm.cassettes["{cid}"].count : 0',
                desc=f"Banknote count in {cname}")

        add_dep(f"Cassette {cid} — {cname} Status",
                f"atm.xfs.cdm.cassette.status[{cid}]",
                f'j.cdm.cassettes["{cid}"] ? j.cdm.cassettes["{cid}"].status : 5',
                valuemap="XFS Cassette Status",
                desc=f"Operational status for {cname}")

        if ctype == "dispense":
            add_dep(f"Cassette {cid} — {cname} Reject Count",
                    f"atm.xfs.cdm.cassette.reject[{cid}]",
                    f'j.cdm.cassettes["{cid}"] ? j.cdm.cassettes["{cid}"].reject_count : 0',
                    desc=f"Reject count associated with {cname}")

    # 4. Card Reader (IDC)
    add_dep("Card Reader — Device Status", "atm.xfs.idc.device_status",
            "(j.idc && j.idc.device !== undefined) ? j.idc.device : 255",
            valuemap="XFS Device Status", desc="IDC fwDevice: 0=Online, 1=Offline, 4=HwError")
    add_dep("Card Reader — Media Status", "atm.xfs.idc.media_status",
            "(j.idc && j.idc.media !== undefined) ? j.idc.media : 255",
            valuemap="XFS IDC Media Status", desc="Card present / jammed / empty")
    add_dep("Card Reader — Retained Cards Bin", "atm.xfs.idc.retain_bin",
            "(j.idc && j.idc.retain_bin !== undefined) ? j.idc.retain_bin : 255",
            valuemap="XFS IDC Retain Bin", desc="Retain bin status")
    add_dep("Card Reader — Retained Cards Count", "atm.xfs.idc.cards",
            "(j.idc && j.idc.cards !== undefined) ? j.idc.cards : 0",
            desc="Captured / retained card count")

    # 5. Receipt Printer (PTR)
    add_dep("Printer — Device Status", "atm.xfs.ptr.device_status",
            "(j.ptr && j.ptr.device !== undefined) ? j.ptr.device : 255",
            valuemap="XFS Device Status", desc="PTR fwDevice: 0=Online, 1=Offline, 4=HwError")
    add_dep("Printer — Paper Status", "atm.xfs.ptr.paper_status",
            "(j.ptr && j.ptr.paper !== undefined) ? j.ptr.paper : 255",
            valuemap="XFS PTR Paper Status", desc="Paper full / low / out")

    # 6. PIN Pad (EPP)
    add_dep("PIN Pad — Device Status", "atm.xfs.pin.device_status",
            "(j.pin && j.pin.device !== undefined) ? j.pin.device : 255",
            valuemap="XFS Device Status", desc="PIN fwDevice: 0=Online, 1=Offline, 4=HwError")

    # 7. SIU Sensors & Enclosures
    add_dep("SIU — Cabinet Door Status", "atm.xfs.siu.cabinet_door",
            "(j.siu && j.siu.cabinet_door !== undefined) ? j.siu.cabinet_door : 255",
            valuemap="XFS Safe Door Status", desc="Upper fascia / operator door: 2=Open, 3=Closed")
    add_dep("SIU — Safe Door Status", "atm.xfs.siu.safe_door",
            "(j.siu && j.siu.safe_door !== undefined) ? j.siu.safe_door : 255",
            valuemap="XFS Safe Door Status", desc="Lower safe / vault door: 2=Open, 3=Closed")
    add_dep("SIU — Supervisor Mode Switch", "atm.xfs.siu.supervisor_mode",
            "(j.siu && j.siu.supervisor_switch !== undefined) ? j.siu.supervisor_switch : 255",
            desc="0=Normal Run, 1=Supervisor / Maintenance Mode")
    add_dep("SIU — Tamper Alarm Sensor", "atm.xfs.siu.tamper",
            "(j.siu && j.siu.tamper_sensor !== undefined) ? j.siu.tamper_sensor : 255",
            desc="0=OK, 1=Tamper Alarm Active")

    # Triggers
    triggers = _el(t, "triggers")

    def add_trig(name, expr, priority="HIGH", desc=""):
        tr = _el(triggers, "trigger")
        _el(tr, "uuid", make_uuid(f"trigger:{name}"))
        _el(tr, "expression", expr)
        _el(tr, "name", name)
        _el(tr, "priority", priority)
        if desc:
            _el(tr, "description", desc)

    add_trig("CDM — Cash Dispenser Hardware Fault on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>255",
             "HIGH", "The cash dispenser hardware is reporting an error or offline state (Busy is excluded).")

    add_trig("CDM — Safe Door Open on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.safe_door)=2",
             "HIGH", "The ATM safe / vault door is currently open.")

    add_trig("CDM — Dispenser Stopped on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.dispenser)=2",
             "HIGH", "The cash dispenser mechanism is halted / in stop state.")

    add_trig("CDM — Out of Cash on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.total_amount)=0",
             "DISASTER", "All cash cassettes are empty; ATM cannot dispense cash.")

    add_trig("CDM — Total Cash Low (< 50,000 ETB) on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.total_amount)<50000 and last(/{TEMPLATE_NAME}/atm.xfs.cdm.total_amount)>0",
             "AVERAGE", "Dispensable cash on the ATM is below 50,000 ETB threshold. Replenishment recommended.")

    add_trig("CDM — Reject Cassette Nearly Full on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.reject_count)>=250",
             "AVERAGE", "Reject cassette Rej01 has 250 or more rejected bills (max capacity ~300).")

    for cid, cname, ctype, denom, cur in CASSETTES:
        if ctype == "dispense":
            add_trig(f"CDM — Cassette {cid} ({denom} {cur}) Empty on {{HOST.NAME}}",
                     f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.cassette.count[{cid}])=0",
                     "WARNING", f"{cname} note count has reached 0.")

    add_trig("Card Reader — Hardware Error on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.idc.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.idc.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.idc.device_status)<>255",
             "HIGH", "The card reader hardware is reporting a failure (Busy is excluded).")

    add_trig("Card Reader — Card Jammed on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.idc.media_status)=3",
             "HIGH", "A customer card is jammed inside the card reader.")

    add_trig("Printer — Hardware Error on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.ptr.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.ptr.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.ptr.device_status)<>255",
             "HIGH", "The XFS printer is reporting an offline or hardware error state (Busy is excluded).")

    add_trig("Printer — Paper Out on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.ptr.paper_status)=2",
             "AVERAGE", "Printer is out of paper. Receipts cannot be printed.")

    add_trig("Printer — Paper Low on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.ptr.paper_status)=1",
             "WARNING", "Printer paper roll is low.")

    add_trig("PIN Pad — Hardware Error on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.pin.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.pin.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.pin.device_status)<>255",
             "HIGH", "PIN Pad / EPP module is reporting a failure (Busy is excluded).")

    add_trig("SIU — Cabinet / Fascia Door Open on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.siu.cabinet_door)=2",
             "AVERAGE", "The upper cabinet / fascia service door is open.")

    add_trig("SIU — Supervisor / Maintenance Mode on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.siu.supervisor_mode)=1",
             "WARNING", "ATM has been switched into Supervisor / Maintenance mode.")

    add_trig("SIU — Tamper Alarm on {HOST.NAME}",
             f"last(/{TEMPLATE_NAME}/atm.xfs.siu.tamper)=1",
             "DISASTER", "Security tamper sensor triggered on the ATM!")

    # Value Maps
    valuemaps = _el(t, "valuemaps")

    def add_vm(vm_name, mappings):
        vm = _el(valuemaps, "valuemap")
        _el(vm, "uuid", make_uuid(f"valuemap:{vm_name}"))
        _el(vm, "name", vm_name)
        maps = _el(vm, "mappings")
        for val, text in mappings:
            m_el = _el(maps, "mapping")
            _el(m_el, "value", str(val))
            _el(m_el, "newvalue", text)

    add_vm("XFS Device Status", [
        (0, "Online"),
        (1, "Offline"),
        (2, "Power Off"),
        (3, "No Device"),
        (4, "Hardware Error"),
        (5, "User Error"),
        (6, "Busy"),
        (7, "Fraud Attempt"),
        (255, "Not queried"),
    ])

    add_vm("XFS Safe Door Status", [
        (1, "Door Not Supported"),
        (2, "Door Open"),
        (3, "Door Closed"),
        (4, "Door Locked"),
        (5, "Door Unknown"),
        (255, "Not queried"),
    ])

    add_vm("XFS Dispenser Status", [
        (0, "OK"),
        (1, "State Unknown"),
        (2, "Stop"),
        (3, "Unknown"),
    ])

    add_vm("XFS Cassette Status", [
        (0, "OK"),
        (1, "Full"),
        (2, "High"),
        (3, "Low"),
        (4, "Empty"),
        (5, "Inoperative"),
        (6, "Missing"),
        (7, "No Values Available"),
        (8, "No Reference Value"),
    ])

    add_vm("XFS IDC Media Status", [
        (1, "Media Present"),
        (2, "Media Not Present"),
        (3, "Media Jammed"),
        (4, "Not Supported"),
        (5, "Unknown"),
        (255, "Not queried"),
    ])

    add_vm("XFS IDC Retain Bin", [
        (1, "OK"),
        (2, "Full"),
        (3, "High"),
        (4, "Not Supported"),
        (255, "Not queried"),
    ])

    add_vm("XFS PTR Paper Status", [
        (0, "Full / OK"),
        (1, "Paper Low"),
        (2, "Paper Out"),
        (3, "Not Supported"),
        (4, "Unknown"),
        (255, "Not queried"),
    ])

    # Pretty print XML
    xml_str = ET.tostring(root, encoding="utf-8")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="    ")


if __name__ == "__main__":
    out_path = "config/zabbix/template_grg_xfs.xml"
    content = build_template()
    # Strip empty lines from minidom
    cleaned = "\n".join([line for line in content.splitlines() if line.strip()])
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(cleaned + "\n")
    print(f"Generated {out_path}")
