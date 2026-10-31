"""ATM identity contract — what a box is allowed to tell the registry.

The identity probe (`atm.identity`) is evidence, never authority. This module
turns a reported JSON document into a fixed shape, rejects values that could
poison the registry (a 300-character TID, a UNC image root, a drive root), and
compares it with the stored row so the portal can show a conflict instead of
silently overwriting it.

Two rules matter more than the field list:

  * Unknown is a valid value. A probe that cannot find the TID reports
    `terminal_source: "unknown"`; nothing is inferred and nothing is written.
  * Every fact carries provenance. "TID006 from the EJ header" and
    "TID006 from a config file" are not equally trustworthy, and the UI must
    be able to say which one it is looking at.

Pure module: no DB, no Flask, no Zabbix. Tests exercise every rule directly.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

IDENTITY_SCHEMA = 1
VENDORS = ('NCR', 'GRG')

# Where a terminal id can come from, best first. `ej-header` is the strongest
# source we can read locally: it is the same value the switch puts in field 41.
TERMINAL_SOURCES = (
    'vendor-config',   # NCR APTRA / GRG application configuration
    'ej-header',       # newest electronic-journal header/receipt
    'registry-file',   # C:\monitor\atm-identity.json written at install
    'manual',          # typed in the portal, echoed by the probe
    'unknown',
)
EJ_SOURCES = ('ej-filename', 'registry-file', 'computer-name', 'unknown')

_TERMINAL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$')
_HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_DRIVE_RE = re.compile(r'^([A-Za-z]):\\(.*)$')
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

CONFIDENCE_BY_SOURCE = {
    'vendor-config': 'high',
    'ej-header': 'high',
    'registry-file': 'high',
    'manual': 'medium',
    'unknown': 'unknown',
}


class IdentityError(ValueError):
    """The document is not a usable identity. Message is safe for the UI."""


def _text(value, field, limit, required=False):
    if value is None:
        if required:
            raise IdentityError(f'{field} is required')
        return None
    if not isinstance(value, str):
        raise IdentityError(f'{field} must be a string')
    value = value.strip()
    if not value:
        if required:
            raise IdentityError(f'{field} is required')
        return None
    if len(value) > limit:
        raise IdentityError(f'{field} is longer than {limit} characters')
    if '\x00' in value or '\r' in value or '\n' in value:
        raise IdentityError(f'{field} contains a control character')
    return value


def _iso(value, field):
    if value is None:
        return None
    text = _text(value, field, 40)
    if text is None:
        return None
    try:
        # Accept a trailing Z: Windows probes emit UTC in that form.
        datetime.fromisoformat(text[:-1] + '+00:00' if text.endswith('Z') else text)
    except ValueError as exc:
        raise IdentityError(f'{field} is not an ISO-8601 timestamp') from exc
    return text


def clean_path(value, field='path'):
    """One absolute Windows directory, or IdentityError.

    Deliberately stricter than the OS: drive-root, UNC, wildcard, ADS and
    traversal are all rejected. These values end up in the image allow-list,
    which must fail closed, so a permissive parser here would defeat it.
    """
    text = _text(value, field, 260, required=True).replace('/', '\\')
    if text.startswith('\\\\'):
        raise IdentityError(f'{field} must not be a UNC path')
    if '*' in text or '?' in text:
        raise IdentityError(f'{field} must not contain a wildcard')
    match = _DRIVE_RE.match(text)
    if not match:
        raise IdentityError(f'{field} must be an absolute drive path (C:\\...)')
    drive, rest = match.group(1).upper(), match.group(2)
    if ':' in rest:
        raise IdentityError(f'{field} must not contain an alternate data stream')
    parts = [p for p in rest.split('\\') if p not in ('', '.')]
    if not parts:
        raise IdentityError(f'{field} must not be a drive root')
    if any(p == '..' for p in parts):
        raise IdentityError(f'{field} must not contain ".."')
    return drive + ':\\' + '\\'.join(parts)


def clean_paths(values, field, limit=10):
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise IdentityError(f'{field} must be a list')
    if len(values) > limit:
        raise IdentityError(f'{field} has more than {limit} entries')
    out, seen = [], set()
    for index, raw in enumerate(values):
        path = clean_path(raw, f'{field}[{index}]')
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _probe_map(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IdentityError('probes must be an object')
    out = {}
    for name, info in value.items():
        name = _text(name, 'probes name', 64, required=True)
        if not isinstance(info, dict):
            raise IdentityError(f'probes.{name} must be an object')
        sha = _text(info.get('sha256'), f'probes.{name}.sha256', 64)
        if sha is not None:
            sha = sha.lower()
            if not _SHA256_RE.match(sha):
                raise IdentityError(f'probes.{name}.sha256 is not a SHA-256')
        out[name] = {
            'version': _text(info.get('version'), f'probes.{name}.version', 40),
            'sha256': sha,
        }
    return out


def normalize(payload):
    """Return the fixed identity shape, or raise IdentityError.

    Unknown top-level keys are dropped rather than stored: a probe update must
    not be able to grow the registry schema by accident. New fields are added
    here, with a schema bump, and the old probe keeps working.
    """
    if not isinstance(payload, dict):
        raise IdentityError('identity must be a JSON object')

    schema = payload.get('schema')
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise IdentityError('schema must be an integer')
    if schema != IDENTITY_SCHEMA:
        raise IdentityError(f'unsupported identity schema {schema}; expected {IDENTITY_SCHEMA}')

    vendor = _text(payload.get('vendor'), 'vendor', 10)
    vendor = vendor.upper() if vendor else None
    if vendor is not None and vendor not in VENDORS:
        raise IdentityError(f'vendor must be one of {", ".join(VENDORS)}')

    terminal_source = _text(payload.get('terminal_source'), 'terminal_source', 40) or 'unknown'
    if terminal_source not in TERMINAL_SOURCES:
        raise IdentityError(f'terminal_source must be one of {", ".join(TERMINAL_SOURCES)}')

    terminal = _text(payload.get('terminal_id'), 'terminal_id', 20)
    if terminal is not None and not _TERMINAL_RE.match(terminal):
        raise IdentityError('terminal_id must be 1-20 characters: letters, digits, dot, dash or underscore')

    confidence = _text(payload.get('terminal_confidence'), 'terminal_confidence', 10)
    if confidence is None:
        confidence = CONFIDENCE_BY_SOURCE.get(terminal_source, 'unknown')
    if confidence not in ('high', 'medium', 'low', 'unknown'):
        raise IdentityError('terminal_confidence must be high, medium, low or unknown')

    ej_raw = payload.get('ej')
    if ej_raw is None:
        ej_raw = {}
    if not isinstance(ej_raw, dict):
        raise IdentityError('ej must be an object')
    ej_hostname = _text(ej_raw.get('hostname'), 'ej.hostname', 64)
    if ej_hostname is not None and not _HOST_RE.match(ej_hostname):
        raise IdentityError('ej.hostname contains characters a journal filename cannot hold')
    ej_source = _text(ej_raw.get('hostname_source'), 'ej.hostname_source', 40) or 'unknown'
    if ej_source not in EJ_SOURCES:
        raise IdentityError(f'ej.hostname_source must be one of {", ".join(EJ_SOURCES)}')

    images_raw = payload.get('images')
    if images_raw is None:
        images_raw = {}
    if not isinstance(images_raw, dict):
        raise IdentityError('images must be an object')
    candidates_raw = images_raw.get('candidates')
    if candidates_raw is None:
        candidates_raw = []
    if not isinstance(candidates_raw, (list, tuple)):
        raise IdentityError('images.candidates must be a list')
    candidates = []
    for index, item in enumerate(candidates_raw):
        if not isinstance(item, dict):
            raise IdentityError(f'images.candidates[{index}] must be an object')
        files = item.get('files')
        if files is not None and (isinstance(files, bool) or not isinstance(files, int) or files < 0):
            raise IdentityError(f'images.candidates[{index}].files must be a non-negative integer')
        candidates.append({
            'path': clean_path(item.get('path'), f'images.candidates[{index}].path'),
            'files': files,
        })

    agent_raw = payload.get('agent')
    if agent_raw is None:
        agent_raw = {}
    if not isinstance(agent_raw, dict):
        raise IdentityError('agent must be an object')

    # Optional since schema 1: an older probe simply omits it, and the fleet
    # report then says "reapply the profile" instead of guessing. Keeping the
    # schema at 1 is deliberate — a bump would reject every box still running
    # the previous probe, which is the opposite of a safe rolling change.
    profile_hash = _text(agent_raw.get('profile_hash'), 'agent.profile_hash', 64)
    if profile_hash is not None:
        profile_hash = profile_hash.lower()
        if not _SHA256_RE.match(profile_hash):
            raise IdentityError('agent.profile_hash is not a SHA-256')
    deploy_hash = _text(agent_raw.get('deploy_hash'), 'agent.deploy_hash', 64)
    if deploy_hash is not None:
        deploy_hash = deploy_hash.lower()
        if not _SHA256_RE.match(deploy_hash):
            raise IdentityError('agent.deploy_hash is not a SHA-256')

    return {
        'schema': IDENTITY_SCHEMA,
        'collected_at': _iso(payload.get('collected_at'), 'collected_at'),
        'computer': _text(payload.get('computer'), 'computer', 64),
        'vendor': vendor,
        'vendor_source': _text(payload.get('vendor_source'), 'vendor_source', 64) or 'unknown',
        'model': _text(payload.get('model'), 'model', 50),
        'serial': _text(payload.get('serial'), 'serial', 64),
        'terminal_id': terminal,
        'terminal_source': terminal_source,
        'terminal_confidence': confidence,
        'ej': {
            'hostname': ej_hostname,
            'hostname_source': ej_source,
            'roots': clean_paths(ej_raw.get('roots'), 'ej.roots'),
            'newest_file': _text(ej_raw.get('newest_file'), 'ej.newest_file', 128),
            'format': _text(ej_raw.get('format'), 'ej.format', 32),
        },
        'images': {
            'roots': clean_paths(images_raw.get('roots'), 'images.roots'),
            'candidates': candidates,
        },
        'ip_addresses': _text_list(payload.get('ip_addresses'), 'ip_addresses', 16),
        'probes': _probe_map(payload.get('probes')),
        'agent': {
            'version': _text(agent_raw.get('version'), 'agent.version', 32),
            'config_hash': _text(agent_raw.get('config_hash'), 'agent.config_hash', 64),
            'profile_hash': profile_hash,
            'deploy_hash': deploy_hash,
            'applied_at': _iso(agent_raw.get('applied_at'), 'agent.applied_at'),
        },
    }


def _text_list(values, field, limit):
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise IdentityError(f'{field} must be a list')
    if len(values) > limit:
        raise IdentityError(f'{field} has more than {limit} entries')
    out = []
    for index, value in enumerate(values):
        text = _text(value, f'{field}[{index}]', 64, required=True)
        if text not in out:
            out.append(text)
    return out


def parse(raw):
    """Accept the JSON string a Zabbix item returns, or an already-decoded dict."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise IdentityError('identity is not UTF-8') from exc
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise IdentityError(f'identity is not valid JSON: {exc}') from exc
    return normalize(raw)


def _same(a, b):
    return (a or '').strip().upper() == (b or '').strip().upper()


def conflicts(identity, row):
    """Differences between what the box reports and what the registry stores.

    Severity is about consequence, not surprise:
      high   — identity used for financial reconciliation (TID, vendor)
      medium — journal/file naming (EJ hostname)
      low    — descriptive or network facts that legitimately drift

    A field the probe could not determine never produces a conflict. Silence is
    not disagreement.
    """
    out = []

    def add(field, registered, reported, severity, note):
        if reported is None or (isinstance(reported, str) and not reported.strip()):
            return
        if _same(registered, reported):
            return
        out.append({'field': field, 'registered': registered, 'reported': reported,
                    'severity': severity, 'note': note})

    add('vendor', row.get('vendor'), identity.get('vendor'), 'high',
        'A vendor mismatch means the wrong probe set and template are attached.')
    add('terminal_id', row.get('terminal_id'), identity.get('terminal_id'), 'high',
        'The terminal id maps ISO 8583 field 41 to transactions; do not auto-heal it.')
    add('ej_hostname', row.get('ej_hostname'), (identity.get('ej') or {}).get('hostname'),
        'medium', 'The journal filename tag decides which EJ files the puller expects.')
    add('model', row.get('model'), identity.get('model'), 'low',
        'Descriptive only; the hardware template does not depend on it.')

    addresses = identity.get('ip_addresses') or []
    registered_ip = (row.get('ip_address') or '').strip()
    if registered_ip and addresses and registered_ip not in addresses:
        out.append({'field': 'ip_address', 'registered': registered_ip,
                    'reported': ', '.join(addresses), 'severity': 'low',
                    'note': 'A NAT or a second NIC can explain this; confirm before changing the registry.'})
    return out


def readiness(identity, approved_roots=None):
    """What the box can actually do, given what it reported.

    `images_unapproved` is the important field: a box that reports a root the
    registry never approved must not have it written into the allow-list. The
    installer stays fail-closed; the portal shows the proposal for review.
    """
    ej = identity.get('ej') or {}
    images = identity.get('images') or {}
    observed = images.get('roots') or []
    approved = {p.lower() for p in (approved_roots or [])}
    unapproved = [p for p in observed if p.lower() not in approved]
    return {
        'ej_ready': bool(ej.get('hostname') and ej.get('roots')),
        'images_ready': bool(observed),
        'images_unapproved': unapproved,
        'probes_reported': bool(identity.get('probes')),
    }


def probe_drift(identity, expected):
    """Compare reported probe hashes with the manifest the profile was built from.

    A hash mismatch is the only signal that distinguishes a stale copy on the
    ATM from a code bug — the exact failure that used to cost an hour of
    debugging. Missing probes are reported, not ignored.
    """
    reported = identity.get('probes') or {}
    missing, hash_mismatch, version_mismatch, unexpected = [], [], [], []

    for name, want in (expected or {}).items():
        got = reported.get(name)
        if not got:
            missing.append(name)
            continue
        want_hash = (want or {}).get('sha256')
        got_hash = got.get('sha256')
        # A missing hash is not a match. The probe should always be able to
        # hash a file it just read; if it cannot, the copy is not verified.
        if want_hash and got_hash != want_hash:
            hash_mismatch.append(name)
        want_version = (want or {}).get('version')
        got_version = got.get('version')
        if want_version and got_version and want_version != got_version:
            version_mismatch.append(name)

    for name in reported:
        if name not in (expected or {}):
            unexpected.append(name)

    return {
        'ok': not missing and not hash_mismatch,
        'missing': sorted(missing),
        'hash_mismatch': sorted(hash_mismatch),
        'version_mismatch': sorted(version_mismatch),
        'unexpected': sorted(unexpected),
    }
