#!/usr/bin/env bash
# diagnose_portal.sh — portal triage in one command, no archaeology.
# Prints: container state, current UTC time, and the CURRENT boot/worker
# error (clock-anchored, never log history). Created 2026-10-15 after a
# 10-round outage hunt that kept reading yesterday's logs as today's.
set -u
echo "== now: $(date -u) =="
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'report-portal|zabbix-web' || true
echo "== fresh errors (last 5 min) =="
docker logs report-portal --since 5m 2>&1 | grep -E 'Error|Traceback|Exception in worker|failed to boot' | head -12
echo "== hint: empty section above + non-Up status means the failure is older than 5 min;"
echo "   widen with: docker logs report-portal --since 30m 2>&1 | tail -30 =="
