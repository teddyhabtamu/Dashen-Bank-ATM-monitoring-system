#!/usr/bin/env python3
"""Find ATM image candidates for one EJ session, on demand.

This is the discovery half of the admin image viewer.  It lists one
allow-listed directory through the Zabbix agent, parses the vendor filename,
and ranks files by capture time and event sequence.  It does **not** fetch
image bytes, write a file, or store metadata.

GRG:

    python3 scripts/find_atm_image.py --host 10.41.30.115 --vendor GRG \
      --start '2026-10-22 15:34:44' --end '2026-10-22 15:35:21'

NCR (day container only until the zip entry format is confirmed):

    python3 scripts/find_atm_image.py --host 10.41.30.114 --vendor NCR \
      --start '2026-10-22 00:00:00' --end '2026-10-22 23:59:59'

The output is JSON.  Raw ATM paths and filenames are masked by default; add
``--show-sensitive`` only for local diagnosis and do not paste that output
into a ticket.  A missing day folder is reported as ``available: false`` with
``Not available on ATM``, the same state the viewer must show.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from typing import Sequence

from atm_image_link import (
    DEFAULT_PRE_WINDOW,
    DEFAULT_POST_WINDOW,
    LinkError,
    rank_grg_candidates,
    select_ncr_zips,
)
from fetch_atm_image import (
    DEFAULT_RETRIES,
    DEFAULT_ROOTS,
    DEFAULT_TIMEOUT,
    ImageFetchError,
    list_dir,
    mask_sensitive,
)

GRG_ROOT = r'E:\pic'
NCR_ZIP_ROOT = r'C:\ISUITE\ICAM\Permit\iCamImageZip'


def grg_directory(day: dt.date, image_root: str = GRG_ROOT) -> str:
    """Return the GRG day folder for one local date.

    ``image_root`` is supplied by the bridge allow-list; the CLI keeps the
    confirmed ``E:\\pic`` default.
    """
    return '{}\\{}\\{:02d}\\{:02d}'.format(
        image_root.rstrip('\\/'), day.year, day.month, day.day)


def _parse_when(value: str) -> dt.datetime:
    text = value.strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f',
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M'):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f'expected YYYY-MM-DD HH:MM:SS, got {value!r}')


def _redact(result: dict) -> dict:
    """Remove raw ATM paths and filenames from human-facing output."""
    redacted = dict(result)
    redacted.pop('directory', None)
    if isinstance(redacted.get('candidates'), list):
        redacted['candidates'] = [
            {key: value for key, value in item.items() if key != 'name'}
            for item in redacted['candidates']
        ]
    return redacted


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Find ATM image candidates for one EJ session.')
    parser.add_argument('--host', required=True,
                        help='ATM address reachable by zabbix_get')
    parser.add_argument('--vendor', required=True, type=str.upper,
                        choices=('GRG', 'NCR'),
                        help='ATM vendor (decides the filename rule)')
    parser.add_argument('--start', required=True, type=_parse_when,
                        help='session start, local ATM time')
    parser.add_argument('--end', required=True, type=_parse_when,
                        help='session end, local ATM time (same day)')
    parser.add_argument('--dir', default=None,
                        help='override the allow-listed directory to list')
    parser.add_argument('--pre-window', type=float,
                        default=DEFAULT_PRE_WINDOW,
                        help=f'seconds before start to include '
                             f'(default: {DEFAULT_PRE_WINDOW:g})')
    parser.add_argument('--post-window', type=float,
                        default=DEFAULT_POST_WINDOW,
                        help=f'seconds after end to include '
                             f'(default: {DEFAULT_POST_WINDOW:g})')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help='zabbix_get timeout in seconds (default: 30, max: 30)')
    parser.add_argument('--retries', type=int, default=DEFAULT_RETRIES,
                        help='retries for timeouts/empty responses '
                             f'(default: {DEFAULT_RETRIES}, max: 5)')
    parser.add_argument('--root', action='append', default=None,
                        help='add a host-side allowed root (the ATM roots '
                             'file must also allow it; repeatable)')
    parser.add_argument('--zbx-get', default=None,
                        help='zabbix_get binary (default: $ZBX_GET or zabbix_get)')
    parser.add_argument('--show-sensitive', action='store_true',
                        help='print raw ATM paths/filenames (default: masked)')
    parser.add_argument('--verbose', action='store_true')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.end < args.start:
        parser.error('--end must not be before --start')
    if args.start.date() != args.end.date():
        parser.error('--end must be on the same local day as --start')
    if args.pre_window < 0 or args.post_window < 0:
        parser.error('--pre-window and --post-window must be >= 0')
    if args.timeout < 1 or args.timeout > 30:
        parser.error('--timeout must be between 1 and 30')
    if args.retries < 0 or args.retries > 5:
        parser.error('--retries must be between 0 and 5')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format='%(levelname)s %(message)s',
    )

    roots = tuple(DEFAULT_ROOTS) + tuple(args.root or ())
    base = {
        'kind': 'atm-image-candidates',
        'vendor': args.vendor,
        'host': args.host,
        'session_start': args.start.isoformat(sep=' '),
        'session_end': args.end.isoformat(sep=' '),
    }

    try:
        if args.vendor == 'GRG':
            directory = args.dir or grg_directory(args.start.date())
            rows = list_dir(
                args.host, directory, timeout=args.timeout, roots=roots,
                command=args.zbx_get, retries=args.retries,
            )
            result = rank_grg_candidates(
                rows, args.start, args.end,
                pre_window=args.pre_window,
                post_window=args.post_window,
            )
        else:
            directory = args.dir or NCR_ZIP_ROOT
            rows = list_dir(
                args.host, directory, timeout=args.timeout, roots=roots,
                command=args.zbx_get, retries=args.retries,
            )
            result = select_ncr_zips(rows, args.start.date())

        result.update(base)
        result.update({
            'available': True,
            'directory': directory,
            'directory_masked': mask_sensitive(directory),
        })
    except ImageFetchError as exc:
        detail = str(exc)
        if 'E_MISSING' in detail:
            result = dict(base)
            result.update({
                'available': False,
                'reason': 'Not available on ATM',
                'detail': detail,
                'link_basis': ('capture_time+event_sequence'
                               if args.vendor == 'GRG' else 'zip_date_only'),
                'confidence': 'none',
                'candidate_count': 0,
                'candidates': [],
            })
        else:
            print(f'error: {detail}', file=sys.stderr)
            return 1
    except LinkError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1

    if not args.show_sensitive:
        result = _redact(result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
