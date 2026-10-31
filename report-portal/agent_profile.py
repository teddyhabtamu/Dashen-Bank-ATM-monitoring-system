"""Per-ATM agent profile — the desired state the installer applies.

One profile per ATM, rendered from the registry row and a manifest of the real
probe files. It carries the exact file contents the installer writes, so the
installer never re-implements policy: it is a writer, not a second source of
truth.

What is deliberately absent:
  * SNMP community, PSK and any other secret. The profile is downloadable and
    must be safe to leave in a change ticket.
  * Anything the box can decide for itself (which drive, which vendor). Those
    are reported back by `atm.identity` and compared, never baked in blind.

Signing is HMAC-SHA256 with `ATM_PROFILE_SECRET`. An unsigned profile is
allowed only when the secret is unset (UAT); `ready` is still true, but
`signature` is null and the page says so.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from atm_identity import IdentityError, clean_paths

PROFILE_SCHEMA = 1
AGENT_SERIES = '6.4'
AGENT_TIMEOUT = 30
AGENT_PORT = 10050
AGENT_INCLUDE = 'C:\\monitor\\atm-agent.conf'
ROOTS_FILE = 'C:\\monitor\\atm-image-roots.txt'

EXEC = 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File'

# Deployable probes per vendor. Order matters only for readability.
PROBES_BY_VENDOR = {
    'NCR': ('atm-wmi-collector.ps1', 'ncr-xfs-probe.ps1'),
    'GRG': ('grg-xfs-probe.ps1',),
}
IMAGE_PROBE = 'atm-image-chunk.ps1'
IDENTITY_PROBE = 'atm-identity.ps1'

# District -> Zabbix proxy, mirrored from scripts/sync_atms_to_zabbix.py.
# A test parses that file and fails if the two ever disagree; duplicate here
# because the portal container has no scripts/ on its import path.
DISTRICT_PROXY = {
    'NAD': 'zabbix-proxy-addis-north',
    'SAD': 'zabbix-proxy-addis-south',
    'EAD': 'zabbix-proxy-addis-east',
    'WAD': 'zabbix-proxy-addis-west',
    'HAWASA': 'zabbix-proxy-hawassa',
    'WOLAITA': 'zabbix-proxy-hawassa',
    'SOUTH WEST': 'zabbix-proxy-hawassa',
    'ADAMA': 'zabbix-proxy-adama',
    'NEKEMTE': 'zabbix-proxy-adama',
    'JIMMA': 'zabbix-proxy-adama',
    'DESSIE': 'zabbix-proxy-dessie',
    'DIRE DAWA': 'zabbix-proxy-dessie',
    'BAHIR DAR': 'zabbix-proxy-bahirdar',
    'MEKELLE': 'zabbix-proxy-bahirdar',
}

_ATM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$')
MANIFEST_PATH = Path(__file__).resolve().parent / 'agent_manifest.json'


class ProfileError(ValueError):
    """The row cannot produce a safe profile. Message is safe for the UI."""


def load_manifest(path=None):
    path = Path(path) if path else MANIFEST_PATH
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ProfileError(
            'agent manifest is missing — run scripts/build_agent_manifest.py'
        ) from exc
    except OSError as exc:
        raise ProfileError('agent manifest is unreadable') from exc
    except ValueError as exc:
        raise ProfileError(f'agent manifest is not valid JSON: {exc}') from exc
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ProfileError('agent manifest has an unsupported schema')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ProfileError('agent manifest contains no files')
    return manifest


def resolve_server(district):
    """Where this agent should accept connections from.

    An explicit env wins so a lab/UAT box can point at the host without
    inventing a proxy. Otherwise the district proxy is the only correct answer
    for passive checks: the proxy is the peer that opens TCP 10050.
    """
    env = (os.environ.get('ATM_AGENT_SERVER') or '').strip()
    if env:
        return env, 'env:ATM_AGENT_SERVER'
    proxy = DISTRICT_PROXY.get((district or '').strip().upper())
    if proxy:
        return proxy, 'district-proxy'
    return None, 'unset'


def _probe_entry(manifest, name):
    entry = (manifest.get('files') or {}).get(name)
    if not entry:
        raise ProfileError(f'probe {name} is not in the agent manifest')
    return {
        'name': name,
        'kind': entry.get('kind'),
        'dest': entry.get('dest') or ('C:\\monitor\\' + name),
        'sha256': entry.get('sha256'),
        'version': entry.get('version'),
    }


def _user_parameters(vendor, probes, image_roots):
    """The include file body. One place decides which key maps to which script.

    The singular `atm.part[*]` spelling is load-bearing: `atm.parts[*]` is
    treated by Zabbix as a duplicate of `atm.parts` and the agent refuses to
    start. That bug is why this is code and not a copy-paste template.
    """
    lines = []
    if vendor == 'NCR':
        lines += [
            'UserParameter=atm.parts,' + EXEC + ' "C:\\monitor\\atm-wmi-collector.ps1"',
            'UserParameter=atm.part[*],' + EXEC + ' "C:\\monitor\\atm-wmi-collector.ps1" -Part "$1"',
            'UserParameter=atm.xfs.cdm,' + EXEC + ' "C:\\monitor\\ncr-xfs-probe.ps1"',
        ]
    elif vendor == 'GRG':
        lines.append(
            'UserParameter=atm.xfs.cdm,' + EXEC + ' "C:\\monitor\\grg-xfs-probe.ps1" -Json')
    else:
        raise ProfileError(f'no user parameters for vendor {vendor!r}')

    if IDENTITY_PROBE in probes:
        lines.append(
            'UserParameter=atm.identity,' + EXEC + ' "C:\\monitor\\atm-identity.ps1"')

    if image_roots:
        lines += [
            'UserParameter=atm.image.info[*],' + EXEC + ' "C:\\monitor\\atm-image-chunk.ps1" -PathB64 "$1" -Info',
            'UserParameter=atm.image.chunk[*],' + EXEC + ' "C:\\monitor\\atm-image-chunk.ps1" -PathB64 "$1" -Offset "$2" -Length "$3"',
            'UserParameter=atm.image.dir[*],' + EXEC + ' "C:\\monitor\\atm-image-chunk.ps1" -DirPathB64 "$1" -DirList -DirOffset "$2" -DirLimit "$3"',
            'UserParameter=atm.image.ziplist[*],' + EXEC + ' "C:\\monitor\\atm-image-chunk.ps1" -ZipPathB64 "$1" -List -ListOffset "$2" -ListLimit "$3"',
            'UserParameter=atm.image.zipentry[*],' + EXEC + ' "C:\\monitor\\atm-image-chunk.ps1" -ZipPathB64 "$1" -EntryIndex "$2" -Offset "$3" -Length "$4"',
        ]
    return lines


def render_include(profile):
    vendor = profile['vendor']
    header = [
        '# Generated by the Dashen ATM portal. Do not edit by hand.',
        f'# ATM: {profile["atm_id"]}   vendor: {vendor}',
        '',
    ]
    return '\n'.join(header + profile['user_parameters']) + '\n'


def render_agent_conf(profile):
    """A complete Agent 2 config, not an include.

    We own the agent, so there is no bank config to preserve. `ServerActive`
    is omitted on purpose: this fleet is passive-only, and an empty active
    server is the difference between "no outbound rule needed" and a firewall
    ticket for TCP 10051.
    """
    agent = profile['agent']
    lines = [
        '# Generated by the Dashen ATM portal. Do not edit by hand.',
        f'# ATM: {profile["atm_id"]}',
        '',
        '# Passive checks only: the Zabbix server/proxy connects to this agent.',
        f'Server={agent["server"] or "UNSET"}',
        f'Hostname={profile["hostname"]}',
        f'ListenPort={agent["listen_port"]}',
        f'Timeout={agent["timeout"]}',
        '',
        '# Agent 2 has no EnableRemoteCommands switch: system.run is denied',
        '# explicitly and only the rendered UserParameters are available.',
        'DenyKey=system.run[*]',
        '',
        '# Keep the agent log beside the probes so a technician can read it',
        '# without guessing where the MSI put its default log file.',
        'LogType=file',
        'LogFile=C:\\monitor\\logs\\zabbix_agent2.log',
        'DebugLevel=3',
        '',
        '# UserParameters live in their own file so a probe update does not',
        '# require rewriting this config.',
        f'Include={AGENT_INCLUDE}',
        '',
        '# TLS: enable only with a bank-approved PSK. Left off so the profile',
        '# stays usable in the UAT lab without pretending a secret exists.',
    ]
    return '\n'.join(lines) + '\n'


def render_roots(profile):
    if not profile['image_roots']:
        return None
    lines = [
        '# Generated by the Dashen ATM portal from approved registry roots.',
        f'# ATM: {profile["atm_id"]}',
        '# The chunk probe fails closed if this file is missing or invalid.',
    ]
    return '\n'.join(lines + profile['image_roots']) + '\n'


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def sign(profile, secret):
    if not secret:
        return None
    body = {k: v for k, v in profile.items() if k != 'signature'}
    digest = hmac.new(secret.encode('utf-8'), _canonical(body), hashlib.sha256).hexdigest()
    return 'hmac-sha256:' + digest


def verify(profile, secret):
    """True only for a well-formed signature over the exact profile body."""
    if not secret:
        return False
    signature = profile.get('signature')
    if not isinstance(signature, str) or not signature.startswith('hmac-sha256:'):
        return False
    expected = sign({k: v for k, v in profile.items() if k != 'signature'}, secret)
    return hmac.compare_digest(signature, expected or '')


def build_profile(row, manifest, secret=None):
    """Render one ATM's profile. Raises ProfileError; never a half-profile.

    Deliberately has no render timestamp: the profile must be byte-identical
    across requests so the hash a technician pins with `/EXPECTEDHASH=` still
    matches the file downloaded later. When it was applied is recorded in the
    installer state and the audit log, not in the desired-state document.
    """
    atm_id = (row.get('atm_id') or '').strip()
    if not _ATM_ID_RE.match(atm_id):
        raise ProfileError(f'ATM id {atm_id!r} cannot be a Zabbix host name')

    vendor = (row.get('vendor') or '').strip().upper()
    if vendor not in PROBES_BY_VENDOR:
        raise ProfileError(f'vendor {vendor or "?"} has no agent profile — needs review')

    try:
        image_roots = clean_paths(row.get('image_roots') or [], 'image_roots')
    except IdentityError as exc:
        raise ProfileError(f'approved image roots are invalid: {exc}') from exc

    manifest_files = manifest.get('files') or {}
    probes = [_probe_entry(manifest, name) for name in PROBES_BY_VENDOR[vendor]]
    if IDENTITY_PROBE in manifest_files:
        probes.append(_probe_entry(manifest, IDENTITY_PROBE))
    if image_roots:
        if IMAGE_PROBE not in manifest_files:
            raise ProfileError(f'{IMAGE_PROBE} is not in the agent manifest')
        probes.append(_probe_entry(manifest, IMAGE_PROBE))

    server, server_source = resolve_server(row.get('district'))
    warnings = []
    if not server:
        warnings.append('Agent server is not configured: set ATM_AGENT_SERVER or a district proxy.')
    if not (row.get('terminal_id') or '').strip():
        warnings.append('Terminal id is not registered; the probe can report it for confirmation.')
    if not (row.get('ej_hostname') or '').strip():
        warnings.append('EJ hostname is not registered; the EJ pullers skip this ATM until it is.')
    if not image_roots:
        warnings.append('No approved image roots; image keys are omitted and the roots file will not be written.')
    if IDENTITY_PROBE not in manifest_files:
        warnings.append('atm.identity probe is not in the manifest yet; identity checks stay unavailable.')

    profile = {
        'schema': PROFILE_SCHEMA,
        'atm_id': atm_id,
        'hostname': atm_id,
        'vendor': vendor,
        'ready': bool(server and probes),
        'warnings': warnings,
        'agent': {
            'series': AGENT_SERIES,
            'server': server,
            'server_source': server_source,
            'listen_port': AGENT_PORT,
            'timeout': AGENT_TIMEOUT,
            'passive_only': True,
            'config_text': '',
        },
        'probes': probes,
        'user_parameters': [],
        'image_roots': image_roots,
        'ej': {
            'hostname': (row.get('ej_hostname') or '').strip() or None,
            'hostname_source': 'registry',
            'format': (row.get('ej_log_format') or '').strip() or None,
            'roots': [],
        },
        'deploy_hash': '',
        'profile_hash': '',
        'signature': None,
    }
    profile['user_parameters'] = _user_parameters(vendor, {p['name'] for p in probes}, image_roots)
    profile['agent']['config_text'] = render_agent_conf(profile)
    profile['files'] = [
        {'dest': AGENT_INCLUDE, 'content': render_include(profile)},
    ]
    roots_text = render_roots(profile)
    if roots_text:
        profile['files'].append({'dest': ROOTS_FILE, 'content': roots_text})

    # The deploy hash covers exactly what the installer writes to the box:
    # the agent config text, the probe files and their hashes, the include and
    # the roots file. It deliberately excludes informational registry fields
    # (EJ hostname, city, warnings). Identity seeds the EJ hostname after the
    # first apply; hashing that field would force a pointless second reapply
    # on every new ATM.
    deployable = {
        'schema': PROFILE_SCHEMA,
        'atm_id': atm_id,
        'hostname': atm_id,
        'vendor': vendor,
        'agent': {
            'series': AGENT_SERIES,
            'server': server,
            'listen_port': AGENT_PORT,
            'timeout': AGENT_TIMEOUT,
            'passive_only': True,
            'config_text': profile['agent']['config_text'],
        },
        'probes': probes,
        'user_parameters': profile['user_parameters'],
        'files': profile['files'],
        'image_roots': image_roots,
    }
    profile['deploy_hash'] = hashlib.sha256(_canonical(deployable)).hexdigest()

    body = {k: v for k, v in profile.items() if k not in ('profile_hash', 'signature')}
    profile['profile_hash'] = hashlib.sha256(_canonical(body)).hexdigest()
    profile['signature'] = sign(profile, secret)
    return profile
