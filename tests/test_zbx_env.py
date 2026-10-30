"""Unit tests for scripts/zbx_env.py (dependency-free, no env mutation).

The loader ends the export-every-shell era: forgotten exports burned failed
logins into the shared service account, so this parser is load-bearing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import zbx_env


def test_basic_and_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('A=1\nB="two words"\nC=\'three\'\n')
    assert zbx_env.parse_dotenv(str(p)) == {'A': '1', 'B': 'two words',
                                            'C': 'three'}


def test_comments_blanks_export_and_junk(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\n\n   \nexport D=4\nNOEQUALS\nE = spaced \n')
    assert zbx_env.parse_dotenv(str(p)) == {'D': '4', 'E': 'spaced'}


def test_interpolation_skipped():
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.env',
                                      delete=False) as f:
        f.write('ZABBIX_PASS=${ZBX_PASS}\nZBX_PASS=secret\n')
        name = f.name
    try:
        assert zbx_env.parse_dotenv(name) == {'ZBX_PASS': 'secret'}
    finally:
        os.unlink(name)


def test_missing_file_is_empty():
    assert zbx_env.parse_dotenv('/nonexistent-xyz/.env') == {}
