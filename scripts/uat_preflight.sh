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
if curl -s -o /dev/null -w "%{http_code}" "http://${ZBX_HOST}:${ZBX_PORT}/api_jsonrpc.php" | grep -q 200; then
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
if curl -s -o /dev/null -w "%{http_code}" "http://${DATA_VM_IP}:9200" | grep -q 200; then
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

echo "== preflight $( [ $fail -eq 0 ] && echo PASS || echo FAILED ) =="
exit $fail
