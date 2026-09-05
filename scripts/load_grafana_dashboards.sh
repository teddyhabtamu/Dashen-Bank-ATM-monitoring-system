#!/usr/bin/env bash
# Import the repo's ATM dashboards into Grafana via the HTTP API.
# (File-provisioning the dashboards/ dir is avoided on purpose: the Grafana
# image lacks provisioning/dashboards/ and nested bind-mounts fail there.)
# Idempotent: re-running overwrites dashboards with the same titles.
#
# Usage (on UAT-01, Grafana admin password from .env):
#   GRAFANA_ADMIN_PASSWORD=<from .env> ./scripts/load_grafana_dashboards.sh
#   GRAFANA_URL=http://localhost:3000 GRAFANA_ADMIN_PASSWORD=... ./scripts/load_grafana_dashboards.sh
set -uo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
: "${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD (see .env)}"
DIR="$(dirname "$0")/../config/grafana/dashboards"
FOLDER="ATM Monitoring"
PASS=0; FAIL=0

auth() { curl -s --max-time 15 -u "$ADMIN_USER:$GRAFANA_ADMIN_PASSWORD" "$@"; }

# datasource name -> uid map (EJ dashboard uses the DS_EJ-ELASTICSEARCH input
# name, which we point at our EJ-OpenSearch datasource explicitly)
DS_JSON=$(auth "$GRAFANA_URL/api/datasources")
uid_of() { echo "$DS_JSON" | python3 -c "import json,sys; print(next((d['uid'] for d in json.load(sys.stdin) if d['name']=='$1'), ''))"; }
ZBX_UID=$(uid_of "Zabbix-ATM")
PG_UID=$(uid_of "ATM-Transactions")
ES_UID=$(uid_of "EJ-OpenSearch")
[ -n "$ZBX_UID" ] && [ -n "$PG_UID" ] && [ -n "$ES_UID" ] \
  || { echo "FAIL: a datasource is missing (check Connections > Data sources)"; exit 1; }

# folder (create if missing)
FOLDER_ID=$(auth "$GRAFANA_URL/api/folders" | python3 -c "
import json,sys
fs = json.load(sys.stdin)
print(next((f['id'] for f in fs if f['title']=='$FOLDER'), ''))")
if [ -z "$FOLDER_ID" ]; then
  FOLDER_ID=$(auth -X POST -H 'Content-Type: application/json' -d "{\"title\":\"$FOLDER\"}" \
    "$GRAFANA_URL/api/folders" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
fi
[ -n "$FOLDER_ID" ] || { echo "FAIL: could not create folder"; exit 1; }
echo "folder '$FOLDER' id=$FOLDER_ID"

shopt -s nullglob
export ZBX_UID PG_UID ES_UID FOLDER_ID
for f in "$DIR"/*.json; do
  name=$(basename "$f")
  code=$(python3 - "$f" <<'PYEOF' | curl -s --max-time 60 -o /dev/null -w "%{http_code}" -u "$ADMIN_USER:$GRAFANA_ADMIN_PASSWORD" -H 'Content-Type: application/json' -d @- "$GRAFANA_URL/api/dashboards/import"
import json, os, sys
dash = json.load(open(sys.argv[1]))
payload = {
    "dashboard": dash,
    "folderId": int(os.environ["FOLDER_ID"]),
    "overwrite": True,
    "inputs": [
        {"name": "DS_ZABBIX-ATM", "type": "datasource",
         "pluginId": "alexanderzobnin-zabbix-datasource", "value": os.environ["ZBX_UID"]},
        {"name": "DS_ATM-TRANSACTIONS", "type": "datasource",
         "pluginId": "grafana-postgresql-datasource", "value": os.environ["PG_UID"]},
        {"name": "DS_EJ-ELASTICSEARCH", "type": "datasource",
         "pluginId": "elasticsearch", "value": os.environ["ES_UID"]},
    ],
}
print(json.dumps(payload))
PYEOF
  )
  if [ "$code" = "200" ]; then echo "  [PASS] $name"; PASS=$((PASS+1));
  else echo "  [FAIL] $name (HTTP $code)"; FAIL=$((FAIL+1)); fi
done
echo "== Grafana dashboard import: $PASS ok, $FAIL fail =="
[ "$FAIL" -eq 0 ]
