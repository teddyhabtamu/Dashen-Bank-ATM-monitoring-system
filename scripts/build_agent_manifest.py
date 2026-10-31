#!/usr/bin/env python3
"""Build report-portal/config/agent_manifest.json — the deployable file contract.

Why this exists: the portal container has no copy of scripts/ (the Dockerfile
copies only the portal), so it cannot hash the probe files at request time.
The manifest is generated here, from the real files, and committed. A test
regenerates it and fails on any difference, so a probe edit cannot ship
without the profile that describes it changing too.

The output is deterministic on purpose: no timestamp, sorted keys. That is
what makes `test_agent_manifest.py` a byte-for-byte drift check instead of a
reminder that someone forgot to regenerate.

Usage:
    python3 scripts/build_agent_manifest.py           # write the manifest
    python3 scripts/build_agent_manifest.py --check   # exit 1 if stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Next to the portal code, not under report-portal/config: that directory is
# root-owned in the UAT checkout and the portal image only copies *.py, so a
# root-level file is the one path that works both bind-mounted and baked in.
OUT = REPO / 'report-portal' / 'agent_manifest.json'

# Files the installer deploys to C:\\monitor. Only add a file here once the
# installer knows where it goes; a hash with no destination is not a contract.
DEPLOYABLES = (
    ('scripts/atm-wmi-collector.ps1', 'collector'),
    ('scripts/ncr-xfs-probe.ps1', 'xfs'),
    ('scripts/grg-xfs-probe.ps1', 'xfs'),
    ('scripts/atm-image-chunk.ps1', 'image'),
    ('scripts/atm-identity.ps1', 'identity'),
)

_VERSION_RE = re.compile(r"probe_version\s*=\s*'([^']+)'")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def build() -> dict:
    files = {}
    for rel, kind in DEPLOYABLES:
        path = REPO / rel
        if not path.exists():
            # Not an error: a deployable may be staged before it ships. The
            # profile simply omits it until it exists, which is the correct
            # behaviour for an installer that must not reference a missing
            # file.
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        m = _VERSION_RE.search(text)
        files[path.name] = {
            'source': rel,
            'kind': kind,
            'sha256': _sha256(path),
            'version': m.group(1) if m else None,
            'dest': 'C:\\monitor\\' + path.name,
        }
    return {'schema': 1, 'files': files}


def render(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='verify the committed manifest is current')
    args = parser.parse_args(argv)

    rendered = render(build())
    current = OUT.read_text(encoding='utf-8') if OUT.exists() else ''
    if args.check:
        if rendered != current:
            print(f'agent manifest is stale: run {Path(__file__).name}', file=sys.stderr)
            return 1
        print('agent manifest is current')
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding='utf-8')
    print(f'wrote {OUT.relative_to(REPO)} ({len(json.loads(rendered)["files"])} files)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
