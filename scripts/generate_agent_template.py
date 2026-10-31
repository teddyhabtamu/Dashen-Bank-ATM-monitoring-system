#!/usr/bin/env python3
"""Generate config/zabbix/template_atm_agent.xml.

One template owns the common agent item every ATM links exactly once. Vendor
templates own vendor telemetry; this one owns identity. Putting `atm.identity`
on both vendor templates made Zabbix reject the GRG import as soon as a host
linked both (duplicate inherited key), which is a data-dependent failure that
should not exist.

The XML is intentionally small. The API importer is the operational path
because Zabbix 6.4's configuration.import is picky about generated XML; the
XML exists so the template can also be imported by hand in the UI.
"""
from __future__ import annotations

import pathlib
import uuid
import xml.etree.ElementTree as ET
from xml.dom import minidom

TEMPLATE_NAME = "Dashen Bank ATM Agent"
TEMPLATE_GROUP = "Templates"
IDENTITY_KEY = "atm.identity"

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "atm.agent.dashenbank.et")


def _uid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


def _el(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def build_template():
    root = ET.Element("zabbix_export")
    _el(root, "version", "6.4")

    groups = _el(root, "template_groups")
    group = _el(groups, "template_group")
    _el(group, "uuid", _uid("group:templates"))
    _el(group, "name", TEMPLATE_GROUP)

    templates = _el(root, "templates")
    template = _el(templates, "template")
    _el(template, "uuid", _uid("template:" + TEMPLATE_NAME))
    _el(template, "template", TEMPLATE_NAME)
    _el(template, "name", TEMPLATE_NAME)
    template_groups = _el(template, "groups")
    tg = _el(template_groups, "group")
    _el(tg, "name", TEMPLATE_GROUP)

    items = _el(template, "items")
    item = _el(items, "item")
    _el(item, "uuid", _uid("item:" + IDENTITY_KEY))
    _el(item, "name", "ATM identity — JSON evidence")
    _el(item, "type", "ZABBIX_AGENT")
    _el(item, "key", IDENTITY_KEY)
    _el(item, "delay", "1h")
    _el(item, "history", "1d")
    _el(item, "trends", "0")
    _el(item, "value_type", "TEXT")
    _el(item, "description",
        "Read-only JSON from C:\\monitor\\atm-identity.ps1. The portal ingests "
        "it automatically; it is evidence, never authority.")

    xml = minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(indent="  ")
    return "\n".join(line for line in xml.splitlines() if line.strip()) + "\n"


def main():
    out = pathlib.Path("config/zabbix/template_atm_agent.xml")
    out.write_text(build_template(), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
