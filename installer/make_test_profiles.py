#!/usr/bin/env python3
"""Render deterministic profiles for installer tests and a CI dry run.

The portal is the only production renderer. This script exists so the
installer can be exercised without a database or a browser: it builds the
same documents through `agent_profile.build_profile`, then rewrites the
Windows deployment root to a temporary directory. The rewrite is test-only;
the signature is deliberately left alone because the installer checks its
presence, not the HMAC (the secret never ships on an ATM).

Usage:
    python3 installer/make_test_profiles.py --out /tmp/profiles \
        --monitor-root /tmp/atm-monitor
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

from agent_profile import build_profile, load_manifest  # noqa: E402

SECRET = 'installer-test-secret'

BASE = {
    'atm_name': 'Installer Test ATM',
    'model': 'Test Model',
    'district': 'NAD',
    'city': 'Addis Ababa',
    'ip_address': '10.41.30.10',
    'terminal_id': 'TID006',
    'ej_hostname': 'ATM-INSTALLER-TEST',
    'ej_log_format': 'ncr',
    'image_roots': ['C:\\ATM\\images'],
}

NCR = dict(BASE, atm_id='ATM-INSTALLER-NCR', vendor='NCR')
GRG = dict(BASE, atm_id='ATM-INSTALLER-GRG', vendor='GRG', ej_log_format='grg')
NO_ROOTS = dict(NCR, atm_id='ATM-INST-NOROOTS', image_roots=[])
NCR_NO_ROOTS = dict(NCR, image_roots=[])


def _replace_windows_path(value, old, new):
    new = new.rstrip('/\\')
    for separator in ('\\', '/'):
        value = value.replace(old + separator, new + os.sep)
    return value.replace(old, new)


def _rewrite(value, monitor_root, image_root):
    if isinstance(value, str):
        value = _replace_windows_path(value, 'C:\\monitor', monitor_root)
        value = _replace_windows_path(value, 'C:\\ATM\\images', image_root)
        return value
    if isinstance(value, list):
        return [_rewrite(item, monitor_root, image_root) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, monitor_root, image_root)
                for key, item in value.items()}
    return value


def _build(row, monitor_root, image_root, secret=SECRET):
    profile = build_profile(row, load_manifest(), secret=secret)
    return _rewrite(profile, monitor_root, image_root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, help='directory for the profiles')
    parser.add_argument('--monitor-root', default='C:\\monitor',
                        help='deployment root written into the fixtures')
    parser.add_argument('--unsigned', action='store_true',
                        help='render an unsigned profile for the rejection test')
    args = parser.parse_args(argv)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    monitor_root = args.monitor_root
    image_root = os.path.join(monitor_root, 'samples', 'images')
    pathlib.Path(image_root).mkdir(parents=True, exist_ok=True)

    os.environ['ATM_AGENT_SERVER'] = '10.255.0.10'
    secret = None if args.unsigned else SECRET
    profiles = {
        'ncr.profile.json': _build(NCR, monitor_root, image_root, secret),
        'grg.profile.json': _build(GRG, monitor_root, image_root, secret),
        'noroots.profile.json': _build(NO_ROOTS, monitor_root, image_root, secret),
        'ncr-noroots.profile.json': _build(NCR_NO_ROOTS, monitor_root, image_root, secret),
    }

    # A profile the portal refuses to call ready: no server could be resolved.
    os.environ.pop('ATM_AGENT_SERVER', None)
    unready_row = dict(NCR, district='UNKNOWN-DISTRICT')
    profiles['unready.profile.json'] = _build(
        unready_row, monitor_root, image_root, secret)
    os.environ['ATM_AGENT_SERVER'] = '10.255.0.10'

    for name, profile in profiles.items():
        path = out / name
        path.write_text(json.dumps(profile, indent=2) + '\n', encoding='utf-8')
        print(f'{path}  atm={profile["atm_id"]} ready={profile["ready"]} '
              f'signed={bool(profile.get("signature"))}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
