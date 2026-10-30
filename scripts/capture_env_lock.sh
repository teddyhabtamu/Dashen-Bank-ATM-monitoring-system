#!/usr/bin/env bash
# Capture the environment that is ACTUALLY running, so it can be rebuilt.
#
# Why this exists: every service image used a floating tag
# (ubuntu-6.4-latest, diouxx/glpi:latest) and the portal had no requirements
# file at all. So "what version are we on?" had no answer in version control,
# and a rebuild produced something that was merely similar.
#
# This captures two things, from the running system rather than from a document:
#   1. the exact image digest behind every container (digest, not tag - a tag
#      can be repointed, a digest cannot)
#   2. the exact installed Python packages in the portal
#
# Usage on the monitoring host:
#
#     bash scripts/capture_env_lock.sh            # inspect, write to dist/
#     bash scripts/capture_env_lock.sh --apply    # also update .env pins
#
# Read-only unless --apply is passed. Nothing here changes a running container.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
OUT="dist"
mkdir -p "$OUT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "==> Container image digests"
# `docker inspect` on the image reference resolves the running container's
# image to an immutable digest. Format is what compose calls the image.
{
  printf '# Captured %s\n' "$STAMP"
  printf '# Resolved from RUNNING containers: a tag can move, a digest cannot.\n'
  for c in $(docker ps --format '{{.Names}}'); do
    ref=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) || continue
    dig=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null) || continue
    [[ -n "$ref" ]] && printf '%-24s %-52s %s\n' "$c" "$ref" "$dig"
  done
} | tee "$OUT/images-$STAMP.txt"

echo
echo "==> Portal Python packages"
if docker exec report-portal python3 -m pip freeze > "$OUT/requirements-$STAMP.txt" 2>/dev/null; then
  grep -vE '^(pkg[-_]resources|setuptools|pip|wheel)==' "$OUT/requirements-$STAMP.txt" \
    > report-portal/requirements.lock
  echo "wrote report-portal/requirements.lock ($(grep -c . report-portal/requirements.lock) packages)"
  echo "  review it, then commit it alongside whatever you changed."
else
  echo "  could not exec into report-portal - is it running?"
fi

echo
echo "==> Suggested .env pins"
{
  printf '# Captured %s - digests from running containers.\n' "$STAMP"
  printf '# Uncomment to make the deployment reproducible. A rebuild then\n'
  printf '# produces THIS environment, not merely a similar one.\n'
  for c in $(docker ps --format '{{.Names}}'); do
    dig=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null) || continue
    case "$c" in
      zabbix-server|zabbix-web|zabbix-agent) printf '# %s: %s\n' "$c" "$dig" ;;
    esac
  done
} | tee "$OUT/env-pins-$STAMP.txt"

echo
echo "Done. Review $OUT/ and report-portal/requirements.lock before committing."
if [[ "${1:-}" = "--apply" ]]; then
  echo "--apply: appending pins to .env (they stay commented until you uncomment)"
  cat "$OUT/env-pins-$STAMP.txt" >> .env
fi