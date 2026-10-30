#!/usr/bin/env python3
"""
Fake ATM switch — lab stand-in for Dashen's real switch.

Sends REAL length-prefixed ISO 8583 0210 bytes over TCP to the
iso8583-gateway in MODE=tcp (default UAT-02:9876). This exercises the
production ingest path (framing -> parse -> TID map -> DB write) that
txn_engine.py bypasses with direct INSERTs.

Contract (must match camel/iso8583_gateway.py parse_iso8583_message):
  [2-byte big-endian length][MTI 4 ASCII][bitmap 8 bytes][fields ascending]
  F2  PAN, LLVAR (2-digit len + digits)
  F3  processing code, 6 ASCII
  F4  amount cents, 12 ASCII (omitted for non-cash, e.g. balance)
  F11 STAN, 6 ASCII
  F38 auth code, 6 ASCII (omitted unless approved)
  F39 response code, 2 ASCII (00 approved, 51 declined)
  F41 TID, fixed 8 ASCII, space-padded — TIDs longer than 8 are REJECTED
      (real F41 is 8 chars; bank TID format is an open switch-spec item).

Usage:
  python3 scripts/fake_iso8583_switch.py --target 172.22.10.60:9876 \
      --tid TIDTEST1 --count 5
  python3 scripts/fake_iso8583_switch.py --target localhost:9876 \
      --tid TIDTEST1 --count 2 --decline-every 2 --txn-type balance

After the run, verify with source='ISO8583_REAL' rows, then DELETE the
test rows — fake data must never linger under the REAL tag.
"""
import argparse
import random
import socket
import struct
import sys

PROCESSING_CODES = {
    'withdrawal': '012000',
    'balance': '310000',
}

# Obviously-fake test cards. Never real PANs.
TEST_PANS = [
    '4000000000001234',
    '5200000000005678',
    '4000000000009012',
]

AMOUNTS_ETB = [100, 500, 1000, 2000, 5000]


def build_0210(*, tid, pan, stan, amount_cents, processing_code,
               response_code, auth_code):
    """Build one length-prefixed 0210 message. Pure function (testable)."""
    if len(tid) > 8:
        raise ValueError(
            f"TID '{tid}' is {len(tid)} chars; ISO 8583 field 41 is "
            f"fixed 8. Bank TID format is still an open switch-spec item — "
            f"use an 8-char lab TID (e.g. TIDTEST1).")
    fields = {}
    fields[2] = f"{len(pan):02d}{pan}"
    fields[3] = processing_code
    if amount_cents is not None:
        fields[4] = f"{amount_cents:012d}"
    fields[11] = stan
    if auth_code is not None:
        fields[38] = auth_code
    fields[39] = response_code
    fields[41] = tid.ljust(8)

    bitmap = 0
    for f in fields:
        bitmap |= 1 << (64 - f)
    body = (b'0210' + bitmap.to_bytes(8, 'big')
            + b''.join(fields[f].encode('ascii')
                       for f in sorted(fields)))
    return struct.pack('>H', len(body)) + body


def parse_target(target):
    host, _, port = target.partition(':')
    return host or 'localhost', int(port or 9876)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--target', default='172.22.10.60:9876',
                    help='host:port of gateway in MODE=tcp')
    ap.add_argument('--tid', required=True,
                    help='8-char-max lab TID (field 41)')
    ap.add_argument('--count', type=int, default=5)
    ap.add_argument('--txn-type', choices=['withdrawal', 'balance'],
                    default='withdrawal')
    ap.add_argument('--decline-every', type=int, default=5,
                    help='every Nth message is declined (0 = all approved)')
    args = ap.parse_args()

    host, port = parse_target(args.target)
    pc = PROCESSING_CODES[args.txn_type]

    sock = socket.create_connection((host, port), timeout=10)
    print(f"Connected to gateway {host}:{port} as fake switch")
    try:
        for i in range(1, args.count + 1):
            declined = (args.decline_every > 0
                        and i % args.decline_every == 0)
            rc = '51' if declined else '00'
            msg = build_0210(
                tid=args.tid,
                pan=random.choice(TEST_PANS),
                stan=f'{random.randint(0, 999999):06d}',
                amount_cents=(None if args.txn_type == 'balance'
                              else random.choice(AMOUNTS_ETB) * 100),
                processing_code=pc,
                response_code=rc,
                auth_code=(None if declined
                           else f'{random.randint(0, 999999):06d}'),
            )
            sock.sendall(msg)
            print(f"  [{i}/{args.count}] {args.txn_type} "
                  f"{'DECLINED' if declined else 'APPROVED'} "
                  f"({len(msg)} bytes)")
    finally:
        sock.close()
    print(f"Done. Verify: SELECT * FROM atm_transactions "
          f"WHERE source='ISO8583_REAL' AND terminal_id='{args.tid}' "
          f"ORDER BY recorded_at DESC LIMIT {args.count};")
    print("Then DELETE those test rows — fake data must not linger "
          "under the REAL tag.")


if __name__ == '__main__':
    main()
