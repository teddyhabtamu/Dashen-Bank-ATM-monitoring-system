#!/usr/bin/env bash
# Install the GRG EJ pull cron entry without editing crontab by hand.
#
# GRG does not push its journal; pull_grg_ej_logs.py fetches EJ<yymmdd>.log
# through the Zabbix agent and writes the canonical *_EJDATA.log. Filebeat and
# the separate EJ ingest cron then process that file automatically.
#
# Idempotent: removes the previous marked entry and appends the current one.
#
#   ./scripts/install_grg_pull_cron.sh
#   ./scripts/install_grg_pull_cron.sh --remove
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MARKER='# grg-ej-pull'
LINE_MARK='grg-ej-pull'

if ! command -v crontab >/dev/null 2>&1; then
    echo "crontab is not installed for this user." >&2
    exit 1
fi

remove_entry() {
    local current backup tmp
    current="$(crontab -l 2>/dev/null || true)"
    backup="$HOME/crontab.backup.grg-$(date +%Y%m%d-%H%M%S)"
    if [ -n "$current" ]; then
        printf '%s\n' "$current" > "$backup"
        echo "backup: $backup"
    fi
    tmp="$(mktemp)"
    if [ -n "$current" ]; then
        printf '%s\n' "$current" | grep -Fv "$LINE_MARK" > "$tmp" || true
    fi
    crontab "$tmp"
    rm -f "$tmp"
}

if [ "${1:-}" = "--remove" ]; then
    remove_entry
    echo "removed GRG EJ pull cron entry."
    exit 0
fi

if [ -n "${1:-}" ]; then
    echo "usage: $0 [--remove]" >&2
    exit 2
fi

if [ ! -f "$ROOT/scripts/pull_grg_ej_logs.py" ]; then
    echo "missing $ROOT/scripts/pull_grg_ej_logs.py" >&2
    exit 1
fi

for tool in docker python3 flock zabbix_get; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "$tool must be available to the cron user." >&2
        exit 1
    fi
done

DOCKER="$(command -v docker)"
PYTHON="$(command -v python3)"
FLOCK="$(command -v flock)"
ZBX_GET="$(command -v zabbix_get)"
mkdir -p "$ROOT/logs"
PATH_LINE='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
LINE="*/5 * * * * $FLOCK -n /tmp/atm-ej-grg-pull.lock -c 'cd $ROOT && PATH=$PATH_LINE EJ_DOCKER_BIN=$DOCKER ZBX_GET=$ZBX_GET $PYTHON scripts/pull_grg_ej_logs.py --days 3 >> logs/grg-ej-pull.log 2>&1' $MARKER"

current="$(crontab -l 2>/dev/null || true)"
backup="$HOME/crontab.backup.grg-$(date +%Y%m%d-%H%M%S)"
if [ -n "$current" ]; then
    printf '%s\n' "$current" > "$backup"
    echo "backup: $backup"
fi

tmp="$(mktemp)"
if [ -n "$current" ]; then
    printf '%s\n' "$current" | grep -Fv "$LINE_MARK" > "$tmp" || true
fi
printf '%s\n' "$LINE" >> "$tmp"
crontab "$tmp"
rm -f "$tmp"

echo "installed:"
crontab -l | grep -F "$LINE_MARK"
