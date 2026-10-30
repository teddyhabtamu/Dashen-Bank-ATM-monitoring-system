#!/usr/bin/env bash
# Install the EJ staging ingestion cron entry without editing crontab by hand.
#
# Idempotent: removes the previous marked entry and appends the current one.
# The pull cron is separate and is not touched.
#
#   ./scripts/install_ej_ingest_cron.sh
#   ./scripts/install_ej_ingest_cron.sh --remove
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MARKER='# ej-shadow-ingest'
LINE_MARK='ej-shadow-ingest'

if ! command -v crontab >/dev/null 2>&1; then
    echo "crontab is not installed for this user." >&2
    exit 1
fi

remove_entry() {
    local current backup tmp
    current="$(crontab -l 2>/dev/null || true)"
    backup="$HOME/crontab.backup.ej-$(date +%Y%m%d-%H%M%S)"
    if [[ -n "$current" ]]; then
        printf '%s\n' "$current" > "$backup"
        echo "backup: $backup"
    fi
    tmp="$(mktemp)"
    if [[ -n "$current" ]]; then
        printf '%s\n' "$current" | grep -Fv "$LINE_MARK" > "$tmp" || true
    fi
    crontab "$tmp"
    rm -f "$tmp"
}

if [[ "${1:-}" = "--remove" ]]; then
    remove_entry
    echo "removed EJ ingestion cron entry (pull cron unchanged)."
    exit 0
fi

if [[ ! -f "$ROOT/scripts/ingest_ej_shadow.py" ]]; then
    echo "missing $ROOT/scripts/ingest_ej_shadow.py" >&2
    exit 1
fi

DOCKER="$(command -v docker || true)"
PYTHON="$(command -v python3 || true)"
FLOCK="$(command -v flock || true)"
if [[ -z "$DOCKER" ]] || [[ -z "$PYTHON" ]] || [[ -z "$FLOCK" ]]; then
    echo "docker, python3 and flock must all be available to the cron user." >&2
    exit 1
fi

mkdir -p "$ROOT/logs"
PATH_LINE='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
LINE="*/10 * * * * $FLOCK -n /tmp/atm-ej-ingest.lock -c 'cd $ROOT && PATH=$PATH_LINE EJ_DOCKER_BIN=$DOCKER $PYTHON scripts/ingest_ej_shadow.py --commit >> logs/ej-ingest.log 2>&1' $MARKER"

current="$(crontab -l 2>/dev/null || true)"
backup="$HOME/crontab.backup.ej-$(date +%Y%m%d-%H%M%S)"
if [[ -n "$current" ]]; then
    printf '%s\n' "$current" > "$backup"
    echo "backup: $backup"
fi

tmp="$(mktemp)"
if [[ -n "$current" ]]; then
    printf '%s\n' "$current" | grep -Fv "$LINE_MARK" > "$tmp" || true
fi
printf '%s\n' "$LINE" >> "$tmp"
crontab "$tmp"
rm -f "$tmp"

echo "installed:"
crontab -l | grep -F "$LINE_MARK"
