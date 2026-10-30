#!/usr/bin/env python3
"""Shadow-parse NCR/GRG electronic journals without writing to the database.

This is the Phase-1 tool from the EJ recommendation: prove parser counts,
amounts and data-quality warnings against real journals before any EJ row is
trusted by reports or the anomaly detector.

Examples:
    python3 scripts/parse_ej_shadow.py "docs/Sample NCR (1).txt"
    python3 scripts/parse_ej_shadow.py --json "docs/Sample GRG (1).txt"
    python3 scripts/parse_ej_shadow.py --terminal-map map.json journals/*.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

from ej_journal import parse_journal_file, summarize  # noqa: E402


def _terminal_map(path):
    if path is None:
        return {}
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError('terminal map must be a regular JSON file')
    source = source.resolve(strict=True)
    # NOSONAR: resolved regular file supplied by the operator CLI.
    data = json.loads(source.read_text(encoding='utf-8'))  # NOSONAR
    if not isinstance(data, dict):
        raise ValueError('terminal map must be a JSON object')
    return {str(k): str(v) for k, v in data.items() if v}


def _file_line(result):
    summary = result.summary()
    return (
        f"{result.vendor:3} {result.encoding:8} "
        f"sessions={summary['sessions']} "
        f"completed={summary['completed']} "
        f"approved={summary['approved']} "
        f"declined={summary['declined']} "
        f"unknown={summary['unknown']} "
        f"incomplete={summary['incomplete']} "
        f"amount={summary['amount']} "
        f"dispensed={summary['dispensed']} "
        f"path={result.path}"
    )


def _print_totals(transactions):
    total = summarize(transactions)
    print(
        f"TOTAL sessions={total['sessions']} "
        f"completed={total['completed']} "
        f"approved={total['approved']} "
        f"declined={total['declined']} "
        f"unknown={total['unknown']} "
        f"incomplete={total['incomplete']} "
        f"amount={total['amount']} ETB "
        f"dispensed={total['dispensed']} ETB"
    )
    if total['warnings']:
        print('WARNINGS ' + ', '.join(
            f'{key}={value}' for key, value in total['warnings'].items()))


def _print_warnings(result):
    for txn in result.transactions:
        if txn.warnings:
            identifier = txn.record_no or txn.serial
            if not identifier or not identifier.strip('0') or not txn.completed:
                identifier = txn.transaction_key
            print(f"  {result.vendor} {identifier}: {', '.join(txn.warnings)}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only shadow parser for NCR/GRG ATM journals.')
    parser.add_argument('journals', nargs='+', type=Path)
    parser.add_argument(
        '--atm-id', default=None,
        help='ATM id to use when a journal has no terminal-to-ATM mapping')
    parser.add_argument(
        '--terminal-map', type=Path, default=None,
        help='JSON object mapping terminal ids to ATM ids')
    parser.add_argument(
        '--json', action='store_true', help='emit full parsed data as JSON')
    parser.add_argument(
        '--show-warnings', action='store_true',
        help='show per-transaction warnings')
    args = parser.parse_args(argv)

    try:
        terminal_map = _terminal_map(args.terminal_map)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'ERROR terminal map: {exc}', file=sys.stderr)
        return 2

    results = []
    errors = []
    for path in args.journals:
        try:
            result = parse_journal_file(
                path, atm_id=args.atm_id, terminal_map=terminal_map)
            if result.vendor == 'UNKNOWN':
                raise ValueError('unrecognised journal format')
            results.append(result)
        except Exception as exc:
            errors.append({'path': str(path), 'error': str(exc)})
            print(f'ERROR {path}: {exc}', file=sys.stderr)

    if args.json:
        payload = {
            'files': [result.to_dict() for result in results],
            'errors': errors,
            'summary': summarize(
                [txn for result in results for txn in result.transactions]),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1 if errors else 0

    for result in results:
        print(_file_line(result))
        if args.show_warnings:
            _print_warnings(result)
    for error in errors:
        print(f"ERROR {error['path']}: {error['error']}")

    if results:
        _print_totals([txn for result in results for txn in result.transactions])
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
