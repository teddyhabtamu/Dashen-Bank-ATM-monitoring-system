"""Unit tests for filebeat.yml EJ multiline rule (shipper-side splitting).

The rule decides record boundaries BEFORE the ingest pipeline ever runs, so
a wrong rule silently re-shapes every document (proven twice live:
date-pattern glued whole files; bare-marker fractured records into dateless
fragments). These tests simulate the rule per line: split = starts new
event, glue = appends to the running record.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BEAT = REPO / "filebeat.yml"


def _rule():
    doc = yaml.safe_load(BEAT.read_text())
    ej = next(i for i in doc["filebeat.inputs"]
              if "/var/log/atm-ej/" in str(i.get("paths", [])))
    assert ej.get("multiline.negate") is True
    assert ej.get("multiline.match") == "after"
    return re.compile(ej["multiline.pattern"])


def _split(lines):
    rx = _rule()
    return ["split" if rx.search(ln) else "glue" for ln in lines]


def test_rule_loads():
    assert _rule() is not None


def test_only_real_pulled_journals_are_shipped():
    doc = yaml.safe_load(BEAT.read_text())
    ej = next(i for i in doc["filebeat.inputs"]
              if "/var/log/atm-ej/" in str(i.get("paths", [])))
    assert ej["paths"] == ["/var/log/atm-ej/*_EJDATA.log"], (
        'filebeat must not index local demo/fixture files such as GRG-001.log')


def test_full_marker_splits():
    assert _split(["[020t*571*10/08/2026*14:59*"]) == ["split"]
    assert _split(["[020t*566*10/08/2026*14:56*"]) == ["split"]


def test_continuation_marker_glues():
    # Starts with [020t but carries no *SEQ*DATE*TIME fields: dateless
    # fragments like these filled the index with 790 ingest-stamped rows.
    assert _split(["[020t CARD INSERTED"]) == ["glue"]
    assert _split(["[020t 17:34:52 CARD TAKEN"]) == ["glue"]
    assert _split(["[020t     *1177*1*P*21,M-"]) == ["glue"]
    assert _split(["[020t     *    *1*P*21,M-"]) == ["glue"]


def test_plain_lines_glue():
    assert _split([" 11:32:41 ATR RECEIVED T=0"]) == ["glue"]
    assert _split(["    WITHDRAWAL"]) == ["glue"]


def test_grg_timestamp_starts_a_record():
    assert _split(["2026-10-24 10:00:00 | GRG-001 | TID006 | VENDOR=GRG | "
                   "TXN_CODE=WITHDRAWAL | SEQ=1 | RESP_CODE=APPROVED"]) == ["split"]
    assert _split(["2026-10-24 10:00:00.123 TRANSACTION START"]) == ["split"]
    assert _split(["    10:00:01 continuation"]) == ["glue"]


def test_sequence_splits_records():
    lines = ["[020t*571*10/08/2026*14:59*",
             "     *TRANSACTION START*",
             "[020t CARD INSERTED",
             "[020t*572*10/08/2026*14:59*"]
    assert _split(lines) == ["split", "glue", "glue", "split"]
