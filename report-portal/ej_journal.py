"""Canonical parser for ATM electronic-journal files (NCR and GRG).

The journal is terminal-side evidence, not a switch ledger. This module turns
the two real journal dialects into a common, auditable transaction shape so a
shadow ingestion job can compare EJ counts with the switch before anything is
written to `atm_transactions`.

Pure stdlib. It never stores raw card data: PANs are masked at parse time.
"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

EAT = timezone(timedelta(hours=3))
UTC = timezone.utc
PARSER_VERSION = 'ej-journal-1'

APPROVED = 'APPROVED'
DECLINED = 'DECLINED'
TIMEOUT = 'TIMEOUT'
ERROR = 'ERROR'
UNKNOWN = 'UNKNOWN'
INCOMPLETE = 'INCOMPLETE'

CASH_TYPES = frozenset({'WITHDRAWAL', 'TRANSFER', 'CARDLESS_TXN'})
_COMPLETED_STATUSES = frozenset({APPROVED, DECLINED, TIMEOUT, ERROR})

_PAN_MASK = re.compile(r'^(\d{4,6})(\*+)(\d{4})$')
_LAST4_MASK = re.compile(r'^(\*+)(\d{4})$')
_BARE_PAN = re.compile(r'(?<!\d)(\d{6})(\d{4,9})(\d{4})(?!\d)')


def _money(value):
    if value is None:
        return None
    text = str(value).strip().replace(',', '')
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def scrub_pan(value):
    """Normalise a PAN-ish value to first6/******/last4, or ******/last4.

    Already-masked values are normalised too, so NCR's variable star runs and
    GRG's four-digit prefixes cannot create two identities for one card.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    masked = _PAN_MASK.fullmatch(text)
    if masked:
        return masked.group(1) + '******' + masked.group(3)
    last4 = _LAST4_MASK.fullmatch(text)
    if last4:
        return '******' + last4.group(2)
    return _BARE_PAN.sub(
        lambda m: m.group(1) + '******' + m.group(3), text)


def _normalise_type(value):
    if not value:
        return None
    raw = re.sub(r'\s+', ' ', str(value).strip().upper())
    aliases = {
        'BALANCE': 'BALANCE_INQ',
        'BALANCE INQUIRY': 'BALANCE_INQ',
        'BALANCE ENQUIRY': 'BALANCE_INQ',
        'MINI STATEMENT': 'MINI_STATEMENT',
        'MINI-STATEMENT': 'MINI_STATEMENT',
        'CARDLESS': 'CARDLESS_TXN',
        'CARDLESS TRANSACTION': 'CARDLESS_TXN',
    }
    return aliases.get(raw, raw.replace(' ', '_'))


def _iso(value):
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


@dataclass
class EjTransaction:
    vendor: str
    atm_id: str | None = None
    terminal_id: str | None = None
    branch: str | None = None
    record_no: str | None = None
    serial: str | None = None
    txn_type: str | None = None
    receipt_type: str | None = None
    amount: Decimal | None = None
    currency: str = 'ETB'
    status: str = UNKNOWN
    error_code: str | None = None
    card_masked: str | None = None
    account_masked: str | None = None
    entry_mode: str | None = None
    auth_code: str | None = None
    requested_amount: Decimal | None = None
    dispensed_amount: Decimal | None = None
    notes: dict[int, int] = field(default_factory=dict)
    emv_result: str | None = None
    reply_code: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    receipt_at: datetime | None = None
    recorded_at: datetime | None = None
    raw_ref: str | None = None
    confidence: str = 'low'
    warnings: list[str] = field(default_factory=list)
    parser_version: str = PARSER_VERSION

    @property
    def completed(self):
        return self.status in _COMPLETED_STATUSES

    @property
    def transaction_key(self):
        terminal = self.terminal_id or self.atm_id or 'UNKNOWN'
        day = ''
        moment = self.ended_at or self.recorded_at or self.started_at
        if moment is not None:
            day = moment.astimezone(EAT).strftime('%Y%m%d')
        ident = self.record_no or self.serial
        if ident and ident.strip('0') and self.completed:
            return f'EJ:{self.vendor}:{terminal}:{day}:{ident}'
        if moment is not None:
            return f'EJ:{self.vendor}:{terminal}:{moment.isoformat()}'
        return f'EJ:{self.vendor}:{terminal}:UNKNOWN'

    @property
    def eligible_for_ingest(self):
        return (self.completed and self.recorded_at is not None
                and self.atm_id is not None)

    def to_dict(self):
        return {
            'vendor': self.vendor,
            'atm_id': self.atm_id,
            'terminal_id': self.terminal_id,
            'branch': self.branch,
            'record_no': self.record_no,
            'serial': self.serial,
            'txn_type': self.txn_type,
            'receipt_type': self.receipt_type,
            'amount': str(self.amount) if self.amount is not None else None,
            'currency': self.currency,
            'status': self.status,
            'error_code': self.error_code,
            'card_masked': self.card_masked,
            'account_masked': self.account_masked,
            'entry_mode': self.entry_mode,
            'auth_code': self.auth_code,
            'requested_amount': (
                str(self.requested_amount)
                if self.requested_amount is not None else None),
            'dispensed_amount': (
                str(self.dispensed_amount)
                if self.dispensed_amount is not None else None),
            'notes': {str(k): v for k, v in sorted(self.notes.items())},
            'emv_result': self.emv_result,
            'reply_code': self.reply_code,
            'started_at': _iso(self.started_at),
            'ended_at': _iso(self.ended_at),
            'receipt_at': _iso(self.receipt_at),
            'recorded_at': _iso(self.recorded_at),
            'raw_ref': self.raw_ref,
            'confidence': self.confidence,
            'warnings': list(self.warnings),
            'parser_version': self.parser_version,
            'transaction_key': self.transaction_key,
            'completed': self.completed,
        }

    def to_db_row(self):
        """Shape for a future shadow insert; recorded_at is naive UTC."""
        recorded = self.recorded_at
        if recorded is not None:
            recorded = recorded.astimezone(UTC).replace(tzinfo=None)
        return {
            'recorded_at': recorded,
            'atm_id': self.atm_id,
            'terminal_id': self.terminal_id,
            'branch': self.branch,
            'txn_type': self.txn_type,
            'card_masked': self.card_masked,
            'amount': self.amount,
            'currency': self.currency,
            'status': self.status,
            'auth_code': self.auth_code,
            'error_code': self.error_code,
            'seq_number': self.record_no or self.serial,
            'iso_mti': None,
            'iso_processing_code': None,
            'iso_stan': None,
            'source': 'EJ',
            'fault_type': None,
            'vendor': self.vendor,
        }


@dataclass
class ParseResult:
    path: str
    vendor: str
    encoding: str
    transactions: list[EjTransaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self):
        out = summarize(self.transactions)
        out.update(path=self.path, vendor=self.vendor,
                   encoding=self.encoding, file_warnings=list(self.warnings))
        return out

    def to_dict(self):
        return {
            'path': self.path,
            'vendor': self.vendor,
            'encoding': self.encoding,
            'warnings': list(self.warnings),
            'summary': self.summary(),
            'transactions': [t.to_dict() for t in self.transactions],
        }


def summarize(transactions):
    status_counts = {}
    warnings = {}
    by_vendor = {}
    by_terminal = {}
    amount_total = Decimal('0.00')
    dispensed_total = Decimal('0.00')
    for txn in transactions:
        status_counts[txn.status] = status_counts.get(txn.status, 0) + 1
        for warning in txn.warnings:
            warnings[warning] = warnings.get(warning, 0) + 1
        vendor = by_vendor.setdefault(txn.vendor, {
            'sessions': 0, 'completed': 0, 'approved': 0,
            'declined': 0, 'unknown': 0, 'incomplete': 0,
            'timeout': 0, 'error': 0,
            'amount': Decimal('0.00'), 'dispensed': Decimal('0.00'),
        })
        vendor['sessions'] += 1
        if txn.completed:
            vendor['completed'] += 1
        if txn.status == APPROVED:
            vendor['approved'] += 1
            if txn.amount is not None:
                vendor['amount'] += txn.amount
                amount_total += txn.amount
            if txn.dispensed_amount is not None:
                vendor['dispensed'] += txn.dispensed_amount
                dispensed_total += txn.dispensed_amount
        elif txn.status == DECLINED:
            vendor['declined'] += 1
        elif txn.status == UNKNOWN:
            vendor['unknown'] += 1
        elif txn.status == INCOMPLETE:
            vendor['incomplete'] += 1
        elif txn.status == TIMEOUT:
            vendor['timeout'] += 1
        elif txn.status == ERROR:
            vendor['error'] += 1
        if txn.terminal_id:
            terminal = by_terminal.setdefault(txn.terminal_id, {
                'sessions': 0, 'completed': 0, 'approved': 0,
                'declined': 0, 'unknown': 0, 'incomplete': 0,
                'timeout': 0, 'error': 0,
                'amount': Decimal('0.00'),
            })
            terminal['sessions'] += 1
            if txn.completed:
                terminal['completed'] += 1
            key = {APPROVED: 'approved', DECLINED: 'declined',
                   UNKNOWN: 'unknown', INCOMPLETE: 'incomplete',
                   TIMEOUT: 'timeout', ERROR: 'error'}.get(txn.status)
            if key:
                terminal[key] += 1
            if txn.status == APPROVED and txn.amount is not None:
                terminal['amount'] += txn.amount

    def money(d):
        return f'{d:.2f}'

    for bucket in by_vendor.values():
        bucket['amount'] = money(bucket['amount'])
        bucket['dispensed'] = money(bucket['dispensed'])
    for bucket in by_terminal.values():
        bucket['amount'] = money(bucket['amount'])

    completed = sum(count for status, count in status_counts.items()
                    if status in _COMPLETED_STATUSES)
    assigned = sum(bucket['sessions'] for bucket in by_terminal.values())
    return {
        'sessions': len(transactions),
        'unassigned': len(transactions) - assigned,
        'completed': completed,
        'approved': status_counts.get(APPROVED, 0),
        'declined': status_counts.get(DECLINED, 0),
        'unknown': status_counts.get(UNKNOWN, 0),
        'incomplete': status_counts.get(INCOMPLETE, 0),
        'timeout': status_counts.get(TIMEOUT, 0),
        'error': status_counts.get(ERROR, 0),
        'amount': money(amount_total),
        'dispensed': money(dispensed_total),
        'status_counts': dict(sorted(status_counts.items())),
        'warnings': dict(sorted(warnings.items())),
        'by_vendor': dict(sorted(by_vendor.items())),
        'by_terminal': dict(sorted(by_terminal.items())),
    }


def decode_journal(raw):
    """Return (decoded text, encoding label) for an EJ file.

    GRG writes UTF-16LE (usually with a BOM); NCR writes 8-bit journal text.
    Decode before line handling: CR-only NCR records and UTF-16 NUL padding
    both destroy the apparent line structure otherwise.
    """
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        text = raw.decode('utf-16', errors='replace')
        encoding = 'utf-16'
    elif b'\x00' in raw[:400]:
        even_nuls = raw[0::2].count(0)
        odd_nuls = raw[1::2].count(0)
        encoding = 'utf-16-le' if even_nuls <= odd_nuls else 'utf-16-be'
        text = raw.decode(encoding, errors='replace')
    else:
        try:
            text = raw.decode('utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
            encoding = 'latin-1'
    text = text.replace('\ufeff', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return text, encoding


def detect_vendor(text):
    if re.search(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+',
                 text, re.MULTILINE):
        return 'GRG'
    if ('[020t' in text or '\x1b[020t' in text
            or 'RECORD NO.' in text or 'DASHEN BANK' in text):
        return 'NCR'
    raise ValueError('Unrecognised ATM journal format')


_NCR_HEADER = re.compile(
    r'^\*(\d+)\*(\d{2}/\d{2}/\d{4})\*(\d{2}):(\d{2})\*?')
_NCR_EVENT = re.compile(r'^(\d{2}):(\d{2}):(\d{2})\s+(.*)$')
_RECEIPT_DATE = re.compile(
    r'^(\d{2})/(\d{2})/(\d{2,4})\s+(\d{2}):(\d{2})\s+(\S+)$')
_RECEIPT_PAN = re.compile(r'^(\d{4,6}\*+\d{4})$')
_RECEIPT_ACC = re.compile(r'^ACC\s+(\S+)$')
_RECEIPT_RECORD = re.compile(r'^RECORD\s+NO\.?\s*(\d+)$')
_RECEIPT_TYPE_AMOUNT = re.compile(
    r'^([A-Z][A-Z0-9 _/-]*?)\s+([\d,]+\.\d{2})$')
_RECEIPT_TYPE_ONLY = re.compile(r'^([A-Z][A-Z0-9 _/-]*)$')
_RECEIPT_BALANCE = re.compile(
    r'^(LEDGER|AVAIL)\s+BAL\s*([\d,]+\.\d{2})?$')
_RECEIPT_AID = re.compile(r'^AID\s*(\S*)$')
_RECEIPT_ERROR_CODE = re.compile(r'^\d{2,3}$')
_RECEIPT_BLACKLIST = (
    'DASHEN', 'THANK', 'ACC', 'LEDGER', 'AVAIL', 'AID', 'RECORD',
    'TRANSACTION', 'CARD', 'NOTES', 'CASH', 'DENOMINATION', 'DISPENSED',
    'REJECTED', 'REMAINING', 'PIN', 'OPCODE', 'ATR', 'GENAC',
)


@dataclass
class _Receipt:
    terminal_id: str | None = None
    branch: str | None = None
    card_masked: str | None = None
    account_masked: str | None = None
    record_no: str | None = None
    txn_type: str | None = None
    amount: Decimal | None = None
    status: str | None = None
    error_code: str | None = None
    receipt_at: datetime | None = None
    aid: str | None = None


def _parse_receipt(lines):
    receipt = _Receipt()
    expecting_branch = False
    for raw in lines:
        line = str(raw).strip()
        if not line:
            continue
        m = _RECEIPT_DATE.match(line)
        if m:
            month, day, year, hh, mm, terminal = m.groups()
            try:
                year_n = int(year)
                if len(year) == 2:
                    year_n += 2000
                receipt.receipt_at = datetime(
                    year_n, int(month), int(day), int(hh), int(mm),
                    tzinfo=EAT)
            except ValueError:
                pass
            receipt.terminal_id = terminal.strip()
            expecting_branch = False
            continue
        m = _RECEIPT_PAN.match(line)
        if m:
            receipt.card_masked = scrub_pan(line)
            expecting_branch = True
            continue
        m = _RECEIPT_ACC.match(line)
        if m:
            receipt.account_masked = scrub_pan(m.group(1))
            expecting_branch = False
            continue
        m = _RECEIPT_RECORD.match(line)
        if m:
            receipt.record_no = m.group(1)
            expecting_branch = False
            continue
        if re.match(r'^(TRANSACTION\s+)?DECLINED\b', line):
            receipt.status = DECLINED
            expecting_branch = False
            continue
        if re.match(r'^TRANSACTION\s+TIMEOUT\b', line):
            receipt.status = TIMEOUT
            expecting_branch = False
            continue
        if re.match(r'^TRANSACTION\s+ERROR\b', line):
            receipt.status = ERROR
            expecting_branch = False
            continue
        if re.match(r'^TRANSACTION\s+APPROVED\b', line):
            receipt.status = APPROVED
            expecting_branch = False
            continue
        if receipt.status in (DECLINED, TIMEOUT, ERROR) and \
                _RECEIPT_ERROR_CODE.match(line):
            receipt.error_code = line
            expecting_branch = False
            continue
        if _RECEIPT_BALANCE.match(line):
            expecting_branch = False
            continue
        m = _RECEIPT_AID.match(line)
        if m:
            receipt.aid = m.group(1) or None
            expecting_branch = False
            continue
        if line.startswith('THANK YOU') or line.startswith('DASHEN BANK |'):
            expecting_branch = False
            continue
        m = _RECEIPT_TYPE_AMOUNT.match(line)
        if m:
            if not line.upper().startswith(_RECEIPT_BLACKLIST):
                receipt.txn_type = _normalise_type(m.group(1))
                receipt.amount = _money(m.group(2))
            expecting_branch = False
            continue
        if expecting_branch:
            receipt.branch = line
            expecting_branch = False
            continue
        m = _RECEIPT_TYPE_ONLY.match(line)
        if m and not line.upper().startswith(_RECEIPT_BLACKLIST):
            receipt.txn_type = receipt.txn_type or _normalise_type(m.group(1))
    if receipt.status is None and receipt.amount is not None:
        receipt.status = APPROVED
    return receipt


@dataclass
class _NcrSession:
    header_date: date | None = None
    header_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    receipt: _Receipt | None = None
    receipt_lines: list[str] | None = None
    in_receipt: bool = False
    notes_presented: list[int] | None = None
    denominations: list[int] | None = None
    notes_taken: bool = False
    warnings: list[str] = field(default_factory=list)


def _eat_from_date(value):
    try:
        return datetime.strptime(value, '%m/%d/%Y').date()
    except ValueError:
        return None


def _combine_eat(day, hh, mm, ss=0, reference=None):
    if day is None:
        return None
    value = datetime(day.year, day.month, day.day,
                     int(hh), int(mm), int(ss), tzinfo=EAT)
    if reference is not None and value < reference - timedelta(hours=12):
        value += timedelta(days=1)
    return value


def _ncr_event(line):
    m = _NCR_EVENT.match(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def _flush_ncr_receipt(session):
    if session.receipt_lines is None:
        return
    parsed = _parse_receipt(session.receipt_lines)
    if session.receipt is None:
        session.receipt = parsed
    else:
        session.warnings.append('multiple_receipts_in_session')
    session.receipt_lines = None
    session.in_receipt = False


def _normalise_ncr(text):
    text = text.replace('\x1b[020t', '\n').replace('[020t', '\n')
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _finalise_ncr(session, atm_id, terminal_map, raw_ref):
    _flush_ncr_receipt(session)
    receipt = session.receipt
    warnings = list(session.warnings)
    if session.ended_at is None:
        warnings.append('no_transaction_end')
    if session.started_at is None:
        warnings.append('no_transaction_start')
    if receipt is None:
        status = INCOMPLETE if session.ended_at is None else UNKNOWN
        warnings.append('no_receipt')
        txn_type = None
        amount = None
        terminal = None
        branch = None
        record = None
        card = None
        account = None
        error_code = None
        receipt_at = None
    else:
        terminal = receipt.terminal_id
        branch = receipt.branch
        record = receipt.record_no
        card = receipt.card_masked
        account = receipt.account_masked
        error_code = receipt.error_code
        receipt_at = receipt.receipt_at
        txn_type = receipt.txn_type
        amount = receipt.amount
        if receipt.status == DECLINED:
            status = DECLINED
        elif receipt.status == TIMEOUT:
            status = TIMEOUT
        elif receipt.status == ERROR:
            status = ERROR
        elif receipt.status == APPROVED:
            status = APPROVED
        elif amount is not None:
            status = APPROVED
        elif txn_type and txn_type not in CASH_TYPES:
            status = APPROVED
        else:
            status = UNKNOWN
            warnings.append('receipt_without_status')

    started_local = session.started_at or receipt_at
    recorded_local = (session.ended_at or receipt_at
                      or started_local or session.header_at)
    if recorded_local is None:
        warnings.append('no_timestamp')
    dispensed = None
    notes = {}
    if receipt is not None and session.notes_presented:
        if session.denominations:
            if len(session.denominations) >= len(session.notes_presented):
                pairs = zip(session.denominations, session.notes_presented)
            else:
                pairs = zip(session.notes_presented, session.denominations)
            total = Decimal('0.00')
            for denom, count in pairs:
                if count:
                    notes[int(denom)] = int(count)
                    total += Decimal(denom) * Decimal(count)
            dispensed = total.quantize(Decimal('0.01'))
        else:
            warnings.append('denominations_missing')
    if (status == APPROVED and txn_type in CASH_TYPES
            and not session.notes_taken):
        warnings.append('cash_not_taken')
    if (status == APPROVED and txn_type in CASH_TYPES
            and dispensed is None):
        warnings.append('dispense_amount_missing')
    if (status == APPROVED and txn_type in CASH_TYPES
            and amount is not None and dispensed is not None
            and dispensed != amount):
        warnings.append('dispense_amount_mismatch')

    atm = None
    if terminal:
        atm = (terminal_map or {}).get(terminal) or atm_id
    if terminal and not atm:
        warnings.append('atm_not_mapped')

    evidence = receipt is not None and terminal and record
    if evidence and status in _COMPLETED_STATUSES:
        if (status == APPROVED and txn_type in CASH_TYPES
                and not (session.notes_taken and dispensed is not None)):
            confidence = 'medium'
        else:
            confidence = 'high'
    elif receipt is not None:
        confidence = 'medium'
    else:
        confidence = 'low'

    return EjTransaction(
        vendor='NCR',
        atm_id=atm,
        terminal_id=terminal,
        branch=branch,
        record_no=record,
        txn_type=txn_type,
        receipt_type=txn_type,
        amount=amount,
        status=status,
        error_code=error_code,
        card_masked=card,
        account_masked=account,
        requested_amount=None,
        dispensed_amount=dispensed,
        notes=notes,
        started_at=(started_local.astimezone(UTC)
                    if started_local else None),
        ended_at=(session.ended_at.astimezone(UTC)
                  if session.ended_at else None),
        receipt_at=(receipt_at.astimezone(UTC) if receipt_at else None),
        recorded_at=(recorded_local.astimezone(UTC)
                     if recorded_local else None),
        raw_ref=raw_ref,
        confidence=confidence,
        warnings=_dedupe(warnings),
    )


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def parse_ncr_text(text, *, atm_id=None, terminal_map=None, raw_ref=None):
    text = _normalise_ncr(text)
    transactions = []
    current = None
    header_date = None
    header_at = None
    for raw in text.split('\n'):
        line = raw.strip()
        if not line:
            continue
        header = _NCR_HEADER.match(line)
        if header:
            header_date = _eat_from_date(header.group(2))
            if header_date is not None:
                header_at = datetime(
                    header_date.year, header_date.month, header_date.day,
                    int(header.group(3)), int(header.group(4)), tzinfo=EAT)
            else:
                header_at = None
            continue
        if 'TRANSACTION START' in line and not _ncr_event(line):
            if current is not None:
                transactions.append(_finalise_ncr(
                    current, atm_id, terminal_map, raw_ref))
            current = _NcrSession(header_date=header_date,
                                  header_at=header_at)
            current.started_at = header_at
            continue
        event = _ncr_event(line)
        if event:
            hh, mm, ss, message = event
            if current is not None and current.in_receipt:
                _flush_ncr_receipt(current)
            if 'TRANSACTION START' in message:
                if current is not None:
                    transactions.append(_finalise_ncr(
                        current, atm_id, terminal_map, raw_ref))
                current = _NcrSession(header_date=header_date,
                                      header_at=header_at)
                current.started_at = _combine_eat(
                    header_date, hh, mm, ss, reference=header_at)
                continue
            if current is None:
                continue
            if current.started_at is None or current.started_at == current.header_at:
                current.started_at = _combine_eat(
                    header_date, hh, mm, ss, reference=header_at)
            if 'TRANSACTION END' in message:
                end_day = header_date
                reference = current.started_at
                if (end_day is None and current.receipt is not None
                        and current.receipt.receipt_at is not None):
                    end_day = current.receipt.receipt_at.date()
                    reference = current.receipt.receipt_at
                current.ended_at = _combine_eat(
                    end_day, hh, mm, ss, reference=reference)
                transactions.append(_finalise_ncr(
                    current, atm_id, terminal_map, raw_ref))
                current = None
                continue
            if 'NOTES PRESENTED' in message:
                current.notes_presented = [
                    int(x) for x in re.findall(r'\d+', message)]
                continue
            if 'NOTES TAKEN' in message:
                current.notes_taken = True
                continue
            continue
        if current is None:
            continue
        if line == 'DASHEN BANK':
            if current.in_receipt:
                _flush_ncr_receipt(current)
            current.in_receipt = True
            current.receipt_lines = []
            continue
        if current.in_receipt:
            if line.startswith('THANK YOU') or line.startswith('DASHEN BANK |'):
                _flush_ncr_receipt(current)
            else:
                current.receipt_lines.append(line)
            continue
        if line.startswith('DENOMINATION'):
            current.denominations = [
                int(x) for x in re.findall(r'\d+', line)]
            continue
    if current is not None:
        transactions.append(_finalise_ncr(
            current, atm_id, terminal_map, raw_ref))
    return transactions


_GRG_LINE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+(.*)$')
_GRG_PAN = re.compile(r'(?:CARD INSERTED|RFCARD INSERTED)\.?\s+PAN:\s*(\S+)')
_GRG_CHIP_PAN = re.compile(r'CHIP CARD PAN:\s*(\S+)')
_GRG_AMOUNT = re.compile(r'AMOUNT ENTERED\s+([\d,]+)')
_GRG_REQUEST = re.compile(r'TRANSACTION REQUEST\s+([A-Z]+)')
_GRG_REPLY = re.compile(r'TRANSACTION REPLY NEXT\s+(\d+)')
_GRG_SERIAL = re.compile(r'TRANSACTION SERIAL NUMBER:\s*(\d+)')
_GRG_EMV = re.compile(r'EMV Completion with\s+([A-Z]+)')
_GRG_CASH = re.compile(
    r'\[CAS\s+([^\]]+)\]\s+OUT:\s*(\d+),\s*REJECT:\s*(\d+),\s+DENO:\s*(\d+)')
_GRG_START = re.compile(r'^-+>\s*TRANSACTION START\b(.*)$')
_GRG_END = re.compile(r'^<-+\s*TRANSACTION END')


def _grg_timestamp(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f').replace(
            tzinfo=EAT)
    except ValueError:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=EAT)


@dataclass
class _GrgSession:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_event_at: datetime | None = None
    implicit_start: bool = False
    entry_mode: str | None = None
    card_masked: str | None = None
    requested_amount: Decimal | None = None
    serial: str | None = None
    request_code: str | None = None
    reply_code: str | None = None
    emv_result: str | None = None
    receipt: _Receipt | None = None
    receipt_lines: list[str] | None = None
    in_receipt: bool = False
    dispense_success: bool = False
    present_success: bool = False
    money_taken: bool = False
    dispensed_amount: Decimal | None = None
    notes: dict[int, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _flush_grg_receipt(session):
    if session.receipt_lines is None:
        return
    parsed = _parse_receipt(session.receipt_lines)
    if session.receipt is None:
        session.receipt = parsed
    else:
        session.warnings.append('multiple_receipts_in_session')
    session.receipt_lines = None
    session.in_receipt = False


def _finalise_grg(session, atm_id, terminal_map, raw_ref):
    _flush_grg_receipt(session)
    receipt = session.receipt
    warnings = list(session.warnings)
    if session.implicit_start:
        warnings.append('implicit_start')
    if session.ended_at is None:
        warnings.append('no_transaction_end')
    if session.started_at is None:
        warnings.append('no_transaction_start')

    terminal = receipt.terminal_id if receipt else None
    branch = receipt.branch if receipt else None
    record = receipt.record_no if receipt else None
    account = receipt.account_masked if receipt else None
    card = receipt.card_masked if receipt else None
    entry_mode = session.entry_mode
    if entry_mode == 'NOCARD':
        card = None
        if receipt is not None and receipt.card_masked:
            warnings.append('cardless_receipt_placeholder')
    txn_type = receipt.txn_type if receipt else None
    receipt_type = txn_type
    if entry_mode == 'NOCARD' and txn_type == 'WITHDRAWAL':
        txn_type = 'CARDLESS_TXN'
    amount = receipt.amount if receipt else None
    error_code = receipt.error_code if receipt else None
    receipt_at = receipt.receipt_at if receipt else None

    status = UNKNOWN
    if session.ended_at is None:
        status = INCOMPLETE
    elif receipt is None:
        warnings.append('no_receipt')
    elif receipt.status == DECLINED:
        status = DECLINED
    elif receipt.status == TIMEOUT:
        status = TIMEOUT
    elif receipt.status == ERROR:
        status = ERROR
    elif txn_type in CASH_TYPES:
        if session.dispense_success and (
                session.money_taken or session.present_success):
            status = APPROVED
        elif session.dispense_success:
            status = APPROVED
            warnings.append('cash_not_taken')
        else:
            warnings.append('receipt_without_dispense')
    elif txn_type is not None or amount is not None:
        status = APPROVED

    if (session.requested_amount is not None and amount is not None
            and session.requested_amount != amount):
        warnings.append('requested_amount_mismatch')
    if (status == APPROVED and txn_type in CASH_TYPES
            and session.dispensed_amount is None):
        warnings.append('dispense_amount_missing')
    if (status == APPROVED and txn_type in CASH_TYPES
            and amount is not None and session.dispensed_amount is not None
            and session.dispensed_amount != amount):
        warnings.append('dispense_amount_mismatch')

    if receipt_at is not None and session.ended_at is not None:
        skew = abs((receipt_at - session.ended_at).total_seconds())
        if skew > 300:
            warnings.append('receipt_time_skew')
    if (session.started_at is not None and session.ended_at is not None
            and (session.ended_at - session.started_at).total_seconds() > 3600):
        warnings.append('long_session')

    if terminal:
        atm = (terminal_map or {}).get(terminal) or atm_id
    else:
        atm = atm_id
    if terminal and not atm:
        warnings.append('atm_not_mapped')

    started_local = session.started_at or receipt_at
    recorded_local = (session.ended_at or session.last_event_at
                      or receipt_at or started_local)
    if recorded_local is None:
        warnings.append('no_timestamp')

    evidence = receipt is not None and terminal and record
    cash_evidence = (
        session.dispense_success and session.money_taken
        and session.dispensed_amount is not None and amount is not None
        and session.dispensed_amount == amount)
    if evidence and status in _COMPLETED_STATUSES:
        if (status == APPROVED and txn_type in CASH_TYPES
                and not cash_evidence):
            confidence = 'medium'
        else:
            confidence = 'high'
    elif receipt is not None:
        confidence = 'medium'
    else:
        confidence = 'low'

    return EjTransaction(
        vendor='GRG',
        atm_id=atm,
        terminal_id=terminal,
        branch=branch,
        record_no=record,
        serial=session.serial,
        txn_type=txn_type,
        receipt_type=receipt_type,
        amount=amount,
        status=status,
        error_code=error_code,
        card_masked=card,
        account_masked=account,
        entry_mode=entry_mode,
        requested_amount=session.requested_amount,
        dispensed_amount=session.dispensed_amount,
        notes=session.notes,
        emv_result=session.emv_result,
        reply_code=session.reply_code,
        started_at=(started_local.astimezone(UTC)
                    if started_local else None),
        ended_at=(session.ended_at.astimezone(UTC)
                  if session.ended_at else None),
        receipt_at=(receipt_at.astimezone(UTC) if receipt_at else None),
        recorded_at=(recorded_local.astimezone(UTC)
                     if recorded_local else None),
        raw_ref=raw_ref,
        confidence=confidence,
        warnings=_dedupe(warnings),
    )


def parse_grg_text(text, *, atm_id=None, terminal_map=None, raw_ref=None):
    transactions = []
    current = None
    for raw in text.split('\n'):
        line = raw.strip()
        if not line:
            continue
        match = _GRG_LINE.match(line)
        if match:
            timestamp = _grg_timestamp(match.group(1))
            message = match.group(2).strip()
            if current is not None and current.in_receipt:
                _flush_grg_receipt(current)
            if _GRG_START.match(message):
                if current is not None:
                    transactions.append(_finalise_grg(
                        current, atm_id, terminal_map, raw_ref))
                marker = _GRG_START.match(message).group(1).strip()
                current = _GrgSession(started_at=timestamp,
                                      last_event_at=timestamp)
                if 'NOCARD' in marker.upper():
                    current.entry_mode = 'NOCARD'
                elif 'CONTACTLESS' in marker.upper():
                    current.entry_mode = 'CONTACTLESS'
                continue
            if _GRG_END.match(message):
                if current is None:
                    continue
                current.ended_at = timestamp
                current.last_event_at = timestamp
                transactions.append(_finalise_grg(
                    current, atm_id, terminal_map, raw_ref))
                current = None
                continue
            if current is None:
                if not any(token in message for token in (
                        'PIN ENTERED', 'TRANSACTION REQUEST', 'CARD INSERTED',
                        'RFCARD INSERTED', 'CHIP CARD PAN', 'AMOUNT ENTERED')):
                    continue
                current = _GrgSession(started_at=timestamp,
                                      last_event_at=timestamp,
                                      implicit_start=True)
            if current.started_at is None:
                current.started_at = timestamp
            current.last_event_at = timestamp
            if 'TRANSACTION RECEIPT DATA' in message:
                if current.in_receipt:
                    _flush_grg_receipt(current)
                current.in_receipt = True
                current.receipt_lines = []
                continue
            if 'DISPENSE SUCCESS' in message:
                current.dispense_success = True
            if 'PRESENT SUCCESS' in message:
                current.present_success = True
            if 'MONEY TAKEN' in message:
                current.money_taken = True
            if 'MODULE STATUS ERROR' in message or 'ERROR!!!' in message:
                current.warnings.append('module_error')
            m = _GRG_PAN.search(message)
            if m:
                current.card_masked = scrub_pan(m.group(1))
                if 'RFCARD' in message:
                    current.entry_mode = 'CONTACTLESS'
                elif current.entry_mode is None:
                    current.entry_mode = 'CHIP'
            m = _GRG_CHIP_PAN.search(message)
            if m:
                current.card_masked = scrub_pan(m.group(1))
                current.entry_mode = 'CHIP'
            m = _GRG_AMOUNT.search(message)
            if m:
                current.requested_amount = _money(m.group(1))
            m = _GRG_REQUEST.search(message)
            if m:
                current.request_code = m.group(1)
            m = _GRG_REPLY.search(message)
            if m:
                current.reply_code = m.group(1)
            m = _GRG_SERIAL.search(message)
            if m:
                current.serial = m.group(1)
            m = _GRG_EMV.search(message)
            if m:
                current.emv_result = m.group(1)
            m = _GRG_CASH.search(message)
            if m:
                out_count = int(m.group(2))
                denom = int(m.group(4))
                if out_count:
                    current.notes[denom] = current.notes.get(denom, 0) + out_count
                    current.dispensed_amount = (
                        current.dispensed_amount or Decimal('0.00')) + (
                        Decimal(denom) * out_count)
            continue
        if current is not None and current.in_receipt:
            current.receipt_lines.append(line)
    if current is not None:
        transactions.append(_finalise_grg(
            current, atm_id, terminal_map, raw_ref))
    return transactions


def parse_journal_text(text, *, vendor=None, path='<memory>', atm_id=None,
                       terminal_map=None):
    try:
        detected = vendor or detect_vendor(text)
    except ValueError:
        return ParseResult(path=path, vendor='UNKNOWN', encoding='text',
                           transactions=[], warnings=['unrecognised_format'])
    if detected == 'NCR':
        transactions = parse_ncr_text(
            text, atm_id=atm_id, terminal_map=terminal_map, raw_ref=path)
    elif detected == 'GRG':
        transactions = parse_grg_text(
            text, atm_id=atm_id, terminal_map=terminal_map, raw_ref=path)
    else:
        raise ValueError(f'Unsupported journal vendor: {detected}')
    return ParseResult(path=path, vendor=detected, encoding='text',
                       transactions=transactions)


def parse_journal_file(path, *, atm_id=None, terminal_map=None):
    path = Path(path)
    raw = path.read_bytes()
    text, encoding = decode_journal(raw)
    result = parse_journal_text(
        text, path=str(path), atm_id=atm_id, terminal_map=terminal_map)
    result.encoding = encoding
    return result


__all__ = [
    'EjTransaction', 'ParseResult', 'PARSER_VERSION', 'APPROVED', 'DECLINED',
    'TIMEOUT', 'ERROR', 'UNKNOWN', 'INCOMPLETE', 'decode_journal',
    'detect_vendor', 'parse_journal_file', 'parse_journal_text',
    'parse_ncr_text', 'parse_grg_text', 'scrub_pan', 'summarize',
]
