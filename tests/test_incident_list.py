"""GLPI range responses for the ticket list.

The portal asks for tickets 0-99. Once an instance holds more than 100
tickets, GLPI answers 206 Partial Content with that first page. Only 200 was
treated as success, so every refresh logged a warning and fell back to stale
data: new tickets stopped appearing in the portal.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import blueprints.incidents as inc  # noqa: E402


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_a_partial_ticket_list_is_a_success(monkeypatch, caplog):
    page = [{'id': 100}, {'id': 99}]
    monkeypatch.setattr(inc.requests, 'get', lambda *a, **k: _Resp(206, page))

    assert inc._tickets('session-token') == page
    assert 'Ticket.list' not in caplog.text


def test_a_failed_ticket_list_is_still_reported(monkeypatch, caplog):
    monkeypatch.setattr(inc.requests, 'get', lambda *a, **k: _Resp(500))

    assert inc._tickets('session-token') is None
    assert 'GLPI Ticket.list status=500' in caplog.text
