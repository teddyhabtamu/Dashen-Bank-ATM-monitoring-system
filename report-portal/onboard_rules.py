"""Closed vocabularies + pure facts validation for the onboarding wizard.

Stdlib-only on purpose: the wizard's guardrails (unknown vendor/model/
district must STOP, never silently skip) are load-bearing safety logic,
so they live here — unit-tested on every commit — not inside the
Flask-bound blueprint. Vendors/models mirror the template map; districts
mirror scripts/sync_atms_to_zabbix.py DISTRICT_PROXY keys plus names in
use. If these drift apart, tests fail loudly rather than misrouting
quietly (the sync script's silent-skip class, retired by design).
"""
import ipaddress
import re

VENDORS = {
    'NCR': ['SelfServ 34'],
    'GRG': ['AD2600'],
}
DISTRICTS = sorted({
    'NAD', 'SAD', 'EAD', 'WAD', 'HAWASA', 'WOLAITA', 'SOUTH WEST',
    'ADAMA', 'NEKEMTE', 'JIMMA', 'DESSIE', 'DIRE DAWA', 'BAHIR DAR',
    'MEKELLE', 'Kirkos',
})
# TCP-probeable port per vendor path (read-only connect, 3s budget).
# GRG speaks UDP/SNMP — no TCP equivalent exists, so its reachability is
# STATIC until provision (stated on screen, never faked).
PROBE_PORT = {'NCR': 10050}

ATM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')

_FACT_FIELDS = ('atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
                'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
                'ip_address')


def validate_facts(f):
    """Return (cleaned_dict, errors_dict). Pure logic — no I/O."""
    errors = {}
    c = {k: (f.get(k, '') or '').strip() for k in _FACT_FIELDS}
    if not ATM_ID_RE.match(c['atm_id'] or '') or len(c['atm_id']) > 20:
        errors['atm_id'] = ('ATM ID must start alphanumeric, '
                            'letters/numbers/_/- only, max 20.')
    for req in ('branch', 'district', 'city', 'terminal_id', 'ip_address'):
        if not c[req]:
            errors[req] = 'Required.'
    if c['vendor'] not in VENDORS:
        errors['vendor'] = 'Unknown vendor — stops here; needs engineer review.'
    elif c['model'] not in VENDORS.get(c['vendor'], []):
        errors['model'] = (f"Unknown model for {c['vendor']} — stops here; "
                           "needs engineer review (Review Queue).")
    if c['district'] and c['district'] not in DISTRICTS:
        errors['district'] = 'Unknown district — stops here; needs engineer review.'
    if c['ip_address']:
        try:
            ipaddress.ip_address(c['ip_address'])
        except ValueError:
            errors['ip_address'] = 'Not a valid IPv4/IPv6 address.'
    for coord in ('latitude', 'longitude'):
        if c[coord]:
            try:
                float(c[coord])
            except (TypeError, ValueError):
                errors[coord] = 'Must be a number (map picker coming).'
    return c, errors
