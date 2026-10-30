#!/usr/bin/env python3
"""Profile pulled EJ journals: what is in them, and what could be parsed.

This exists to answer one question honestly before anyone writes an EJ parser:
*of the lines the ATM actually produces, how many carry a transaction?* Without
that number, a parser is a guess, and a confidently wrong parser produces
plausible amounts that are false - worse than no parser at all.

It prints the SHAPE of each line rather than its content, so the output can be
shared (in a ticket, a chat, a report) without leaking card data: every run of
digits is masked, and long runs are masked hardest because that is what a PAN
looks like.

Usage:

    python3 scripts/ej_profile.py                      # profile ej-logs/*.log
    python3 scripts/ej_profile.py path/to/one.log     # a single file
    python3 scripts/ej_profile.py --min-count 2        # hide one-off shapes
    python3 scripts/ej_profile.py --json               # machine readable

Nothing here writes to the database. Profiling is a measurement, and a
measurement that changed the data would not be one.
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

# Transaction-ish words as they appear in NCR/ISO EJ text. Deliberately broad and
# matched case-insensitively: the point is to tell "this line looks like money
# movement" from "this line is the terminal reporting it is alive", not to
# classify perfectly.
TXN_WORDS = (b'withdraw', b'deposit', b'transfer', b'balance', b'inquiry',
             b'purchase', b'reversal', b'resp', b'approved', b'declin', b'txn',
             b'transaction', b'card read', b'dispense', b'cash', b'pin')
STATE_WORDS = (b'on-line', b'online', b'off-line', b'offline', b'sst', b'heartbeat',
               b'component version', b'boot', b'login', b'logoff', b'status',
               b'config', b'initialis', b'initializ')

AMOUNT_RE = re.compile(rb'(?<![\d.])(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})(?![\d])')
LONG_DIGITS_RE = re.compile(rb'\d{6,}')

# A new NDC record starts ONLY at a full marker carrying a date. This mirrors
# filebeat.yml's multiline rule exactly (same pattern, same reasoning), so the
# profiler measures the same units the index is built from.
#
# Without this, a single withdrawal spanning four lines is counted as four
# unknown lines and a transaction share comes out far too low - which is exactly
# the number we are trying to measure.
RECORD_START_RE = re.compile(rb'\[020t\s*\*[^*]*\*\d{2}/\d{2}/\d{4}\*')
# A pipe-delimited line is its own record (the legacy single-line format).
PIPE_RECORD_RE = re.compile(rb'\|\s*[A-Z0-9_]+\s*\|')


def mask(line):
    """Mask digit runs so the output can be shared safely.

    A 12-19 digit run is a PAN: keep the first 6 and last 4, which is enough to
    tell two cards apart and useless to anyone trying to use one. Shorter runs
    are amounts, response codes and trace numbers, and are kept - they are the
    fields the parser cares about.
    """
    def repl(m):
        run = m.group(0)
        if 12 <= len(run) <= 19:
            return run[:6] + b'*' * (len(run) - 10) + run[-4:]
        if len(run) >= 20:
            return b'<%d digits>' % len(run)
        return run
    return LONG_DIGITS_RE.sub(repl, line)


def shape(line, width=72):
    """Reduce a line to its skeleton: structure without values."""
    s = mask(line).strip()
    s = re.sub(rb'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', b'<ts>', s)
    s = re.sub(rb'\d{2}/\d{2}/\d{4}', b'<date>', s)
    s = re.sub(rb'\d{2}:\d{2}:\d{2}', b'<time>', s)
    s = re.sub(rb'[A-Za-z0-9]{16,}', b'<blob>', s)
    s = re.sub(rb'\s+', b' ', s)
    return s[:width]


def classify(line):
    low = line.lower()
    if any(w in low for w in TXN_WORDS):
        return 'txn-ish'
    if any(w in low for w in STATE_WORDS):
        return 'state-ish'
    return 'other'


def group_records(raw_lines):
    """Group raw lines into EJ records.

    A line starting a full NDC marker begins a new record; every other line
    belongs to the record above it. A pipe-delimited line stands alone.
    Preamble before the first marker is kept as its own record, because boot
    banners and firmware lines legitimately appear before any marker.
    """
    records, current = [], []
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        if RECORD_START_RE.search(line):
            if current:
                records.append(current)
            current = [line]
        elif PIPE_RECORD_RE.search(line):
            if current:
                records.append(current)
            records.append([line])
            current = []
        else:
            current.append(line)
    if current:
        records.append(current)
    return records


def profile(path, min_count=1):
    shapes = collections.Counter()
    classes = collections.Counter()
    amounts = collections.Counter()
    examples = {}
    n_lines = n_bytes = 0
    with open(path, 'rb') as fh:
        data = fh.read()
    n_bytes = len(data)
    raw_lines = data.splitlines()
    for raw in raw_lines:
        n_lines += 1
        line = raw.strip()
        if not line:
            continue
        cls = classify(line)
        classes[cls] += 1
        sh = shape(line)
        shapes[sh] += 1
        examples.setdefault(sh, line)
        # A firmware line like "COMPONENT VERSION|0CC 03.00.00" contains
        # something that looks exactly like an amount. Counting it would
        # overstate what a parser could extract, so version-ish lines are
        # skipped rather than quietly inflating the figure.
        if b'VERSION' in line.upper() or b'COMPONENT' in line.upper():
            continue
        for m in AMOUNT_RE.finditer(line):
            amounts[m.group(1).decode('ascii', 'replace')] += 1

    # Record-level view: the number that actually answers "can a parser see
    # transactions?", because a transaction can span many lines.
    rec_classes = collections.Counter()
    n_records = 0
    for rec in group_records(raw_lines):
        n_records += 1
        rec_classes[classify(b' '.join(rec))] += 1

    total = sum(classes.values()) or 1
    kept = [(s, c) for s, c in shapes.most_common() if c >= min_count]
    return {
        'file': os.path.basename(path),
        'path': path,
        'lines': n_lines,
        'records': n_records,
        'bytes': n_bytes,
        'record_classes': {k: {'count': v,
                               'pct': round(100.0 * v / n_records, 1) if n_records else 0.0}
                           for k, v in rec_classes.items()},
        'classes': {k: {'count': v, 'pct': round(100.0 * v / total, 1)}
                    for k, v in classes.items()},
        'shapes': [{'shape': s.decode('utf-8', 'replace'), 'count': c,
                    'kind': classify(examples[s]),
                    'example': mask(examples[s]).decode('utf-8', 'replace')[:160]}
                   for s, c in kept],
        'distinct_amounts': len(amounts),
        'top_amounts': [{'amount': a, 'count': c}
                        for a, c in amounts.most_common(5)],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*', help='EJ files (default: ej-logs/*.log)')
    ap.add_argument('--min-count', type=int, default=1,
                    help='only show shapes seen at least this many times')
    ap.add_argument('--json', action='store_true', help='emit JSON')
    args = ap.parse_args()

    paths = args.paths or sorted(glob.glob(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ej-logs', '*.log')))
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        print('No EJ files found. Pull first: ./scripts/pull_ej_logs.sh 2')
        return 1

    reports = []
    rgrand = collections.Counter()
    rgrand_total = 0
    for p in paths:
        try:
            rep = profile(p, args.min_count)
            reports.append(rep)
            for k, v in rep['record_classes'].items():
                rgrand[k] += v['count']
            rgrand_total += rep['records']
        except Exception as e:
            print('skip %s: %s' % (p, e), file=sys.stderr)

    if args.json:
        print(json.dumps(reports, indent=2))
        return 0

    print('EJ journal profile - %d file(s), %s' % (
        len(reports), dt.datetime.now().strftime('%Y-%m-%d %H:%M')))
    print('Digit runs are masked: a 12-19 digit run is treated as a PAN.\n')
    grand = collections.Counter()
    for r in reports:
        print('=' * 78)
        print('%s  %s lines, %s bytes' % (r['file'], r['lines'], r['bytes']))
        parts = ['%s %.1f%%' % (k, v['pct']) for k, v in sorted(r['classes'].items())]
        print('  line kinds: %s' % (', '.join(parts) or 'none'))
        for k, v in r['classes'].items():
            grand[k] += v['count']
        print('  distinct amounts seen: %d  %s' % (
            r['distinct_amounts'],
            ', '.join('%s x%d' % (a['amount'], a['count']) for a in r['top_amounts']) or '-'))
        print('  shapes:')
        for s in r['shapes'][:12]:
            print('    [%4d] %-9s %s' % (s['count'], s['kind'], s['shape']))
        if len(r['shapes']) > 12:
            print('    ... and %d more shape(s)' % (len(r['shapes']) - 12))
    total = sum(grand.values()) or 1
    print('=' * 78)
    print('LINE level  : %s' % ', '.join('%s %.1f%%' % (k, 100.0 * v / total)
                                         for k, v in sorted(grand.items())))
    gtot = sum(rgrand.values()) or 1
    print('RECORD level: %s' % ', '.join('%s %.1f%%' % (k, 100.0 * v / gtot)
                                         for k, v in sorted(rgrand.items())))
    print()
    print('  %d records from %d lines. A transaction often spans several lines,'
          % (rgrand_total, sum(r['lines'] for r in reports)))
    print('  so RECORD level is the honest figure; LINE level is a lower bound.')
    print('  The txn-ish share is the ceiling on what any parser could extract.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
