"""The automatic identity ingest needs a passive item on both vendor templates.

The portal can only read `atm.identity` if the template that every NCR/GRG
host links actually defines it. These are contract tests: the generators, the
shipped XML, and the API importers must all agree.
"""
from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO / 'config' / 'zabbix'


def items_by_key(path):
    root = ET.parse(path).getroot()
    return {item.findtext('key'): item for item in root.iter('item')}


def assert_identity_item(path):
    item = items_by_key(path).get('atm.identity')
    assert item is not None, f'{path} has no atm.identity item'
    assert item.findtext('type') == 'ZABBIX_AGENT'
    assert item.findtext('delay') == '1h'
    assert item.findtext('history') == '1d'
    assert item.findtext('trends') == '0'
    assert item.findtext('value_type') == 'TEXT'


def test_the_ncr_agent_template_ships_the_identity_item():
    assert_identity_item(TEMPLATES / 'template_atm_wmi.xml')


def test_the_grg_agent_template_ships_the_identity_item():
    assert_identity_item(TEMPLATES / 'template_grg_xfs.xml')


def test_the_generators_keep_the_item_on_regeneration():
    for name in ('generate_wmi_template.py', 'generate_grg_xfs_template.py'):
        source = (REPO / 'scripts' / name).read_text()
        assert 'atm.identity' in source, name
        assert '"1h"' in source, name


def test_the_api_importers_create_the_item():
    for name in ('import_wmi_template_api.py', 'import_grg_xfs_template_api.py'):
        source = (REPO / 'scripts' / name).read_text()
        assert 'def ensure_identity_item' in source, name
        assert 'ensure_identity_item(tid, auth)' in source, name
        assert 'atm.identity' in source, name
    grg = (REPO / 'scripts' / 'import_grg_xfs_template_api.py').read_text()
    assert "'--no-link'" in grg, 'template-only import must not link/delete a host'
