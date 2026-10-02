#!/bin/bash
# Backup ALL manual tool configurations (the half backup_db.sh doesn't cover).
# A DB dump alone does NOT rebuild this stack: Zabbix trigger actions can't
# be XML-exported, hand-edited Grafana dashboards live only in their volume,
# and .env/filebeat.yml/compose edits live only on this disk.
#
# Collects into config/backups/<DATE>/ then tarballs it:
#   zabbix/  - hosts + templates + media types (API export), trigger actions
#              as raw JSON (recreated by scripts/setup_zabbix_actions.py)
#   grafana/ - datasources + dashboard JSON (API export)
#   files/   - .env, compose files, filebeat.yml, config/, glpi-plugins/
# Set BACKUP_DEST (scp target) to also copy the bundle off this host —
# a backup that lives on the dying disk is not a backup.
#
# Usage: scripts/backup_configs.sh [--dry-run]
#   --dry-run prints what would be collected + connectivity checks, writes nothing.

set -u
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_ROOT="$REPO_DIR/config/backups"
DATE=$(date +%Y%m%d_%H%M%S)
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

if [ -f "$REPO_DIR/.env" ]; then
  set -a; . "$REPO_DIR/.env"; set +a
fi

ZBX_URL="${ZBX_URL:-http://localhost:8080/api_jsonrpc.php}"
ZBX_USER="${ZBX_USER:-Admin}"
ZBX_PASS="${ZBX_PASS:-zabbix}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-}"
BACKUP_DEST="${BACKUP_DEST:-}"

log()  { echo "[backup-configs] $*"; }
warn() { echo "[backup-configs] WARNING: $*" >&2; }

if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN — would collect into $BACKUP_ROOT/$DATE/:"
  log "  zabbix/hosts.json, templates.json, mediatypes.json (API export)"
  log "  zabbix/actions.json (raw; recreated by setup_zabbix_actions.py)"
  log "  grafana/datasources.json + dashboards/*.json (API export)"
  log "  files/ (.env, compose files, filebeat.yml, config/, glpi-plugins/)"
  [ -n "$BACKUP_DEST" ] && log "  then scp bundle to $BACKUP_DEST" || log "  BACKUP_DEST unset: bundle would stay local (fix this)"
  log "Connectivity checks:"
  # api_jsonrpc.php only speaks POST (GET returns 412) — probe with apiinfo.version.
  if curl -sf -m 10 -H 'Content-Type: application/json-rpc' \
      -d '{"jsonrpc":"2.0","method":"apiinfo.version","id":1,"params":{}}' \
      -o /dev/null "$ZBX_URL"; then
    log "  zabbix-web API reachable"
  else
    warn "  zabbix-web API NOT reachable"
  fi
  if [ -n "$GRAFANA_ADMIN_PASSWORD" ]; then
    curl -sf -m 10 -o /dev/null -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA_URL/api/org" \
      && log "  grafana API reachable" || warn "  grafana API NOT reachable (check password/URL)"
  else
    warn "  GRAFANA_ADMIN_PASSWORD unset: grafana export would be skipped"
  fi
  exit 0
fi

DEST="$BACKUP_ROOT/$DATE"
mkdir -p "$DEST/zabbix" "$DEST/grafana/dashboards" "$DEST/files"

# --- 1. Zabbix ---------------------------------------------------------------
# One python block: login, export hosts/templates/media, dump raw actions.
# (Actions have no XML export — raw JSON + setup_zabbix_actions.py to rebuild.)
export ZBX_URL ZBX_USER ZBX_PASS DEST
if python3 - <<'EOF'; then
import json, os, urllib.request
BASE = os.environ['ZBX_URL']
DEST = os.environ['DEST']

def call(method, params, auth=None):
    import urllib.error
    body = {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1}
    if auth:
        body['auth'] = auth
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json-rpc'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:500]
        raise RuntimeError(f"{method}: HTTP {e.code} {detail}")
    if 'error' in res:
        raise RuntimeError(f"{method}: {res['error']}")
    return res.get('result')

auth = call('user.login', {'username': os.environ['ZBX_USER'],
                           'password': os.environ['ZBX_PASS']})
try:
    specs = [('hosts', 'host.get', {'output': ['hostid']}, 'hostid', 'hosts'),
             ('templates', 'template.get', {'output': ['templateid']}, 'templateid', 'templates'),
             ('mediatypes', 'mediatype.get', {'output': ['mediatypeid']}, 'mediatypeid', 'mediaTypes')]
    for name, method, params, idkey, optkey in specs:
        try:
            ids = [x[idkey] for x in (call(method, params, auth) or [])]
            exp = call('configuration.export',
                       {'format': 'json', 'options': {optkey: ids}}, auth)
            with open(f'{DEST}/zabbix/{name}.json', 'w') as f:
                f.write(exp if isinstance(exp, str) else json.dumps(exp))
            print(f'  exported {name} ({len(ids)} objects)')
        except Exception as e:
            print(f'  FAILED {name}: {e}')
    try:
        acts = call('action.get', {'output': 'extend', 'selectOperations': 'extend',
                                   'selectRecoveryOperations': 'extend'}, auth)
        with open(f'{DEST}/zabbix/actions.json', 'w') as f:
            json.dump(acts, f, indent=1)
        print(f'  exported actions ({len(acts or [])})')
    except Exception as e:
        print(f'  FAILED actions: {e}')
finally:
    try:
        call('user.logout', [], auth)
    except Exception:
        pass
EOF
  log "Zabbix exports done"
else
  warn "Zabbix exports FAILED (login/API) — DB dumps in backup_db.sh unaffected"
fi

# --- 2. Grafana --------------------------------------------------------------
if [ -z "$GRAFANA_ADMIN_PASSWORD" ]; then
  warn "GRAFANA_ADMIN_PASSWORD unset — skipping Grafana export"
else
  if curl -sf -m 20 -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA_URL/api/datasources" -o "$DEST/grafana/datasources.json" 2>/dev/null; then
    log "  exported datasources"
  else
    warn "datasources export failed"
  fi
  for uid in $(curl -sf -m 20 -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA_URL/api/search?query=" 2>/dev/null \
      | python3 -c "import json,sys; print(' '.join(d.get('uid','') for d in json.load(sys.stdin) if d.get('uid')))" 2>/dev/null); do
    if curl -sf -m 20 -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA_URL/api/dashboards/uid/$uid" -o "$DEST/grafana/dashboards/$uid.json" 2>/dev/null; then
      log "  exported dashboard $uid"
    else
      warn "dashboard $uid export failed"
    fi
  done
fi

# --- 3. Files (secrets included — bundle is sensitive, never commit) ---------
# NOTE: $DEST lives inside $REPO_DIR/config/backups/, so copy config/ via
# tar --exclude=backups — a plain cp -r would recurse into the bundle itself.
{
  [ -f "$REPO_DIR/.env" ] && cp "$REPO_DIR/.env" "$DEST/files/.env"
  cp "$REPO_DIR/docker-compose.yml" "$DEST/files/" 2>/dev/null
  cp "$REPO_DIR/docker-compose.gateway.yml" "$DEST/files/" 2>/dev/null
  cp "$REPO_DIR/filebeat.yml" "$DEST/files/" 2>/dev/null
  tar -czf "$DEST/files/config.tar.gz" --exclude=backups -C "$REPO_DIR" config 2>/dev/null
  [ -d "$REPO_DIR/glpi-plugins" ] && tar -czf "$DEST/files/glpi-plugins.tar.gz" -C "$REPO_DIR" glpi-plugins 2>/dev/null
  chmod 600 "$DEST/files/.env" "$DEST/files/"*.tar.gz 2>/dev/null
  true
} || warn "some files could not be copied"
log "  files collected"

# --- 4. Bundle + optional off-host copy --------------------------------------
TARBALL="$BACKUP_ROOT/configs_$DATE.tar.gz"
tar -czf "$TARBALL" -C "$BACKUP_ROOT" "$DATE" && rm -rf "$DEST"
chmod 600 "$TARBALL"
log "Bundle: $TARBALL"
ls -t "$BACKUP_ROOT"/configs_*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm

if [ -n "$BACKUP_DEST" ]; then
  if scp "$TARBALL" "$BACKUP_DEST"; then
    log "Copied off-host to $BACKUP_DEST"
  else
    warn "scp to $BACKUP_DEST FAILED — bundle stays local"
  fi
else
  warn "BACKUP_DEST unset — bundle is LOCAL ONLY. Set BACKUP_DEST=user@backup-server:/backups/ (e.g. in .env) so a disk death doesn't take backups with it."
fi

# --- 5. Restore order (also the disaster runbook) -----------------------------
cat <<'RESTOREF'

Restore order after a total loss (fresh RHEL9 + Docker):
  1. git clone <repo> && copy sealed .env back (from off-host bundle files/.env)
  2. docker compose up -d postgres  (+ mariadb/glpi on their hosts)
  3. scripts/restore_db.sh --confirm   (Postgres custom tables)
     + GLPI: zcat <bundle>/files/.../glpi_*.sql.gz | docker exec -i glpi-db mysql glpi
  4. Zabbix: Configuration -> Import the zabbix/*.json exports
     + python3 scripts/setup_zabbix_actions.py   (recreates trigger actions)
  5. Grafana: POST grafana/datasources.json + dashboards/*.json to the API
  6. Copy back filebeat.yml (chown root:root!), compose files, glpi-plugins/
  7. Verify: portal login, Monitoring Center data, one GLPI ticket flow.
RESTOREF
