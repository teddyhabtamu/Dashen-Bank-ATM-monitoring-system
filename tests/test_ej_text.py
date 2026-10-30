"""Unit tests for report-portal/ej_text.py (dependency-free, always runs).

The LOG DETAILS box shows this module's output instead of raw wire text.
Redaction rule for samples: structure must be real, digits masked.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'report-portal'))

from ej_text import humanize_ndc_text


def test_state_line_reads_clean():
    out = humanize_ndc_text('[020t*715*10/13/2026*06:13*[05pSST OFF-LINE[00p')
    assert out == 'SST OFF-LINE', repr(out)


def test_transaction_keeps_words_and_order():
    out = humanize_ndc_text(
        '[020t*571*10/08/2026*14:59*\n WITHDRAWAL\n TRANSACTION DECLINED\n 56')
    assert 'WITHDRAWAL' in out and 'DECLINED' in out and '56' in out
    assert '[020t' not in out and '[05p' not in out


def test_ansi_and_wrappers_stripped():
    out = humanize_ndc_text('\x1b[020t*1*2*3*4*\x1b[00p hello \x1b[12p')
    assert '\x1b' not in out
    assert 'hello' in out


def test_empty_and_garbage_never_raise():
    assert humanize_ndc_text('') == ''
    assert humanize_ndc_text(None) == ''
    assert isinstance(humanize_ndc_text('[020t***'), str)


def test_never_returns_blank_for_content():
    # Unknown shapes fall back to the raw text, never an empty box.
    assert humanize_ndc_text('[0r(1]2[000pXYZ') != ''
