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
# EJ_FAST=1 turns on the near-real-time cadence: only TODAY's file is
# re-fetched (yesterday's is final once seen) and fetches run in parallel.
# Hourly+serial is fine for a handful of boxes; at 1,300 ATMs a per-minute cron
# would need this, or every run would still be working when the next starts.
DAYS="${1:-3}"
EJ_FAST="${EJ_FAST:-0}"
EJ_PARALLEL="${EJ_PARALLEL:-8}"
ZBX_URL="${ZBX_URL:-http://172.22.10.45:8080/api_jsonrpc.php}"
OUTDIR="$(dirname "$0")/../ej-logs"
RAWDIR="$OUTDIR/raw"
mkdir -p "$RAWDIR"

# Overlap guard. With a per-minute cron, a slow run (or a wide fleet) would
# still be fetching when the next one starts - two runs writing the same
# evidence files, and a growing pile of orphaned zabbix_get processes. flock is
# released by the kernel when the process dies, so a crashed run cannot wedge
# the schedule.
# The lock lives beside the evidence it protects: ej-logs/ is already ignored
# and only *.log at its top level is harvested, so a .lock file there is
# invisible to git AND to filebeat - no .gitignore change needed.
LOCKFILE="${EJ_LOCK:-$OUTDIR/pull.lock}"

# Stamps remember "we already looked for this file and the box did not have it",
# so a per-minute cron stops re-asking for a journal that does not exist yet.
# They expire (EJ_ABSENT_RETRY_MIN) so a file that appears later is still picked
# up within minutes instead of being ignored for the rest of the day.
STAMPDIR="$OUTDIR/.absent"
EJ_ABSENT_RETRY_MIN="${EJ_ABSENT_RETRY_MIN:-10}"
mkdir -p "$STAMPDIR"
exec 9>"$LOCKFILE"
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    echo "ej: a pull is already running (lock held) - this run is skipped, not queued." >&2
    exit 0
  fi
fi

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

# One ATM: fetch its journal files into the evidence copy and the working copy.
pull_one() {
  spec="$1"
  atm_id="${spec%%:*}"; rest="${spec#*:}"
  ip="${rest%%:*}"; tag="${rest##*:}"
  local rc=0
  # Which journal is actually being appended to? Assumed to be the newest file
  # we hold for this box, NOT "today's": NCR names the live journal by its own
  # bank date, which on this fleet trails the server date by a day. Assuming
  # today meant every fast run re-fetched a file the box does not have yet and
  # never re-fetched the one that was growing.
  local live
  live="$(ls -t "$OUTDIR"/*_"$tag"_EJDATA.log 2>/dev/null | head -1)"
  if [ -z "$live" ]; then
    # Defensive: if we hold files but cannot pick the newest (odd tag, glob
    # quirk), fall back to probing today. Otherwise every existing file would be
    # classed "closed" and the growing journal would go unrefreshed - silent
    # staleness that looks exactly like a healthy run.
    live="$OUTDIR/$(date -u +%F 2>/dev/null || date -u +%F)_${tag}_EJDATA.log"
  fi
  for i in $(seq 0 $((DAYS - 1))); do
    day="$(date -u -d "$i days ago" +%F 2>/dev/null || date -u -v-"$i"d +%F)"
    f="${day}_${tag}_EJDATA.log"
    if [ "$EJ_FAST" = "1" ]; then
      if [ -n "$live" ] && [ "$live" = "$OUTDIR/$f" ]; then
        : # the live journal: always re-fetch, it grows between runs
      elif [ -s "$OUTDIR/$f" ]; then
        continue # a closed journal we already have: pure load, no new data
      elif find "$STAMPDIR/$tag-$f.stamp" -mmin -"$EJ_ABSENT_RETRY_MIN" -print -quit 2>/dev/null | grep -q .; then
        continue # recently confirmed absent; retried every EJ_ABSENT_RETRY_MIN
      fi
    fi
    if zabbix_get -s "$ip" -k "vfs.file.contents[E:\\EJLogs\\$f]" > "$RAWDIR/$f" 2>/dev/null; then
      if grep -q . "$RAWDIR/$f" 2>/dev/null && ! grep -qiE 'ZBX_NOTSUPPORTED|Timeout' "$RAWDIR/$f"; then
        # Append-only on disk. filebeat tails ej-logs/ (bind-mounted as
        # /var/log/atm-ej) and treats a shrinking file as a truncation, so the
        # old "tr > out" rewrote the file from zero and made filebeat re-read
        # and re-index every line - visible as duplicate EJ rows. So: identical
        # content is left completely untouched, growth is appended (filebeat
        # reads only the delta), and only a genuine upstream rewrite replaces.
        norm="$RAWDIR/$f.norm"
        tr '\r' '\n' < "$RAWDIR/$f" > "$norm"
        old=0
        [ -s "$OUTDIR/$f" ] && old="$(wc -c < "$OUTDIR/$f")"
        if [ "$old" -gt 0 ] && cmp -s "$norm" "$OUTDIR/$f"; then
          rm -f "$norm"
          echo "  $atm_id $f unchanged ($old bytes, not re-written)"
        elif [ "$old" -gt 0 ] && head -c "$old" "$norm" | cmp -s - "$OUTDIR/$f"; then
          tail -c "+$((old + 1))" "$norm" >> "$OUTDIR/$f"
          rm -f "$norm"
          echo "  $atm_id $f +$(( $(wc -c < "$OUTDIR/$f") - old )) bytes (appended, now $(wc -c < "$OUTDIR/$f"))"
        else
          mv "$norm" "$OUTDIR/$f"
          echo "  $atm_id $f replaced ($old -> $(wc -c < "$OUTDIR/$f") bytes, upstream rewrite)"
        fi
        rm -f "$STAMPDIR/$tag-$f.stamp"
      else
        rm -f "$RAWDIR/$f"; touch "$STAMPDIR/$tag-$f.stamp"
        echo "  $atm_id $f absent-on-box (skipped)"
      fi
    else
      rm -f "$RAWDIR/$f"; echo "  $atm_id $f fetch-failed"
      rc=1
    fi
  done
  return $rc
}

if [ "${EJ_DRY_RUN:-0}" = "1" ]; then
  echo "ej: dry run - $(echo "$ATMS" | wc -w) target(s), DAYS=$DAYS, fast=$EJ_FAST, parallel=$EJ_PARALLEL"
  exit 0
fi

fail=0
if [ "$EJ_FAST" = "1" ] && [ "$EJ_PARALLEL" -gt 1 ]; then
  # Bounded parallelism: never more than EJ_PARALLEL fetches in flight, so a
  # per-minute cron cannot open 1,300 sockets at the Zabbix server at once.
  running=0
  for spec in $ATMS; do
    pull_one "$spec" &
    running=$((running + 1))
    if [ "$running" -ge "$EJ_PARALLEL" ]; then
      wait -n 2>/dev/null || wait
      running=$((running - 1))
    fi
  done
  wait || fail=1
else
  for spec in $ATMS; do
    pull_one "$spec" || fail=1
  done
fi
exit $fail
