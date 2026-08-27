#!/usr/bin/env bash
# Capture a full SNMP walk from a REAL test ATM and save it for OID mapping.
# Run this during UAT once the bank provides the test-ATM IP + community string.
#
# Usage:
#   ./capture_walk.sh <VENDOR:NCR|GRG> <ATM_IP> <COMMUNITY> [OUTPUT]
#
# Example (from docs/UAT_Migration_Guide.md Step F):
#   ./capture_walk.sh NCR 172.22.248.4 dashen_sim
#   ./capture_walk.sh GRG 172.23.248.6 dashen_sim
#
# Output: config/snmp/real_atm_snmpwalk_<VENDOR>.txt
# Next:   map the real OIDs into config/snmp/oid_mapping.csv (checklist Step 0.3),
#         then clone the Zabbix SNMP templates (Step 0.4) and register the host
#         with sync_atms_to_zabbix.py --apply.
set -euo pipefail

VENDOR="${1:-NCR}"
IP="${2:?ATM IP required}"
COMMUNITY="${3:-dashen_sim}"
OUT="${4:-config/snmp/real_atm_snmpwalk_${VENDOR}.txt}"

mkdir -p "$(dirname "$OUT")"
echo "Walking ${VENDOR} ATM ${IP} (community '${COMMUNITY}') -> ${OUT}"

# Standard MIB-2 tree + vendor enterprise tree
snmpwalk -v2c -c "$COMMUNITY" -On "$IP" .1.3.6.1.2.1 >> "$OUT" 2>&1 || true
snmpwalk -v2c -c "$COMMUNITY" -On "$IP" .1.3.6.1.4.1 >> "$OUT" 2>&1 || true

echo "Saved $(wc -l < "$OUT") lines to $OUT"
echo "Now edit config/snmp/oid_mapping.csv (columns: sim_oid,real_oid,item_name,vendor,data_type)"
