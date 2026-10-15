"""
blueprints/onboarding.py — guided ATM onboarding wizard (PREVIEW).

Status: Screens 1 (facts) + 2 (pre-flight) only, ADMIN role only, ZERO
writes. Provisioning, new RBAC roles, and credential escrow are explicitly
NOT in this file (see docs/onboarding-wizard-spec.md) — nothing here can
create, modify, or credential anything. Safe for tester clicks today.

Screen 1 collects facts with inline validation. Screen 2 runs pre-flight:
each check reports LIVE (actually probed) or STATIC (verified at provision
time) — never implying more certainty than it has.
"""
import logging
import socket

from flask import Blueprint, render_template, request
from blueprints.auth import login_required, role_required, ROLE_ADMIN
from db import get_db
from onboard_rules import VENDORS, DISTRICTS, PROBE_PORT, validate_facts

logger = logging.getLogger(__name__)

bp = Blueprint('onboarding', __name__, url_prefix='/admin')


def _tcp_probe(ip, port, timeout=3):
    """True if TCP connects, False otherwise. Read-only network probe."""
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _preflight(cleaned):
    """Run ordered checks. Each result: (label, ok: True/False/None,
    detail, fixer). ok=None means STATIC (verified later, stated)."""
    out = []
    ip = cleaned['ip_address']
    vendor = cleaned['vendor']
    # 1. DB uniqueness (LIVE).
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT atm_id FROM atm_locations WHERE atm_id = %s OR ip_address = %s",
                        (cleaned['atm_id'], ip))
            clash = cur.fetchone()
            cur.close()
        if clash:
            out.append(('Not already registered', False,
                        f'Conflicts with existing row {clash[0]}.',
                        'You fix: pick an unused ATM ID / IP.'))
        else:
            out.append(('Not already registered', True,
                        'ATM ID and IP are unused.', '—'))
    except Exception as e:
        logger.warning('onboard uniqueness check failed: %s', e)
        out.append(('Not already registered', None,
                    'Database check unavailable right now.',
                    'Engineer: verify DB reachability.'))
    # 2. Reachability (LIVE where possible).
    if vendor in PROBE_PORT:
        port = PROBE_PORT[vendor]
        if _tcp_probe(ip, port):
            out.append((f'Reachable on TCP/{port}', True,
                        f'{ip}:{port} answered.', '—'))
        else:
            out.append((f'Reachable on TCP/{port}', False,
                        f'{ip}:{port} silent.',
                        'Network team: forward the vendor port (see onboarding doc Step 0).'))
    else:
        out.append(('Reachability', None,
                    'GRG speaks UDP/SNMP — no TCP equivalent; verified live at provision.',
                    'Not a failure. Continue.'))
    # 3. Model known (STATIC by construction — closed dropdown).
    out.append(('Model known', True,
                f"{cleaned['vendor']} {cleaned['model']} has templates.",
                '—'))
    # 4. Credential reference (STATIC in preview — escrow lands with RBAC).
    out.append(('Credential reference', None,
                'Preview build: credentials resolve at provision time.',
                'Admin: pick the sealed escrow entry when provisioning.'))
    # 5. Journal preview (STATIC — pattern stated).
    out.append(('Journal path preview', None,
                'Journals pull hourly per ATM after onboarding.',
                'Nothing to do now.'))
    return out


@bp.route('/onboard', methods=['GET', 'POST'])
@login_required
@role_required(ROLE_ADMIN)
def onboard():
    """Screen 1 (GET/POST facts) → Screen 2 (pre-flight results)."""
    if request.method == 'GET':
        return render_template('onboarding.html',
                               vendors=VENDORS, districts=DISTRICTS,
                               values={}, errors={}, checks=None)
    values = {k: (request.form.get(k, '') or '') for k in
              ('atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
               'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
               'ip_address')}
    cleaned, errors = validate_facts(values)
    if errors:
        return render_template('onboarding.html',
                               vendors=VENDORS, districts=DISTRICTS,
                               values=values, errors=errors, checks=None)
    checks = _preflight(cleaned)
    return render_template('onboarding.html',
                           vendors=VENDORS, districts=DISTRICTS,
                           values=cleaned, errors={}, checks=checks)
