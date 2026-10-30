"""Unit tests for scripts/ensure_reachability_layer.py (dependency-free).

v1 (icmpping everywhere) died on bank reality: ICMP is dropped network-wide
and the NAT forwards TCP, not protocols. v2 checks what is actually
reachable per template — these contracts pin that decision so no future
edit silently reverts to ping-everywhere.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import ensure_reachability_layer as reach


def test_ncr_uses_forwarded_tcp_port():
    kind, name, key, desc, prio = reach.CHECKS["Dashen Bank ATM Hardware"]
    assert kind == 'tcp'
    assert key == 'net.tcp.service[tcp,,10050]'
    assert prio == 2  # Average: context for humans, not a ticket


def test_grg_keeps_ping_tracker():
    kind, name, key, desc, prio = reach.CHECKS["Dashen Bank ATM Hardware - GRG"]
    assert kind == 'icmp'
    assert key == 'icmpping'
    assert prio == 4  # High: the firing ticket tracks the firewall ask


def test_trigger_expression_shape():
    assert (reach.check_expr('Dashen Bank ATM Hardware',
                             'net.tcp.service[tcp,,10050]')
            == 'last(/Dashen Bank ATM Hardware/net.tcp.service[tcp,,10050])=0')


def test_v1_leftovers_scoped_to_ncr():
    key, desc = reach.RETIRED_NCR
    assert key == 'icmpping'
    assert 'ping' in desc.lower()
