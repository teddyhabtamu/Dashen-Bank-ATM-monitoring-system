#!/usr/bin/env bash
# Capture a full SNMP walk from a REAL test ATM and save it for OID mapping.
# Run this during UAT once the bank provides the test-ATM IP, community, and SNMP version.
#
# Usage:
#   ./capture_walk.sh <VENDOR:NCR|GRG> <ATM_IP> <COMMUNITY> [OPTIONS]
#
# Examples:
#   ./capture_walk.sh NCR 172.23.248.4 public
#   ./capture_walk.sh NCR 172.23.248.4 '' -v3 -u snmpuser -a SHA -A authpass -x AES -X privpass -l authPriv
#
# Options:
#   -v, --version VERSION   SNMP version: 2c (default) or 3
#   -u USER                 SNMPv3 security name
#   -a PROTO                SNMPv3 auth protocol (SHA|MD5)
#   -A PASS                 SNMPv3 auth password
#   -x PROTO                SNMPv3 priv protocol (AES|DES)
#   -X PASS                 SNMPv3 priv password
#   -l LEVEL                SNMPv3 security level (noAuthNoPriv|authNoPriv|authPriv)
#   -o, --output FILE       output file (default config/snmp/real_atm_snmpwalk_<VENDOR>.txt)
set -euo pipefail

VENDOR="${1:?VENDOR required}"; shift
IP="${1:?ATM IP required}"; shift
COMMUNITY="${1:-}"; shift || true

VERSION=2c
V3OPTS=()
OUTPUT="config/snmp/real_atm_snmpwalk_${VENDOR}.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--version) VERSION="$2"; shift 2;;
    -u) V3OPTS+=(-u "$2"); shift 2;;
    -a) V3OPTS+=(-a "$2"); shift 2;;
    -A) V3OPTS+=(-A "$2"); shift 2;;
    -x) V3OPTS+=(-x "$2"); shift 2;;
    -X) V3OPTS+=(-X "$2"); shift 2;;
    -l) V3OPTS+=(-l "$2"); shift 2;;
    -o|--output) OUTPUT="$2"; shift 2;;
    *) echo "Unknown argument: $1" >&2; exit 1;;
  esac
done

mkdir -p "$(dirname "$OUTPUT")"
echo "Walking ${VENDOR} ATM ${IP} (SNMP v${VERSION}) -> ${OUTPUT}"

if [[ "$VERSION" == "3" ]]; then
  snmpwalk -v3 "${V3OPTS[@]}" -On "$IP" .1.3.6.1.2.1 >> "$OUTPUT" 2>&1 || true
  snmpwalk -v3 "${V3OPTS[@]}" -On "$IP" .1.3.6.1.4.1 >> "$OUTPUT" 2>&1 || true
else
  C="${COMMUNITY:-dashen_sim}"
  snmpwalk -v2c -c "$C" -On "$IP" .1.3.6.1.2.1 >> "$OUTPUT" 2>&1 || true
  snmpwalk -v2c -c "$C" -On "$IP" .1.3.6.1.4.1 >> "$OUTPUT" 2>&1 || true
fi

echo "Saved $(wc -l < "$OUTPUT" 2>/dev/null || echo 0) lines to $OUTPUT"
echo "Now edit config/snmp/oid_mapping.csv (columns: sim_oid,real_oid,item_name,vendor,data_type)"
