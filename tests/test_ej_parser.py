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
    assert len(patterns) >= 8, f"expected >= 8 legacy patterns, got {len(patterns)}"
    covered = set()
    for p in procs:
        if "grok" in p:
            for pat in p["grok"]["patterns"]:
                covered |= _named_captures(pat)
    missing = EXPECTED_FIELDS - covered
    assert not missing, f"pipeline missing expected fields: {sorted(missing)}"


def test_ndc_record_pattern_and_scrub():
    """Live NCR dialect (proven 2026-10-13): ESC-led [020t records, PAN
    scrubbed at ingest before storage."""
    text = json.loads(PIPELINE.read_text())
    blob = json.dumps(text["processors"])
    assert "020t" in blob, "no NDC record pattern"
    assert "MM/dd/yyyy" in blob, "no NDC date format"
    assert "atm.host" in blob and "TESTMACHINE" in blob, "no filename->atm mapping"
    assert "d{6}" in blob and "******" in blob, "no ingest PAN scrub"
    # Control-byte strip must precede grok (else ESC-led lines never match).
    kinds = [next(iter(p)) for p in text["processors"]]
    first_gsub = kinds.index("gsub")
    first_grok = kinds.index("grok")
    assert first_gsub < first_grok, "scrub must run before grok"


def _es_grok_to_python(pattern: str):
    """Translate a pure-regex ES named-capture pattern to Python re.

    Only for patterns without %{MACRO} references (the NDC record pattern).
    Dots in names become underscores (Python restriction, ES allows dots).
    Uses FULLMATCH: ES ingest grok requires the pattern to cover the whole
    field, so prefix-only matching hides multi-line failures (proven live:
    single-line records parsed while receipt-bearing multi-line records
    fell through silently).
    """
    assert "%{" not in pattern, "macro patterns not translatable"
    return re.compile(re.sub(r"\(\?<([A-Za-z0-9_.]+)>",
                             lambda m: "(?P<" + m.group(1).replace(".", "_") + ">",
                             pattern))


def test_ndc_record_pattern_matches_live_shape():
    """Regression: the record separator is '*' between DATE and TIME
    ([020t*SEQ*MM/DD/YYYY*HH:MM*TEXT) — a space-joined pattern silently
    matches nothing, which is exactly the 2026-10-13 blob recurrence."""
    doc = json.loads(PIPELINE.read_text())
    rec = next(
        p["grok"]["patterns"][0] for p in doc["processors"]
        if "grok" in p and any("020t" in pat for pat in p["grok"]["patterns"]))
    rx = _es_grok_to_python(rec)
    # Multi-line record (marker + receipt continuations): must FULL-match
    # with the text spanning newlines — prefix matching hides this class.
    multi = ("[020t*571*10/08/2026*14:59*\n     *TRANSACTION START*\n"
             "␛[020t CARD INSERTED\n 14:59:18 ATR RECEIVED T=0\n"
             "    DASHEN BANK\n    WITHDRAWAL\n    TRANSACTION DECLINED\n    56")
    m = rx.fullmatch(multi)
    assert m, "NDC record pattern does not full-match a multi-line record"
    assert m.group("event_time") == "10/08/2026*14:59"
    assert "WITHDRAWAL" in m.group("ndc_text")
    m2 = rx.fullmatch("[020t*559*10/08/2026*11:33*TRANSACTION END")
    assert m2 and m2.group("transaction_sequence") == "559"


def test_pipeline_has_trim_processors():
    doc = json.loads(PIPELINE.read_text())
    trimmed = set()
    for p in doc["processors"]:
        if "trim" in p:
            trimmed.add(p["trim"]["field"])
    # At minimum the status field must be trimmed (our trailing-space bug).
    assert "transaction.status" in trimmed, "no trim processor for transaction.status"
    assert "card.masked" in trimmed, "no trim processor for card.masked"


def test_amount_converted_to_float():
    """transaction.amount must index numeric (template formats it with
    :,.2f — a string amount 500s the whole EJ Search page, seen live
    2026-10-13). Convert runs after trims (trim needs the raw string)."""
    doc = json.loads(PIPELINE.read_text())
    procs = doc["processors"]
    kinds = [next(iter(p)) for p in procs]
    conv = [p["convert"] for p in procs if "convert" in p
            and p["convert"].get("field") == "transaction.amount"]
    assert conv and conv[0].get("type") == "float", "no float convert for amount"
    last_trim = max(i for i, p in enumerate(procs) if "trim" in p)
    conv_at = kinds.index("convert")
    assert conv_at > last_trim, "convert must run after trims"


def test_component_category_and_response_code():
    """Review-driven (2026-10-14): component ACTIVATED lines are COMPONENT
    (not TXN), decline codes land in fault.code. Structural assertions on
    the pipeline (live _simulate proves behavior on DATA_VM)."""
    doc = json.loads(PIPELINE.read_text())
    procs = doc["processors"]
    blob = json.dumps(procs)
    assert "component.name" in blob, "no component extraction"
    assert "fault.code" in blob, "no response-code extraction"
    comp_at = next(i for i, p in enumerate(procs)
                   if "grok" in p and "component.name" in json.dumps(p))
    txn_default_at = next(i for i, p in enumerate(procs)
                          if "set" in p and p["set"].get("value") == "TXN")
    assert comp_at < txn_default_at, "component must categorize before TXN default"


def test_amount_kept_only_for_money_types():
    """Version strings are not cash: CAM 03.00.00 parsed as ETB 3.00 and
    APTRA 05.00.01 as ETB 5.00 on STATE rows (seen live 2026-10-14). The
    remove-gate keeps amounts only on WITHDRAWAL/TRANSFER records."""
    doc = json.loads(PIPELINE.read_text())
    procs = doc["processors"]
    gates = [p["remove"] for p in procs if "remove" in p
             and p["remove"].get("field") == "transaction.amount"]
    assert gates, "no amount gate"
    cond = gates[0].get("if", "")
    assert "WITHDRAWAL" in cond and "TRANSFER" in cond, \
        "amount gate must allow money-moving types"


def test_event_time_parsed_as_eat():
    """Journal wall time is EAT: without an explicit timezone the date
    processor reads it as UTC and the portal's +3 display double-shifts
    every row (same instant shown as 17:35 and 20:35 on one screen)."""
    doc = json.loads(PIPELINE.read_text())
    dates = [p["date"] for p in doc["processors"] if "date" in p]
    assert dates, "no date processor"
    assert dates[0].get("timezone") == "Africa/Addis_Ababa", \
        "event_time must be interpreted as EAT"


def test_state_rule_precedes_txn_default():
    """STATE categorization must win over the TXN default (ordering
    contract — silent miscategorization otherwise)."""
    doc = json.loads(PIPELINE.read_text())
    procs = doc["processors"]
    sets = [(i, p["set"]) for i, p in enumerate(procs) if "set" in p]
    state_at = next(i for i, s in sets
                    if s.get("value") == "STATE" and "SST" in s.get("if", ""))
    txn_at = next(i for i, s in sets
                  if s.get("value") == "TXN" and "transaction" in s.get("if", ""))
    assert state_at < txn_at, "STATE rule must precede TXN default"
    txn_if = next(s["if"] for _, s in sets if s.get("value") == "TXN")
    assert "transaction?.type" in txn_if and "transaction?.status" in txn_if, \
        "TXN default must require type or status, not bare sequence"


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
