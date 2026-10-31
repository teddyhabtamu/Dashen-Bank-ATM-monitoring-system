"""The ATM team's identifier is the terminal ID, not our internal atm_id.

These are source-level checks because the ATM list needs a live DB to render.
They pin the contract that matters to operators: the terminal ID is the first
thing they see, the first thing they can search, and the sort key. The internal
atm_id stays visible as the secondary monitoring/Zabbix key.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / 'report-portal' / 'templates'
ADMIN = REPO / 'report-portal' / 'blueprints' / 'admin.py'


def test_atm_list_leads_with_terminal_id():
    html = (TEMPLATES / 'admin_atm.html').read_text()
    header = html.split('<thead>', 1)[1].split('</thead>', 1)[0]

    assert '<th>Terminal ID</th>' in header
    assert '<th>ATM ID</th>' not in header, (
        'the list must not lead with the internal monitoring key')
    assert "{{ d.terminal_id or '—' }}" in html
    assert 'ATM ID: {{ d.atm_id }}' in html, (
        'the internal key must stay visible for support and Zabbix lookups')


def test_atm_list_searches_and_sorts_by_terminal_id():
    src = ADMIN.read_text()
    assert 'l.terminal_id ILIKE %s' in src, (
        'the ATM team searches by terminal ID')
    assert "ORDER BY COALESCE(NULLIF(l.terminal_id, ''), l.atm_id), l.atm_id" in src, (
        'the list should read in terminal-ID order, not internal-ID order')


def test_detail_page_shows_terminal_id_before_the_system_key():
    html = (TEMPLATES / 'admin_atm_detail.html').read_text()
    assert 'TID {{ atm.terminal_id' in html
    info = html.split('ATM Information', 1)[1]
    assert info.index('Terminal ID') < info.index('ATM ID (system)')


def test_import_preview_and_template_lead_with_terminal_id():
    html = (TEMPLATES / 'admin_atm.html').read_text()
    assert "var COLS = ['terminal_id','atm_id'" in html
    assert "var LABELS = ['Terminal ID','ATM ID'" in html

    src = ADMIN.read_text()
    assert "w.writerow(['terminal_id', 'atm_id'" in src, (
        'the downloadable import template must show the terminal ID first')


def test_fleet_page_leads_with_terminal_id():
    html = (TEMPLATES / 'fleet.html').read_text()
    assert '{{ r.terminal_id or r.atm_id }}' in html
    assert 'ATM ID {{ r.atm_id }}' in html


def test_onboarding_form_asks_for_terminal_id_first():
    html = (TEMPLATES / 'onboarding.html').read_text()
    assert html.index("fld('terminal_id'") < html.index("fld('atm_id'"), (
        'the wizard should ask for the bank identifier before the internal key')
