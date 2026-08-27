"""UAT-prep tests for the EJ structured-schema pipeline.

Dependency-free: validates config/opensearch/atm_ej_parser.json (the artifact
OpenSearch actually applies) so EJ Search keeps working in UAT. If the report
portal's parser function can be imported, it is also smoke-tested; otherwise
that sub-test is skipped (does not fail the suite).
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "config" / "opensearch" / "atm_ej_parser.json"

# Fields the Report Portal / dashboards depend on.
EXPECTED_FIELDS = {
    "atm.id", "terminal.id", "event.category", "transaction.type",
    "transaction.sequence", "card.masked", "transaction.amount",
    "transaction.currency", "transaction.status", "transaction.auth_code",
    "vendor", "fault.code", "fault.desc",
}


def _named_captures(pattern: str):
    names = set()
    names |= set(re.findall(r"%\{\w+:([A-Za-z0-9_.]+?)(?::\w+)?\}", pattern))
    names |= set(re.findall(r"\(\?<([A-Za-z0-9_.]+)>", pattern))
    return names


def test_pipeline_file_exists():
    assert PIPELINE.exists(), f"missing {PIPELINE}"


def test_pipeline_has_8_patterns_and_expected_fields():
    doc = json.loads(PIPELINE.read_text())
    procs = doc["processors"]
    grok = next(p for p in procs if "grok" in p)
    patterns = grok["grok"]["patterns"]
    assert len(patterns) == 8, f"expected 8 patterns, got {len(patterns)}"
    covered = set()
    for p in patterns:
        covered |= _named_captures(p)
    missing = EXPECTED_FIELDS - covered
    assert not missing, f"pipeline missing expected fields: {sorted(missing)}"


def test_pipeline_has_trim_processors():
    doc = json.loads(PIPELINE.read_text())
    trimmed = set()
    for p in doc["processors"]:
        if "trim" in p:
            trimmed.add(p["trim"]["field"])
    # At minimum the status field must be trimmed (our trailing-space bug).
    assert "transaction.status" in trimmed, "no trim processor for transaction.status"
    assert "card.masked" in trimmed, "no trim processor for card.masked"


def test_parser_function_if_importable():
    """Smoke-test the portal's own parser if it imports without a live app."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ej_search_standalone",
            REPO / "report-portal" / "blueprints" / "ej_search.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        parse = mod._parse_ej_log_line
    except Exception as e:  # app/not importable in this env — skip, don't fail
        import pytest
        pytest.skip(f"parser not importable here ({e})")

    ncr = ("2026-08-26 13:22:47 | ATM-002 | TID002 | TXN | WITHDRAWAL | "
           "SEQ=284461 | CARD=************0123 | AMOUNT=2000.00 | "
           "CURRENCY=ETB | STATUS=APPROVED | AUTH=803616")
    out = parse(ncr)
    assert out.get("transaction", {}).get("status") == "APPROVED"
    assert " " not in out.get("transaction", {}).get("status", "")
    assert out.get("card", {}).get("masked") == "************0123"
