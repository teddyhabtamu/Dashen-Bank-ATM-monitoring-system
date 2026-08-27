#!/usr/bin/env bash
# UAT preflight — verifies the deployed UAT stack is healthy BEFORE sign-off.
# Run on UAT-01 (or from a host that can reach the UAT endpoints).
#
# Override endpoints via env vars if needed:
#   OS_HOST=localhost:9200 RP_HOST=localhost:8888 GLPI_HOST=localhost:8082 \
#   GRAFANA_HOST=localhost:3000 ZABBIX_HOST=localhost:8080 ./uat_preflight.sh
set -uo pipefail

OS_HOST="${OS_HOST:-localhost:9200}"
RP_HOST="${RP_HOST:-localhost:8888}"
GLPI_HOST="${GLPI_HOST:-localhost:8082}"
GRAFANA_HOST="${GRAFANA_HOST:-localhost:3000}"
ZABBIX_HOST="${ZABBIX_HOST:-localhost:8080}"

PASS=0; FAIL=0
ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "== UAT preflight =="

# 1. OpenSearch reachable
if curl -s -m 5 "http://$OS_HOST/" >/dev/null; then ok "OpenSearch reachable ($OS_HOST)"; else bad "OpenSearch unreachable ($OS_HOST)"; fi

# 2. EJ ingest pipeline present
if curl -s -m 5 "http://$OS_HOST/_ingest/pipeline/atm_ej_parser" | grep -q '"atm_ej_parser"'; then
  ok "EJ ingest pipeline present"; else bad "EJ ingest pipeline MISSING (load config/opensearch/atm_ej_parser.json)"; fi

# 3. EJ index template present
if curl -s -m 5 "http://$OS_HOST/_index_template/atm-ej-live" | grep -q '"atm-ej-live"'; then
  ok "EJ index template present"; else bad "EJ index template MISSING (load config/opensearch/ej_live_index_template.json)"; fi

# 4. Report Portal reachable (expect 200 or 302 to login)
code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" "http://$RP_HOST/")
if [ "$code" = "200" ] || [ "$code" = "302" ]; then ok "Report Portal reachable ($code)"; else bad "Report Portal unreachable ($code)"; fi

# 5. GLPI reachable
code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" "http://$GLPI_HOST/")
if [ "$code" = "200" ] || [ "$code" = "302" ]; then ok "GLPI reachable ($code)"; else bad "GLPI unreachable ($code)"; fi

# 6. Grafana health
if curl -s -m 5 "http://$GRAFANA_HOST/api/health" | grep -q '"database":"ok"\|"commit"'; then
  ok "Grafana healthy"; else bad "Grafana not healthy"; fi

# 7. Zabbix web reachable
code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" "http://$ZABBIX_HOST/")
if [ "$code" = "200" ] || [ "$code" = "302" ]; then ok "Zabbix web reachable ($code)"; else bad "Zabbix web unreachable ($code)"; fi

# 8. Card-mask consistency in DB (0 legacy 20-char rows expected)
if command -v psql >/dev/null 2>&1 && [ -n "${DB_PASS:-}" ]; then
  bad_rows=$(psql -t -A -h "${DB_HOST:-postgres}" -U "${DB_USER:-zabbix}" -d "${DB_NAME:-zabbix}" \
    -c "SELECT count(*) FROM atm_transactions WHERE card_masked ~ '^[0-9A-Za-z]{4}\*{12}[0-9A-Za-z]{4}\$';" 2>/dev/null | tail -1)
  if [ "${bad_rows:-0}" = "0" ]; then ok "card_masked consistent (0 legacy rows)"; else bad "card_masked has $bad_rows legacy 20-char rows"; fi
else
  echo "  [SKIP] card_masked DB check (psql/DB_PASS not available here)"
fi

echo "== preflight done: $PASS pass, $FAIL fail =="
[ "$FAIL" -eq 0 ]
