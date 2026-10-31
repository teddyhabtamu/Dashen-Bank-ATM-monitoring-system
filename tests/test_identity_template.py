"""`atm.identity` must have exactly one template owner.

It used to live on both vendor templates. That works only while no host links
both, and the UAT host did: Zabbix then refused the GRG import with "Cannot
inherit item with key atm.identity ... already inherited". These tests pin the
single-owner design so the failure cannot come back.
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


def test_the_shared_agent_template_ships_the_identity_item():
    assert_identity_item(TEMPLATES / 'template_atm_agent.xml')


def test_vendor_templates_do_not_define_identity():
    """A host may link the NCR and GRG vendor templates (the UAT host did).
    If both define the same key, Zabbix rejects the second template import."""
    for name in ('template_atm_wmi.xml', 'template_grg_xfs.xml'):
        assert 'atm.identity' not in items_by_key(TEMPLATES / name), name


def test_the_shared_generator_keeps_the_item_on_regeneration():
    source = (REPO / 'scripts' / 'generate_agent_template.py').read_text()
    assert 'atm.identity' in source
    assert '"1h"' in source


def test_the_shared_importer_creates_and_links_the_item():
    source = (REPO / 'scripts' / 'import_agent_template_api.py').read_text()
    assert 'def ensure_item' in source
    assert 'def remove_legacy_item' in source
    assert 'def link_all' in source
    assert '--link-all' in source
    assert 'atm.identity' in source


def test_the_vendor_importers_remove_the_legacy_item():
    """A vendor importer must converge, not leave the duplicate behind."""
    for name in ('import_wmi_template_api.py', 'import_grg_xfs_template_api.py'):
        source = (REPO / 'scripts' / name).read_text()
        assert 'def remove_legacy_identity_item' in source, name
        assert 'remove_legacy_identity_item(tid, auth)' in source, name
        assert 'def ensure_identity_item' not in source, name
    grg = (REPO / 'scripts' / 'import_grg_xfs_template_api.py').read_text()
    assert "'--no-link'" in grg, 'template-only import must not link/delete a host'


def test_the_portal_links_the_shared_template_for_both_vendors():
    source = (REPO / 'report-portal' / 'zabbix_sync.py').read_text()
    assert 'TEMPLATE_AGENT_NAME' in source
    assert 'TEMPLATE_AGENT_NAME' in source[source.index('def _templates_for'):]
