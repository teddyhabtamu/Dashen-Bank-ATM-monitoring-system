"""Pure helpers used by the Sonar remediation.

These do not touch Flask or the database, so they can be tested directly.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

from log_safety import safe_log  # noqa: E402
from net_url import service_url  # noqa: E402


def test_safe_log_flattens_newlines_and_control_bytes():
    assert safe_log('alice\nfake 2026-01-01 ERROR') == 'alice fake 2026-01-01 ERROR'
    assert safe_log('atm\r\n001\x00') == 'atm  001 '


def test_safe_log_flattens_unicode_line_separators():
    assert safe_log('one\u2028two\u2029three') == 'one two three'


def test_safe_log_caps_length():
    assert len(safe_log('x' * 500, limit=20)) == 20


def test_service_url_keeps_an_explicit_scheme(monkeypatch):
    monkeypatch.setenv('ES_SCHEME', 'https')
    assert service_url('https://search.example:9200', '/_search', 'ES_SCHEME') == \
        'https://search.example:9200/_search'


def test_service_url_uses_the_configured_scheme(monkeypatch):
    monkeypatch.setenv('ES_SCHEME', 'https')
    assert service_url('search:9200', '/_search', 'ES_SCHEME') == \
        'https://search:9200/_search'
    monkeypatch.delenv('ES_SCHEME')
    assert service_url('search:9200', '/_search', 'ES_SCHEME') == \
        'http://search:9200/_search'
