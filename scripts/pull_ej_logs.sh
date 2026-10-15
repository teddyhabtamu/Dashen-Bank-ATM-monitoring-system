#!/usr/bin/env bash
# pull_ej_logs.sh — fetch NCR journal files via the Zabbix agent (no ATM
# credentials, no firewall changes) and stage them for filebeat.
#
# Evidence rule: the ATM's journal uses bare-CR line endings, which filebeat
# 7.12 cannot split (no line_terminator option — verified against the 7.12
# reference 2026-10-13). So each pull writes TWO artifacts:
#   ej-logs/raw/<file>   byte-identical evidence archive (never harvested:
#                        the filebeat glob only matches top-level *.log)
#   ej-logs/<file>       working copy with CR normalized to LF for parsing.
# The transform is line-ending normalization only — record bytes untouched —
# and this header documents it. Dispute-grade forensics uses raw/.
#
# Usage: ZBX_USER=... ZBX_PASS=... [ZBX_URL=...] ./scripts/pull_ej_logs.sh [days]
# Cron (hourly is plenty; journals rotate ~17:30 daily on the box):
#   20 * * * * cd /data/atm-monitoring && ZBX_USER=... ZBX_PASS=... ./scripts/pull_ej_logs.sh 3 >> logs/ej-pull.log 2>&1
#   (Seal credentials as usual: export from .env via grep-cut, never in chat.)
set -u
# cron runs with a minimal PATH (/usr/bin:/bin). Prepend the standard sbin/bin
# directories so python3, docker (used to read atm_locations) and zabbix_get
# resolve the same way they do in an interactive shell.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
DAYS="${1:-3}"
ZBX_URL="${ZBX_URL:-http://172.22.10.45:8080/api_jsonrpc.php}"
OUTDIR="$(dirname "$0")/../ej-logs"
RAWDIR="$OUTDIR/raw"
mkdir -p "$RAWDIR"

# ATM_ID:AGENT_IP:HOSTNAME — resolved from atm_locations, not hardcoded.
# Adding an ATM used to mean editing the ATMS= line below; it is now data:
# vendor/model/IP/hostname set in ATM Registration is enough. Resolution order
# (scripts/ej_pull_targets.py): $EJ_ATMS override > the portal database > the
# nightly CSV snapshot from scripts/backup_db.sh. The resolver logs its source
# and any skipped ATMs to stderr, which lands in the cron log.
ATMS="$(python3 "$(dirname "$0")/ej_pull_targets.py")"
if [ -z "$ATMS" ]; then
  echo "ej: no pull targets resolved — read the resolver line above" >&2
  echo "ej: (source + skip reasons) before assuming the fleet is empty." >&2
  exit 1
fi

fail=0
for spec in $ATMS; do
  atm_id="${spec%%:*}"; rest="${spec#*:}"
  ip="${rest%%:*}"; tag="${rest##*:}"
  for i in $(seq 0 $((DAYS - 1))); do
    day="$(date -u -d "$i days ago" +%F 2>/dev/null || date -u -v-"$i"d +%F)"
    f="${day}_${tag}_EJDATA.log"
    if zabbix_get -s "$ip" -k "vfs.file.contents[E:\\EJLogs\\$f]" > "$RAWDIR/$f" 2>/dev/null; then
      if grep -q . "$RAWDIR/$f" 2>/dev/null && ! grep -qiE 'ZBX_NOTSUPPORTED|Timeout' "$RAWDIR/$f"; then
        tr '\r' '\n' < "$RAWDIR/$f" > "$OUTDIR/$f"
        echo "  $atm_id $f $(wc -c < "$OUTDIR/$f") bytes"
      else
        rm -f "$RAWDIR/$f"; echo "  $atm_id $f absent-on-box (skipped)"
      fi
    else
      rm -f "$RAWDIR/$f"; echo "  $atm_id $f fetch-failed"
      fail=1
    fi
  done
done
exit $fail
