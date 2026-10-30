#!/usr/bin/env bash
# Import OpenSearch Dashboards saved objects for UAT (index patterns, visualizations, dashboards).
# Every *.ndjson file in config/osd/saved_objects/ is imported (idempotent; overwrite=true).
#
# Usage:
#   OSD_HOST=172.22.10.60:5601 ./load_osd_dashboards.sh
#
# (If you build dashboards in the OSD UI during UAT, export them from
#  Stack Management -> Saved Objects and drop the .ndjson here for reuse.)
set -uo pipefail

OSD_HOST="${OSD_HOST:-localhost:5601}"
DIR="$(dirname "$0")/../config/osd/saved_objects"
PASS=0; FAIL=0

shopt -s nullglob
files=("$DIR"/*.ndjson)
if [[ ${#files[@]} -eq 0 ]]; then echo "No .ndjson files in $DIR"; exit 0; fi

for f in "${files[@]}"; do
  echo "Importing $(basename "$f") ..."
  resp=$(curl -s -X POST "http://$OSD_HOST/api/saved_objects/_import?overwrite=true" \
    -H "osd-xsrf: true" \
    -F "file=@$f")
  if echo "$resp" | grep -q '"success":true\|"success": true'; then
    echo "  [PASS] $(basename "$f")"; PASS=$((PASS+1))
  else
    echo "  [FAIL] $(basename "$f"): $resp"; FAIL=$((FAIL+1))
  fi
done

echo "== OSD saved-object import: $PASS ok, $FAIL fail =="
[[ "$FAIL" -eq 0 ]]
