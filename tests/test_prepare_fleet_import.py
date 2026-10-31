"""The fleet workbook is not the portal's import contract.

These tests pin the fill rules that turn it into one: vendor/district
normalisation, the required city/region/model fields, stable test ATM IDs,
safe synthetic IPs, duplicate handling, and validation with the portal's own
validator. A generated CSV that passes here is importable; one that does not
must fail before an operator uploads 1,202 rows.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))

import prepare_fleet_import as prep  # noqa: E402

HEADER = ['No', 'Terminal ID', 'Terminal Name', 'IP Address', 'Owner Branch',
          'District/Office', 'ATM Vendor and Model', 'Berand', None]


def workbook(tmp_path, rows):
    path = tmp_path / 'fleet.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row in rows:
        ws.append(row + [None] * (len(HEADER) - len(row)))
    wb.save(path)
    return path


def raw_rows(tmp_path, rows):
    return prep.load_workbook_rows(workbook(tmp_path, rows))


def base_rows():
    return [
        [1, 'TID001', 'ATM ONE', '10.0.0.1', 'Branch One', 'NAD',
         'Moti engineering/NCR', 'ATM'],
        [2, 'TID002', 'ATM TWO', '10.0.0.2', '', 'Wolaita',
         'tech24et/GRG', 'ATM'],
        [3, 'TID003', 'ATM THREE', '', 'Branch Three', 'Adama',
         'Moti engineering/NCR', 'Recycler'],
        [4, 'TID004', 'ATM FOUR', '10.0.0.4', 'Branch Four', 'South West',
         'tech24et/GRG', 'ITM'],
    ]


def test_workbook_rows_are_prepared_to_the_portal_contract(tmp_path):
    rows, details, summary = prep.build_rows(
        raw_rows(tmp_path, base_rows()), prefix='TST', ip_mode='synthetic')

    assert summary['rows_prepared'] == 4
    assert summary['rows_skipped'] == 0
    assert summary['validation_errors'] == 0
    assert [r['atm_id'] for r in rows] == [
        'TST-0001', 'TST-0002', 'TST-0003', 'TST-0004']

    by_tid = {r['terminal_id']: r for r in rows}
    assert by_tid['TID001']['vendor'] == 'NCR'
    assert by_tid['TID001']['district'] == 'NAD'
    assert by_tid['TID001']['city'] == 'Addis Ababa'
    assert by_tid['TID001']['region'] == 'Addis Ababa'
    assert by_tid['TID001']['model'] == 'SelfServ 34'
    assert by_tid['TID001']['ej_log_format'] == 'NCR_APTRA'

    assert by_tid['TID002']['vendor'] == 'GRG'
    assert by_tid['TID002']['district'] == 'WOLAITA'
    assert by_tid['TID002']['model'] == 'AD2600'
    assert by_tid['TID002']['ej_log_format'] == 'GRG'
    assert by_tid['TID002']['branch'] == 'ATM TWO BRANCH', (
        'a missing owner branch is filled from the ATM name')

    assert by_tid['TID003']['ip_address'].startswith('198.18.')
    assert by_tid['TID004']['district'] == 'SOUTH WEST'

    assert len({r['ip_address'] for r in rows}) == 4
    assert all(not prep._validate(r) for r in rows)
    assert any('workbook brand=Recycler' in d['notes'] for d in details)
    assert any('workbook brand=ITM' in d['notes'] for d in details)


def test_real_ip_mode_preserves_addresses_and_fills_only_gaps(tmp_path):
    rows = [
        [1, 'TID001', 'ATM ONE', '10.0.0.1', 'B1', 'NAD',
         'Moti engineering/NCR', 'ATM'],
        [2, 'TID002', 'ATM TWO', '', 'B2', 'NAD',
         'Moti engineering/NCR', 'ATM'],
        [3, 'TID003', 'ATM THREE', 'not-an-ip', 'B3', 'NAD',
         'Moti engineering/NCR', 'ATM'],
        [4, 'TID004', 'ATM FOUR', '10.0.0.1', 'B4', 'NAD',
         'Moti engineering/NCR', 'ATM'],
    ]
    prepared, details, summary = prep.build_rows(
        raw_rows(tmp_path, rows), prefix='TST', ip_mode='real')

    by_tid = {r['terminal_id']: r for r in prepared}
    assert by_tid['TID001']['ip_address'] == '10.0.0.1'
    assert by_tid['TID002']['ip_address'].startswith('198.18.')
    assert by_tid['TID003']['ip_address'].startswith('198.18.')
    assert by_tid['TID004']['ip_address'].startswith('198.18.')
    kinds = {d['terminal_id']: d['ip_kind'] for d in details}
    assert kinds == {
        'TID001': 'real', 'TID002': 'synthetic-fill',
        'TID003': 'synthetic-fill', 'TID004': 'synthetic-duplicate'}
    assert summary['ip_kinds']['real'] == 1
    assert len({r['ip_address'] for r in prepared}) == 4


def test_duplicate_terminal_ids_are_skipped_not_imported(tmp_path):
    rows = base_rows() + [
        [5, 'TID001', 'DUPLICATE', '10.0.0.9', 'B5', 'NAD',
         'Moti engineering/NCR', 'ATM']]
    prepared, _details, summary = prep.build_rows(
        raw_rows(tmp_path, rows), prefix='TST', ip_mode='synthetic')

    assert len(prepared) == 4
    assert summary['rows_skipped'] == 1
    assert 'duplicate terminal_id' in summary['skipped'][0]['reason']


def test_unknown_vendor_is_skipped_and_reported(tmp_path):
    rows = [[1, 'TID001', 'ATM ONE', '10.0.0.1', 'B1', 'NAD',
             'Wincor Nixdorf', 'ATM']]
    prepared, _details, summary = prep.build_rows(
        raw_rows(tmp_path, rows), prefix='TST', ip_mode='synthetic')

    assert prepared == []
    assert summary['rows_skipped'] == 1
    assert 'unknown vendor' in summary['skipped'][0]['reason']


def test_limit_is_a_mixed_sample_with_stable_ids(tmp_path):
    rows = []
    for i in range(1, 101):
        vendor = 'Moti engineering/NCR' if i % 2 else 'tech24et/GRG'
        rows.append([i, f'TID{i:03d}', f'ATM {i}', f'10.0.{i // 256}.{i % 256}',
                     f'Branch {i}', 'NAD', vendor, 'ATM'])
    prepared, _details, summary = prep.build_rows(
        raw_rows(tmp_path, rows), prefix='TST', ip_mode='synthetic', limit=10)

    assert len(prepared) == 10
    assert summary['vendors'].get('NCR') and summary['vendors'].get('GRG')
    for row in prepared:
        no = int(row['atm_id'].split('-')[1])
        assert row['terminal_id'] == f'TID{no:03d}', (
            'the id stays tied to the workbook No, not the sample position')
    assert prepared == sorted(prepared, key=lambda r: r['atm_id'])


def test_a_validation_error_blocks_import_ready_status(tmp_path):
    rows = [[1, 'T' * 21, 'ATM ONE', '10.0.0.1', 'B1', 'NAD',
             'Moti engineering/NCR', 'ATM']]
    prepared, details, summary = prep.build_rows(
        raw_rows(tmp_path, rows), prefix='TST', ip_mode='synthetic')

    assert len(prepared) == 1
    assert summary['validation_errors'] == 1
    assert 'terminal_id' in details[0]['validation_errors']


def test_custom_snmp_community_is_carried_into_every_row(tmp_path):
    rows, _details, _summary = prep.build_rows(
        raw_rows(tmp_path, base_rows()), prefix='TST', ip_mode='synthetic',
        snmp_community='dashen_sim')
    assert {r['snmp_community'] for r in rows} == {'dashen_sim'}


def test_generated_csv_has_the_portal_required_columns(tmp_path):
    rows, details, summary = prep.build_rows(
        raw_rows(tmp_path, base_rows()), prefix='TST', ip_mode='synthetic')
    out = tmp_path / 'import.csv'
    detail_path = tmp_path / 'details.csv'
    report = tmp_path / 'report.md'
    prep.write_csv(out, rows, prep.CSV_FIELDS)
    prep.write_csv(detail_path, details, prep.DETAIL_FIELDS)
    prep.write_report(report, summary, out, detail_path, 'fleet.xlsx')

    header = next(csv.reader(out.open()))
    for required in ('atm_id', 'branch', 'district', 'city', 'region',
                     'terminal_id', 'vendor', 'model', 'status'):
        assert required in header
    text = report.read_text()
    assert 'TST-' in text and 'synthetic' in text and 'Cleanup' in text
