"""Unit tests for scripts/ensure_icmp_layer.py (dependency-free).

The ping layer splits power-cut from agent-crash fleet-wide, so its
naming/expression contract is load-bearing: a renamed key silently orphans
the portal's future ping-aware states.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import ensure_icmp_layer as icmp


def test_item_contract():
    assert icmp.ITEM_KEY == 'icmpping'
    assert icmp.ITEM_NAME == 'ATM Network — Ping'


def test_trigger_contract():
    assert icmp.TRIGGER_DESC == 'ATM Network — Unreachable by ping'
    expr = icmp.ping_trigger_expr('Dashen Bank ATM Hardware')
    assert expr == 'last(/Dashen Bank ATM Hardware/icmpping)=0'


def test_both_vendors_covered():
    assert icmp.TEMPLATES == ["Dashen Bank ATM Hardware",
                              "Dashen Bank ATM Hardware - GRG"]
