#!/usr/bin/env python3
"""
Real SNMP responder for the ATM simulator.

Serves an ATM's OID values over UDP (on the same per-ATM port the HTTP
simulator uses) so Zabbix can poll it with genuine **SNMP agent** items —
not HTTP agent. This makes the collection path identical to production
(real ATMs are SNMP-polled on UDP 161); the only differences at cutover are
the target port (161 vs the sim port) and the OID tree (real NCR/GRG MIB).

Uses pysnmp v6 (asyncio carrier). A small in-memory MIB controller answers
GET/GETNEXT for the OIDs in `oid_map` with the live `state` values.

Value types:
  - integers / bools -> Integer32
  - floats           -> Gauge32 (truncated)
Only numeric metrics are registered (strings like atm_id are not polled).
"""

import threading
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdrsp
from pysnmp.entity.rfc3413 import context as snmp_context
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.smi import instrum
from pysnmp.proto.api import v2c
from pysnmp.proto.rfc1902 import Integer32, Gauge32, ObjectName


COMMUNITY = 'dashen_sim'

# Dashen private enterprise root for the simulator's synthetic MIB. Real ATMs
# use their vendor's enterprise OID (e.g. NCR .1.3.6.1.4.1.37513); at cutover
# the OID tree is remapped but the collection mechanism (SNMP GET) is identical.
MIB_ROOT = (1, 3, 6, 1, 4, 1, 99999)


class _ATMInstrumentation(instrum.AbstractMibInstrumController):
    """Answers SNMP GET/GETNEXT for a fixed OID->value map backed by a live dict."""

    def __init__(self, oid_map, state):
        super().__init__()
        self._oid_map = {}  # tuple(ints) -> state key
        for oid, key in oid_map.items():
            full = MIB_ROOT + tuple(int(p) for p in oid.split('.'))
            self._oid_map[full] = key
        self._state = state

    def _value_for(self, oid_tuple):
        key = self._oid_map.get(oid_tuple)
        if key is None:
            return None
        raw = self._state.get(key)
        if isinstance(raw, bool):
            return Integer32(1 if raw else 0)
        try:
            f = float(raw)
        except (TypeError, ValueError):
            return Integer32(0)
        if f != int(f) or abs(f) > 2_147_483_647:
            return Gauge32(int(f))
        return Integer32(int(f))

    def readVars(self, varBinds, acInfo=None):
        out = []
        for name, val in varBinds:
            oid_tuple = tuple(name)
            v = self._value_for(oid_tuple)
            if v is None:
                out.append((name, val))
            else:
                out.append((ObjectName(oid_tuple), v))
        return out

    def readNextVars(self, varBinds, acInfo=None):
        out = []
        for name, val in varBinds:
            oid_tuple = tuple(name)
            nxt = None
            for reg in self._oid_map:
                if reg > oid_tuple and (nxt is None or reg < nxt):
                    nxt = reg
            if nxt is None:
                out.append((name, val))
                continue
            v = self._value_for(nxt)
            if v is None:
                out.append((name, val))
                continue
            out.append((ObjectName(nxt), v))
        return out


def start_snmp(atm_id, port, oid_map, state):
    """Start an SNMP responder for one ATM on UDP `port`. Returns True on success."""
    try:
        snmp_engine = engine.SnmpEngine()
        config.addV1System(snmp_engine, COMMUNITY, COMMUNITY)
        config.addSocketTransport(
            snmp_engine,
            udp.domainName,
            udp.UdpTransport().openServerMode(('0.0.0.0', port)),
        )
        instrum_ctrl = _ATMInstrumentation(oid_map, state)
        snmp_ctx = snmp_context.SnmpContext(snmp_engine)
        # Override the default (empty) context so GET on contextName '' uses our
        # synthetic MIB instead of pysnmp's built-in MIB controller.
        snmp_ctx.contextNames[b''] = instrum_ctrl
        cmdrsp.GetCommandResponder(snmp_engine, snmp_ctx)
        cmdrsp.NextCommandResponder(snmp_engine, snmp_ctx)
        cmdrsp.SetCommandResponder(snmp_engine, snmp_ctx)
        threading.Thread(
            target=snmp_engine.transportDispatcher.runDispatcher, daemon=True
        ).start()
        print(f"[SNMP] {atm_id} SNMP responder on UDP :{port}")
        return True
    except Exception as e:
        print(f"[SNMP] {atm_id} FAILED to start on :{port}: {e}")
        return False
