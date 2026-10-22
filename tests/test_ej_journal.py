"""Tests for report-portal/ej_journal.py.

The parser is the shadow-ingestion gate: a wrong approval, amount or cash
figure is worse than no figure. These fixtures are synthetic but keep the
real NCR/GRG framing, including the details that made the first live samples
hard (CR control records, UTF-16, receipt-after-dispense, cardless dummy PAN,
and sessions with no receipt).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'report-portal'))

import ej_journal as ej  # noqa: E402

UTC = timezone.utc
MAP = {'NCRT0001': 'ATM-NCR-1', 'GRGT0001': 'ATM-GRG-1'}


NCR_APPROVED = (
    "\x1b[020t*303*09/09/2026*23:32*\n"
    "     *TRANSACTION START*\n"
    "\x1b[020t 23:32:13 CARD INSERTED\n"
    "\x1b[020t 23:32:16 PIN ENTERED\n"
    "\x1b[020t 23:32:30 NOTES STACKED\n"
    "    DASHEN BANK\n"
    "    09/09/26  23:32 NCRT0001\n"
    "    923140*********5264\n"
    "    TEST BRANCH NCR 001\n"
    "    RECORD NO.                         7748\n"
    "    WITHDRAWAL                     3,000.00\n"
    "    ACC      ************5011\n"
    "    LEDGER BAL                    22,984.28\n"
    "    AVAIL BAL                     22,984.28\n"
    "    AID A000000025010402\n"
    "    THANK YOU FOR VISITING DASHEN BANK\n"
    "    DASHEN BANK | ALWAYS ONE STEP AHEAD!\n"
    " 23:32:35 NOTES PRESENTED 0,0,0,15\n"
    "\n"
    "CASH TOTAL       TYPE1 TYPE2 TYPE3 TYPE4\n"
    "DENOMINATION        50   100   100   200\n"
    "DISPENSED        00000 00000 01256 03112\n"
    "REJECTED         00000 00000 00126 00030\n"
    "REMAINING        00000 00000            \n"
    "\n"
    " 23:32:40 NOTES TAKEN\n"
    " 23:32:44 CARD TAKEN\n"
    " 23:32:44 TRANSACTION END\n"
    "\x1b[020t*304*09/09/2026*23:32*\n"
    "     *PRIMARY CARD READER ACTIVATED*\n"
)

NCR_DECLINED = (
    "\x1b[020t*305*09/10/2026*07:34*\n"
    "     *TRANSACTION START*\n"
    "\x1b[020t 07:34:14 CARD INSERTED\n"
    "\x1b[020t 07:34:18 PIN ENTERED\n"
    "\x1b[020t 07:34:20 OPCODE = AA   C B\n"
    "\x1b[020t 07:34:24 NOTES STACKED\n"
    "    DASHEN BANK\n"
    "    09/10/26  07:34 NCRT0001\n"
    "    458571******1398\n"
    "    TEST BRANCH NCR 001\n"
    "    RECORD NO.                         7749\n"
    "    WITHDRAWAL\n"
    "    TRANSACTION DECLINED\n"
    "    56\n"
    "    ACC      ************3738\n"
    "    LEDGER BAL\n"
    "    AVAIL BAL                    885,381.22\n"
    "    AID D231CBE00001\n"
    "    THANK YOU FOR VISITING DASHEN BANK\n"
    "    DASHEN BANK | ALWAYS ONE STEP AHEAD!\n"
    " 07:34:30 CARD TAKEN\n"
    " 07:34:35 TRANSACTION END\n"
)

GRG_APPROVED = (
    "2026-10-12 08:26:45.940 ----> TRANSACTION START\n"
    "2026-10-12 08:26:45.984 CARD INSERTED. PAN: 4583********6018\n"
    "2026-10-12 08:26:53.734 CHIP CARD PAN: 4583********6018\n"
    "2026-10-12 08:26:59.018 PIN ENTERED\n"
    "2026-10-12 08:27:08.724 TRANSACTION REQUEST AB   C A\n"
    "2026-10-12 08:27:09.992 TRANSACTION REPLY NEXT 120\n"
    "2026-10-12 08:27:10.012 TRANSACTION SERIAL NUMBER:4588\n"
    "2026-10-12 08:27:11.488 EMV Completion with TC:<13C6868CB71D021E>.\n"
    "2026-10-12 08:27:11.598 TRANSACTION RECEIPT DATA\n"
    "    DASHEN BANK\n"
    "    10/12/26  08:27 GRGT0001\n"
    "    458300******6018\n"
    "    TEST BRANCH GRG 001\n"
    "    RECORD NO.   4588\n"
    "    WITHDRAWAL                       500.00\n"
    "    ACC      *********6995\n"
    "    LEDGER BAL       \n"
    "    AVAIL BAL                     21,564.74\n"
    "    AID A0000000031010\n"
    "    THANK YOU FOR VISITING DASHEN BANK\n"
    "    DASHEN BANK | ALWAYS ONE STEP AHEAD!\n"
    "\n"
    "2026-10-12 08:27:12.700 DISPENSE COMMAND FROM HOST:\n"
    "[Type (C)], REQUESTED: 01\n"
    "[Type (D)], REQUESTED: 02\n"
    "2026-10-12 08:27:12.770 DISPENSE COMMAND TO CASSETTE:\n"
    "[Type (C)][CAS 0100B] REQUESTED: 01\n"
    "[Type (D)][CAS 0200A] REQUESTED: 02\n"
    "2026-10-12 08:27:23.830 DISPENSE SUCCESS\n"
    "2026-10-12 08:27:25.044 [CAS 0100B] OUT: 01, REJECT: 00, DENO: 100\n"
    "2026-10-12 08:27:25.065 [CAS 0200A] OUT: 02, REJECT: 00, DENO: 200\n"
    "2026-10-12 08:27:31.136 PRESENT SUCCESS.\n"
    "2026-10-12 08:27:33.481 DELIVER MONEY...MONEY TAKEN=======\n"
    "2026-10-12 08:27:37.853 CARD TAKEN\n"
    "2026-10-12 08:27:37.902 <---- TRANSACTION END\n"
)

GRG_CARDLESS = (
    "2026-10-12 09:47:33.558 ----> TRANSACTION START NOCARD\n"
    "2026-10-12 09:48:03.869 AMOUNT ENTERED 2000\n"
    "2026-10-12 09:48:05.425 TRANSACTION REQUEST AC C   A\n"
    "2026-10-12 09:48:06.513 TRANSACTION REPLY NEXT 123\n"
    "2026-10-12 09:48:06.535 TRANSACTION SERIAL NUMBER:4592\n"
    "2026-10-12 09:48:18.871 DISPENSE SUCCESS\n"
    "2026-10-12 09:48:19.963 [CAS 0200A] OUT: 10, REJECT: 00, DENO: 200\n"
    "2026-10-12 09:48:25.675 PRESENT SUCCESS.\n"
    "2026-10-12 09:48:28.092 DELIVER MONEY...MONEY TAKEN=======\n"
    "2026-10-12 09:48:28.884 TRANSACTION RECEIPT DATA\n"
    "    DASHEN BANK\n"
    "    10/12/26  09:48 GRGT0001\n"
    "    888888******8888\n"
    "    TEST BRANCH GRG 001\n"
    "    RECORD NO.   4592\n"
    "    WITHDRAWAL                     2,000.00\n"
    "    ACC      ************3041\n"
    "    LEDGER BAL                     6,340.23\n"
    "    AVAIL BAL                      6,340.23\n"
    "    AID\n"
    "    THANK YOU FOR VISITING DASHEN BANK\n"
    "    DASHEN BANK | ALWAYS ONE STEP AHEAD!\n"
    "2026-10-12 09:48:37.282 <---- TRANSACTION END\n"
)

GRG_UNKNOWN = (
    "2026-10-12 00:00:01.629 PIN ENTERED\n"
    "2026-10-12 00:00:10.460 TRANSACTION REQUEST AAC    B\n"
    "2026-10-12 00:00:11.772 TRANSACTION REPLY NEXT 150\n"
    "2026-10-12 00:00:11.792 TRANSACTION SERIAL NUMBER:0000\n"
    "2026-10-12 00:00:13.223 EMV Completion with AAC:<2A3F0EDE8108CBED>.\n"
    "2026-10-12 00:00:23.446 CARD TAKEN\n"
    "2026-10-12 00:00:23.500 <---- TRANSACTION END\n"
)


def _one(text, vendor, **kw):
    result = ej.parse_journal_text(text, vendor=vendor, **kw)
    assert len(result.transactions) == 1
    return result.transactions[0]


def test_ncr_approved_amount_cash_and_timestamps():
    txn = _one(NCR_APPROVED, 'NCR', path='ncr.txt', terminal_map=MAP)
    assert txn.vendor == 'NCR'
    assert txn.atm_id == 'ATM-NCR-1'
    assert txn.terminal_id == 'NCRT0001'
    assert txn.branch == 'TEST BRANCH NCR 001'
    assert txn.record_no == '7748'
    assert txn.txn_type == 'WITHDRAWAL'
    assert txn.status == ej.APPROVED
    assert txn.amount == Decimal('3000.00')
    assert txn.dispensed_amount == Decimal('3000.00')
    assert txn.notes == {200: 15}
    assert txn.card_masked == '923140******5264'
    assert txn.account_masked == '******5011'
    assert txn.receipt_at == datetime(2026, 9, 9, 20, 32, tzinfo=UTC)
    assert txn.started_at == datetime(2026, 9, 9, 20, 32, 13, tzinfo=UTC)
    assert txn.recorded_at == datetime(2026, 9, 9, 20, 32, 44, tzinfo=UTC)
    assert txn.confidence == 'high'
    assert txn.transaction_key == 'EJ:NCR:NCRT0001:20260909:7748'
    assert txn.eligible_for_ingest is True
    assert 'atm_not_mapped' not in txn.warnings


def test_ncr_receipt_header_with_branch_suffix_is_parsed():
    text = NCR_DECLINED.replace('    DASHEN BANK\n', '    DASHEN BANK1\n')
    txn = _one(text, 'NCR', terminal_map=MAP)
    assert txn.status == ej.DECLINED
    assert txn.record_no == '7749'
    assert txn.amount is None


def test_ncr_branch_line_is_not_mistaken_for_a_receipt_header():
    text = NCR_APPROVED.replace(
        'TEST BRANCH NCR 001', 'DASHEN BANK HQ 15 FLOOR')
    txn = _one(text, 'NCR', terminal_map=MAP)
    assert txn.record_no == '7748'
    assert txn.branch == 'DASHEN BANK HQ 15 FLOOR'
    assert txn.amount == Decimal('3000.00')


def test_ncr_declined_has_no_amount_and_keeps_error_code():
    txn = _one(NCR_DECLINED, 'NCR', terminal_map=MAP)
    assert txn.status == ej.DECLINED
    assert txn.amount is None
    assert txn.dispensed_amount is None
    assert txn.error_code == '56'
    assert txn.txn_type == 'WITHDRAWAL'
    assert txn.card_masked == '458571******1398'


def test_ncr_one_receipt_after_two_pin_attempts_is_one_transaction():
    text = NCR_APPROVED.replace(
        "\x1b[020t 23:32:16 PIN ENTERED\n",
        "\x1b[020t 23:32:16 PIN ENTERED\n"
        "\x1b[020t 23:32:20 OPCODE = C    C B\n"
        "\x1b[020t 23:32:25 PIN ENTERED\n"
        "\x1b[020t 23:32:28 OPCODE = AD   C B\n")
    txn = _one(text, 'NCR', terminal_map=MAP)
    assert txn.record_no == '7748'
    assert txn.amount == Decimal('3000.00')


def test_ncr_cross_midnight_rolls_the_end_date_forward():
    text = (NCR_APPROVED
            .replace('09/09/2026*23:32*', '09/09/2026*23:59*')
            .replace('23:32:13 CARD', '23:59:50 CARD')
            .replace('23:32:16 PIN', '23:59:51 PIN')
            .replace('23:32:35 NOTES', '23:59:55 NOTES')
            .replace('23:32:40 NOTES', '23:59:56 NOTES')
            .replace('23:32:44 CARD', '00:00:04 CARD')
            .replace('23:32:44 TRANSACTION', '00:00:10 TRANSACTION'))
    txn = _one(text, 'NCR', terminal_map=MAP)
    assert txn.started_at == datetime(2026, 9, 9, 20, 59, 50, tzinfo=UTC)
    assert txn.recorded_at == datetime(2026, 9, 9, 21, 0, 10, tzinfo=UTC)


def test_ncr_session_without_receipt_is_unknown_not_approved():
    text = (
        "\x1b[020t*500*09/10/2026*10:00*\n"
        "     *TRANSACTION START*\n"
        "\x1b[020t 10:00:05 CARD INSERTED\n"
        "\x1b[020t 10:00:15 PIN ENTERED\n"
        " 10:00:20 TRANSACTION END\n")
    txn = _one(text, 'NCR')
    assert txn.status == ej.UNKNOWN
    assert txn.amount is None
    assert txn.record_no is None
    assert 'no_receipt' in txn.warnings
    assert txn.completed is False


def test_grg_approved_receipt_and_dispense_amount_agree():
    txn = _one(GRG_APPROVED, 'GRG', terminal_map=MAP)
    assert txn.vendor == 'GRG'
    assert txn.atm_id == 'ATM-GRG-1'
    assert txn.terminal_id == 'GRGT0001'
    assert txn.branch == 'TEST BRANCH GRG 001'
    assert txn.record_no == '4588'
    assert txn.serial == '4588'
    assert txn.entry_mode == 'CHIP'
    assert txn.status == ej.APPROVED
    assert txn.amount == Decimal('500.00')
    assert txn.dispensed_amount == Decimal('500.00')
    assert txn.notes == {100: 1, 200: 2}
    assert txn.card_masked == '458300******6018'
    assert txn.recorded_at == datetime(2026, 10, 12, 5, 27, 37, 902000,
                                       tzinfo=UTC)
    assert txn.confidence == 'high'


def test_grg_receipt_can_arrive_after_money_taken():
    receipt_start = GRG_APPROVED.index('2026-10-12 08:27:11.598')
    receipt_end = GRG_APPROVED.index('2026-10-12 08:27:12.700')
    receipt = GRG_APPROVED[receipt_start:receipt_end]
    without = GRG_APPROVED[:receipt_start] + GRG_APPROVED[receipt_end:]
    money_at = without.index('2026-10-12 08:27:33.481')
    moved = without[:money_at] + receipt + without[money_at:]
    txn = _one(moved, 'GRG', terminal_map=MAP)
    assert txn.status == ej.APPROVED
    assert txn.amount == Decimal('500.00')
    assert txn.record_no == '4588'


def test_grg_cardless_placeholder_pan_is_not_stored_as_a_card():
    txn = _one(GRG_CARDLESS, 'GRG', terminal_map=MAP)
    assert txn.entry_mode == 'NOCARD'
    assert txn.txn_type == 'CARDLESS_TXN'
    assert txn.receipt_type == 'WITHDRAWAL'
    assert txn.card_masked is None
    assert txn.status == ej.APPROVED
    assert txn.amount == Decimal('2000.00')
    assert txn.dispensed_amount == Decimal('2000.00')
    assert 'cardless_receipt_placeholder' in txn.warnings


def test_grg_no_receipt_is_unknown_and_keeps_diagnostics():
    txn = _one(GRG_UNKNOWN, 'GRG')
    assert txn.status == ej.UNKNOWN
    assert txn.amount is None
    assert txn.record_no is None
    assert txn.reply_code == '150'
    assert txn.emv_result == 'AAC'
    assert 'implicit_start' in txn.warnings
    assert 'no_receipt' in txn.warnings
    assert txn.transaction_key.startswith('EJ:GRG:UNKNOWN:')


def test_session_without_end_is_incomplete_not_a_silent_transaction():
    text = GRG_APPROVED.rsplit('2026-10-12 08:27:37.902', 1)[0]
    txn = _one(text, 'GRG', terminal_map=MAP)
    assert txn.status == ej.INCOMPLETE
    assert 'no_transaction_end' in txn.warnings
    assert txn.amount == Decimal('500.00')


def test_journal_file_decodes_utf16_and_cr_only_ncr(tmp_path):
    grg_file = tmp_path / 'grg.txt'
    grg_file.write_bytes(GRG_APPROVED.encode('utf-16'))
    result = ej.parse_journal_file(grg_file, terminal_map=MAP)
    assert result.vendor == 'GRG'
    assert result.encoding == 'utf-16'
    assert len(result.transactions) == 1

    ncr_file = tmp_path / 'ncr.txt'
    ncr_file.write_bytes(NCR_APPROVED.replace('\n', '\r').encode('ascii'))
    result = ej.parse_journal_file(ncr_file, terminal_map=MAP)
    assert result.vendor == 'NCR'
    assert result.encoding == 'utf-8'
    assert len(result.transactions) == 1
    assert result.transactions[0].record_no == '7748'


def test_pan_scrubbing_handles_full_pan_and_every_mask_shape():
    assert ej.scrub_pan('3799871234561477') == '379987******1477'
    assert ej.scrub_pan('923140*********5264') == '923140******5264'
    assert ej.scrub_pan('************5011') == '******5011'
    assert ej.scrub_pan('4006********4052') == '4006******4052'
    assert ej.scrub_pan(None) is None


def test_summary_counts_approved_declined_and_unknown():
    txn_ncr = _one(NCR_APPROVED, 'NCR', terminal_map=MAP)
    txn_declined = _one(NCR_DECLINED, 'NCR', terminal_map=MAP)
    txn_unknown = _one(GRG_UNKNOWN, 'GRG')
    summary = ej.summarize([txn_ncr, txn_declined, txn_unknown])
    assert summary['sessions'] == 3
    assert summary['completed'] == 2
    assert summary['approved'] == 1
    assert summary['declined'] == 1
    assert summary['unknown'] == 1
    assert summary['amount'] == '3000.00'
    assert summary['dispensed'] == '3000.00'
    assert summary['by_vendor']['NCR']['sessions'] == 2


def test_db_row_is_naive_utc_for_the_timestamp_without_time_zone_column():
    txn = _one(NCR_APPROVED, 'NCR', terminal_map=MAP)
    row = txn.to_db_row()
    assert row['recorded_at'] == datetime(2026, 9, 9, 20, 32, 44)
    assert row['recorded_at'].tzinfo is None
    assert row['source'] == 'EJ'
    assert row['vendor'] == 'NCR'
    assert row['seq_number'] == '7748'
    assert row['card_masked'] == '923140******5264'


def test_money_is_not_ingested_until_the_terminal_maps_to_an_atm():
    txn = _one(NCR_APPROVED, 'NCR')
    assert txn.completed is True
    assert txn.atm_id is None
    assert txn.eligible_for_ingest is False
    assert 'atm_not_mapped' in txn.warnings


def test_requested_and_receipt_amounts_must_not_diverge_silently():
    text = GRG_CARDLESS.replace('AMOUNT ENTERED 2000', 'AMOUNT ENTERED 1500')
    txn = _one(text, 'GRG', terminal_map=MAP)
    assert txn.amount == Decimal('2000.00')
    assert txn.requested_amount == Decimal('1500.00')
    assert 'requested_amount_mismatch' in txn.warnings


def test_ncr_approved_receipt_without_cash_taken_is_flagged():
    text = NCR_APPROVED.replace(' 23:32:40 NOTES TAKEN\n', '')
    txn = _one(text, 'NCR', terminal_map=MAP)
    assert txn.status == ej.APPROVED
    assert 'cash_not_taken' in txn.warnings
    assert txn.confidence == 'medium'


def test_shadow_cli_emits_one_valid_json_document(tmp_path):
    journal = tmp_path / 'ncr.txt'
    journal.write_text(NCR_APPROVED)
    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'scripts', 'parse_ej_shadow.py')
    proc = subprocess.run(
        [sys.executable, script, '--json', str(journal)],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['summary']['sessions'] == 1
    assert payload['summary']['approved'] == 1
    assert payload['files'][0]['vendor'] == 'NCR'
    assert payload['errors'] == []


def test_unknown_format_is_reported_not_guessed():
    result = ej.parse_journal_text('not an ATM journal', path='bad.txt')
    assert result.vendor == 'UNKNOWN'
    assert result.transactions == []
    assert result.warnings == ['unrecognised_format']
    with pytest.raises(ValueError):
        ej.detect_vendor('not an ATM journal')
