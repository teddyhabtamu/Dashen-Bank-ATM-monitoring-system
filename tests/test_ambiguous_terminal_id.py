"""A terminal ID registered twice must never be silently overwritten.

UAT lost its 4 Kilo test ATM this way: both `ATM-TEST-NCR` and the real
Megenagna row carried terminal ID `PATMT003`. A TID-only CSV import resolved
the key to the first row by atm_id order and rewrote the test ATM's name,
vendor, model and IP with the real branch's data.

The registry read now separates ambiguous terminal IDs, the importer refuses
those rows, and the preview marks them before upload.
"""
from __future__ import annotations

import inspect
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'report-portal'))
sys.path.insert(0, REPO)

from blueprints import admin  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ''

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return list(self.rows)


def test_ambiguous_terminal_is_excluded_from_the_lookup_map():
    cur = FakeCursor([
        ('ATM-TEST-NCR', 'PATMT003'),
        ('TST-0316', 'PATMT003'),
        ('ATM-0001', 'PATML001'),
    ])

    existing, used, ambiguous = admin.registry_key_index(cur)

    assert existing == {'PATML001': 'ATM-0001'}
    assert ambiguous == {'PATMT003': ['ATM-TEST-NCR', 'TST-0316']}
    assert used == {'ATM-TEST-NCR', 'TST-0316', 'ATM-0001'}


def test_terminal_comparison_is_case_insensitive():
    cur = FakeCursor([('ATM-1', ' patmt003 '), ('ATM-2', 'PATMT003')])

    _, _, ambiguous = admin.registry_key_index(cur)

    assert list(ambiguous) == ['PATMT003']


def test_import_refuses_a_row_with_an_ambiguous_terminal_id():
    src = inspect.getsource(admin.atm_import)

    assert 'ambiguous_terminals' in src
    assert 'Terminal ID matches multiple registered ATMs' in src
    assert 'resolve the duplicate before importing' in src


def test_preview_marks_an_ambiguous_terminal_id_as_an_error():
    html = open(os.path.join(REPO, 'report-portal', 'templates',
                             'admin_atm.html')).read()

    assert "data.terminal_ambiguous" in html
    assert "_ambiguous" in html
    assert 'Matches multiple registered ATMs' in html
