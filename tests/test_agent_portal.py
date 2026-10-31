"""The agent surface must be reachable, admin-only, and backed up.

The profile renderer and identity parser are pure modules, but they are useless
if the blueprint is not registered, the template is missing, or the new table is
left out of the backup list. These are the wiring tests for that edge.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
PORTAL = REPO / 'report-portal'
TEMPLATE = (PORTAL / 'templates' / 'agent.html').read_text()
BLUEPRINT = (PORTAL / 'blueprints' / 'agent.py').read_text()
APP = (PORTAL / 'app.py').read_text()
DETAIL = (PORTAL / 'templates' / 'admin_atm_detail.html').read_text()
DOCKERFILE = (PORTAL / 'Dockerfile').read_text()
REGISTRY = (PORTAL / 'agent_registry.py').read_text()


def test_the_blueprint_is_registered_and_its_schema_is_initialised():
    assert 'from blueprints.agent      import bp as agent_bp' in APP
    assert 'app.register_blueprint(agent_bp)' in APP
    assert 'from agent_registry        import init_agent_identity_table' in APP
    assert 'init_agent_identity_table()' in APP


def test_every_agent_route_requires_an_admin():
    routes = re.findall(r"@bp\.route\([^\n]+\)\n(?:@[^\n]+\n)+def ",
                        BLUEPRINT)
    assert routes, 'no routes found in the agent blueprint'
    for block in routes:
        assert '@login_required' in block
        assert '@role_required(ROLE_ADMIN)' in block


def test_the_agent_page_template_exists_and_posts_with_csrf():
    assert TEMPLATE.startswith('{% extends "base.html" %}')
    assert "url_for('agent.agent_roots_save'" in TEMPLATE
    assert "url_for('agent.agent_identity_ingest'" in TEMPLATE
    assert "url_for('agent.agent_profile_download'" in TEMPLATE
    assert TEMPLATE.count('name="csrf_token"') == 2
    assert 'for="roots"' in TEMPLATE
    assert 'for="payload"' in TEMPLATE


def test_the_agent_page_shows_warnings_conflicts_and_signature_state():
    for phrase in ('Desired state', 'Reported identity', 'Registry conflicts',
                   'Profile signature', 'Probe drift', 'Readiness',
                   'Approved image roots'):
        assert phrase in TEMPLATE, phrase


def test_the_atm_detail_page_links_admins_to_the_agent_setup():
    assert "url_for('agent.agent_page', atm_id=atm.atm_id)" in DETAIL
    assert "session.get('role') == 'admin'" in DETAIL


def test_the_agent_cards_keep_their_body_padding():
    """agent.html uses .card-body but has no local rule for it. Without a
    shared default every card body sits flush against the card border, which
    is what the Agent setup page looked like in the browser."""
    assert 'class="card-body"' in TEMPLATE
    for name in ('templates/base.html', 'static/style.css'):
        css = (PORTAL / name).read_text()
        assert re.search(r'\.card-body\s*\{[^}]*padding:\s*20px 24px', css), name


def test_the_portal_image_ships_the_generated_manifest():
    assert 'COPY agent_manifest.json ./' in DOCKERFILE


def test_the_identity_table_is_created_and_backed_up():
    assert 'CREATE TABLE IF NOT EXISTS atm_agent_identity' in REGISTRY
    assert 'ADD COLUMN IF NOT EXISTS image_roots TEXT[]' in REGISTRY
    tables = (REPO / 'config' / 'portal_tables.txt').read_text().split()
    assert 'atm_agent_identity' in tables


def test_the_blueprint_never_writes_identity_into_atm_locations():
    assert 'UPDATE atm_locations' not in BLUEPRINT
    assert 'INSERT INTO atm_locations' not in BLUEPRINT
    assert 'UPDATE atm_locations SET image_roots' in REGISTRY
