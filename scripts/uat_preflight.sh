#!/usr/bin/env bash
# UAT Phase 0 preflight — run on UAT-01 before executing the runbook.
# Fails fast if a prerequisite is missing so you don't discover it mid-cutover.
set -u
ZBX_HOST="${ZABBIX_HOST:-172.26.208.176}"
ZBX_PORT="${ZABBIX_PORT:-8080}"
DATA_VM_IP="${DATA_VM_IP:-172.26.21.50}"
fail=0

ok()   { echo "  [OK]   $1"; }
bad()  { echo "  [FAIL] $1"; fail=1; }

echo "== UAT preflight =="
echo "-- inventory (docker) --"
command -v docker >/dev/null 2>&1 && ok "docker present" || bad "docker missing"

echo "-- Zabbix API reachable --"
if curl -s --max-time 5 -o /dev/null -w "%{http_code}" "http://${ZBX_HOST}:${ZBX_PORT}/api_jsonrpc.php" | grep -q 200; then
  ok "Zabbix API on ${ZBX_HOST}:${ZBX_PORT}"
else
  bad "Zabbix API unreachable on ${ZBX_HOST}:${ZBX_PORT}"
fi

echo "-- seeded test ATMs --"
if docker exec zabbix-db psql -U zabbix -d zabbix -tAc "SELECT count(*) FROM atm_locations WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');" 2>/dev/null | grep -q 2; then
  ok "ATM-TEST-NCR / ATM-TEST-GRG seeded"
else
  bad "test ATMs not seeded (run setup + seed_uat_atms.sql)"
fi

echo "-- SNMP loader present --"
command -v snmpwalk >/dev/null 2>&1 && ok "snmpwalk present" || bad "snmpwalk missing (apt install snmp)"

echo "-- OpenSearch reachable from UAT-01 --"
if curl -s --max-time 5 -o /dev/null -w "%{http_code}" "http://${DATA_VM_IP}:9200" | grep -q 200; then
  ok "OpenSearch on ${DATA_VM_IP}:9200"
else
  bad "OpenSearch unreachable on ${DATA_VM_IP}:9200"
fi

echo "-- filebeat syslog listener --"
if ss -lun 2>/dev/null | grep -q ":514"; then
  ok "UDP/514 listening (filebeat)"
else
  bad "nothing listening on UDP/514 (filebeat syslog input)"
fi

echo "-- templates not accidentally remapped already --"
if grep -rq "1.3.6.1.4.1.99999" config/zabbix/template_*.xml 2>/dev/null; then
  ok "templates still on simulator OID root (expected before cutover)"
else
  echo "  [WARN] no simulator OIDs found — templates may already be remapped"
fi

echo "-- ENABLE_SIMULATORS must be false for UAT --"
if grep -q "^ENABLE_SIMULATORS=.*true" .env 2>/dev/null; then
  bad "ENABLE_SIMULATORS is true in .env — simulators would run in UAT"
else
  ok "ENABLE_SIMULATORS is false (real ATM polling)"
fi

echo "-- Zabbix hosts registered for the test ATMs --"
if docker exec zabbix-db psql -U zabbix -d zabbix -tAc "SELECT count(*) FROM hosts WHERE name LIKE 'ATM-TEST%';" 2>/dev/null | grep -q 2; then
  ok "Zabbix hosts for ATM-TEST-NCR / ATM-TEST-GRG present"
else
  echo "  [WARN] expected 2 Zabbix hosts named ATM-TEST* (run sync_atms_to_zabbix.py --apply)"
fi

echo "-- real OID mapping captured --"
if [ -f config/snmp/oid_mapping.csv ]; then
  lines=$(wc -l < config/snmp/oid_mapping.csv 2>/dev/null || echo 0)
  if [ "$lines" -gt 1 ]; then
    ok "oid_mapping.csv has $lines rows (real walk captured)"
  else
    echo "  [WARN] oid_mapping.csv is still the stub — run capture_walk.sh then fill real OIDs"
  fi
else
  bad "config/snmp/oid_mapping.csv missing"
fi

echo "-- template remap dry-run (guardrail preview) --"
if command -v python3 >/dev/null 2>&1 && [ -f scripts/generate_zabbix_templates.py ]; then
  python3 scripts/generate_zabbix_templates.py --dry-run || echo "  [WARN] generator reported issues (guardrail will block the real run until resolved)"
fi

echo "-- EJ index reachable in OpenSearch --"
ej_idx=$(curl -s --max-time 5 "http://${DATA_VM_IP}:9200/_cat/indices/atm-*?h=index" 2>/dev/null | head -1)
if [ -n "$ej_idx" ]; then
  ok "EJ index present: $ej_idx"
else
  echo "  [WARN] no atm-* index in OpenSearch — EJ ingestion may not be running"
fi

echo "-- status_map.json + cassette_capacity.json present --"
[ -f config/status_map.json ] && ok "status_map.json present" || bad "config/status_map.json missing"
[ -f config/cassette_capacity.json ] && ok "cassette_capacity.json present" || bad "config/cassette_capacity.json missing"

echo "== preflight $( [ $fail -eq 0 ] && echo PASS || echo FAILED ) =="
exit $fail
