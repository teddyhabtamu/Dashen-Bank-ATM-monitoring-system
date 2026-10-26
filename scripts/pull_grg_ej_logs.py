#!/usr/bin/env python3
"""Pull GRG application journals through the Zabbix agent.

GRG writes its journal as ``EJ<yymmdd>.log`` under the ATM application's Log
directory.  The NCR puller cannot fetch it: the name, the encoding (UTF-16LE,
not 8-bit NCR journal text) and the source directory are all different.

This puller deliberately produces the SAME local contract as the NCR puller:

    ej-logs/<YYYY-MM-DD>_<HOST>_EJDATA.log       filebeat / shadow parser
    ej-logs/raw/<YYYY-MM-DD>_<HOST>_EJDATA.log   evidence, never harvested

That means ``scripts/ingest_ej_shadow.py`` and the existing filebeat glob do
not need a second naming convention.  The working copy is decoded to UTF-8 and
line endings are normalised; ``raw/`` keeps the bytes the agent returned.

The source directory is configurable with ``GRG_EJ_DIR`` because GRG
installations move it.  Both two-digit (``EJ261024.log``) and four-digit
(``EJ20261024.log``) year forms are tried, since the NetXMS GRG config uses
``%y`` while a different application build may use ``%Y``.

Usage (on the UAT host):

    python3 scripts/pull_grg_ej_logs.py --days 3
    python3 scripts/pull_grg_ej_logs.py --days 1 --dry-run
    python3 scripts/pull_grg_ej_logs.py --source-dir 'D:\\GRG\\Log'

Requires only python3 and zabbix_get.  The target list comes from the portal,
via scripts/ej_pull_targets.py --vendor GRG.
"""
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import fcntl
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import ej_pull_targets as ept  # noqa: E402

OUTDIR = ROOT / 'ej-logs'
RAWDIR = OUTDIR / 'raw'
LOCKFILE = OUTDIR / 'grg-pull.lock'
DEFAULT_SOURCE_DIR = os.environ.get(
    'GRG_EJ_DIR', r'D:\Program Files\DTATMW\Bin\ATMAPP\Log')
ZBX_GET = os.environ.get('ZBX_GET', 'zabbix_get')
FETCH_TIMEOUT = int(os.environ.get('GRG_EJ_TIMEOUT', '120'))


def candidate_names(day: dt.date):
    """The two naming forms seen in GRG installations, newest first."""
    return [
        f'EJ{day:%y%m%d}.log',
        f'EJ{day:%Y%m%d}.log',
    ]


def local_name(day: dt.date, host: str):
    return f'{day:%Y-%m-%d}_{host}_EJDATA.log'


def decode_text(raw: bytes) -> str:
    """Decode GRG's UTF-16 journal, with an 8-bit fallback for older boxes."""
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        text = raw.decode('utf-16', errors='replace')
    elif b'\x00' in raw[:400]:
        even_nuls = raw[0::2].count(0)
        odd_nuls = raw[1::2].count(0)
        encoding = 'utf-16-le' if even_nuls <= odd_nuls else 'utf-16-be'
        text = raw.decode(encoding, errors='replace')
    else:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
    text = text.replace('\ufeff', '')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def write_append_only(path: Path, data: bytes):
    """Write without truncating a growing journal file.

    filebeat treats a shrinking file as a truncation and re-reads it, which
    duplicates every EJ row.  If the old file is a prefix, append the delta;
    only replace when the source genuinely rewrote the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.exists() else b''
    if old == data:
        return 'unchanged'
    if old and data.startswith(old):
        with path.open('ab') as fh:
            fh.write(data[len(old):])
        return 'appended'
    path.write_bytes(data)
    return 'replaced'


def zabbix_fetch(ip: str, remote: str):
    """Return (raw bytes, None) or (None, reason)."""
    # Quote the parameter: GRG's default directory contains a space.
    key = f'vfs.file.contents["{remote}"]'
    try:
        proc = subprocess.run(
            [ZBX_GET, '-s', ip, '-k', key],
            capture_output=True, timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, (proc.stderr or b'fetch failed').decode(
            'utf-8', 'replace').strip().splitlines()[0]
    raw = proc.stdout
    if not raw:
        return None, 'empty file'
    head = raw[:200].lstrip()
    if head.startswith(b'ZBX_NOTSUPPORTED') or b'Timeout while executing' in head:
        return None, head.decode('utf-8', 'replace').strip()
    return raw, None


def pull_one(atm_id: str, ip: str, host: str, day: dt.date,
             source_dir: str, dry_run: bool = False):
    name = local_name(day, host)
    existing_raw = RAWDIR / name
    # A frozen (closed) day already in evidence is not re-fetched.  The live
    # file is today's; re-fetch it because it is still growing.
    if (day < dt.date.today() and existing_raw.exists()
            and existing_raw.stat().st_size and (OUTDIR / name).exists()):
        return ('skipped', name, 'closed day already held')

    last_reason = ''
    for source_name in candidate_names(day):
        remote = source_dir.rstrip('\\/') + '\\' + source_name
        if dry_run:
            return ('plan', name, f'zabbix_get {ip} {remote}')
        raw, reason = zabbix_fetch(ip, remote)
        if raw is None:
            last_reason = reason or 'not found'
            continue
        text = decode_text(raw)
        if not text.strip():
            last_reason = 'decoded empty'
            continue
        RAWDIR.mkdir(parents=True, exist_ok=True)
        existing_raw.write_bytes(raw)
        result = write_append_only(OUTDIR / name, text.encode('utf-8'))
        return ('ok', name, f'{result}, {len(raw)} bytes from {source_name}')
    return ('absent', name, last_reason or 'no candidate name existed')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--days', type=int, default=3,
                    help='calendar days to consider, including today (default: 3)')
    ap.add_argument('--source-dir', default=DEFAULT_SOURCE_DIR,
                    help='GRG Log directory on the ATM (default: %(default)s)')
    ap.add_argument('--dry-run', action='store_true',
                    help='print what would be fetched; no agent calls or writes')
    args = ap.parse_args(argv)

    if args.days < 1:
        print('--days must be at least 1', file=sys.stderr)
        return 2

    targets, source, _ = ept.resolve(vendor='GRG')
    print(f'grg-ej: {len(targets)} target(s) from {source}', file=sys.stderr)
    if not targets:
        print('grg-ej: no GRG EJ targets. Set vendor=GRG, status=active, '
              'IP address and EJ hostname in ATM Registration.', file=sys.stderr)
        return 1

    OUTDIR.mkdir(parents=True, exist_ok=True)
    RAWDIR.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        lock = LOCKFILE.open('w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('grg-ej: another pull is running; skipped.', file=sys.stderr)
            return 0

    today = dt.datetime.now(dt.timezone.utc).date()
    failed = 0
    for atm_id, ip, host in targets:
        for offset in range(args.days):
            day = today - dt.timedelta(days=offset)
            status, name, detail = pull_one(
                atm_id, ip, host, day, args.source_dir, dry_run=args.dry_run)
            print(f'  {atm_id} {day} {name}: {detail}' if status == 'ok'
                  else f'  {atm_id} {day} {status}: {detail}')
            if status == 'absent' and detail not in (
                    'not found', 'empty file', 'decoded empty') and not detail.startswith(
                    'ZBX_NOTSUPPORTED'):
                failed += 1
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
