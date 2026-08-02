#!/bin/bash
# ============================================
# Dashen Bank ATM Monitoring System
# One-command new machine setup
# Run: bash scripts/setup_new_machine.sh
# ============================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "======================================"
echo "Dashen Bank ATM Monitoring Setup"
echo "======================================"

# ── Step 1 — Check .env exists ─────────────────
echo ""
echo "Step 1: Checking .env file..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "  Created .env from .env.example — edit passwords before proceeding."
        echo "  At minimum set: ADMIN_PASS, FLASK_SECRET_KEY"
        echo "  Then re-run this script."
        exit 1
    else
        echo "  ERROR: No .env or .env.example found. Create one with your settings."
        exit 1
    fi
fi
echo ".env OK"

# ── Step 2 — Check Docker ──────────────────────
echo ""
echo "Step 2: Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo "  Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "  Docker installed. Please log out and back in, then re-run."
    exit 1
fi
echo "Docker OK"

# ── Step 3 — Check required files ──────────────
echo ""
echo "Step 3: Checking project files..."
required_files=(
    "docker-compose.yml" "filebeat.yml"
    "simulators/sim_engine.py" "simulators/txn_engine.py"
    "simulators/ej_engine.py" "simulators/snmp_agent.py"
    "simulators/state_manager.py" "simulators/common.py"
    "simulators/Dockerfile.atm-simulator"
    "report-portal/app.py" "report-portal/Dockerfile"
    "camel/iso8583_gateway.py" "camel/Dockerfile.gateway"
    "config/zabbix/zbx_export_mediatypes.xml"
    "config/zabbix/template_ncr_snmp.xml"
    "config/zabbix/template_grg_snmp.xml"
)
for f in "${required_files[@]}"; do
    if [ ! -f "$f" ]; then
        echo "  Missing: $f"
        exit 1
    fi
done
echo "All files present OK"

# ── Step 4 — Create directories ────────────────
echo ""
echo "Step 4: Creating directories..."
mkdir -p ej-logs reports config/grafana/dashboards config/postgres
echo "Directories OK"

# ── Step 5 — Build Docker images ───────────────
echo ""
echo "Step 5: Building Docker images..."
docker compose build \
    atm-sim-engine atm-txn-engine atm-ej-engine state-manager \
    report-portal iso8583-gateway
echo "Build OK"

# ── Step 6 — Start all services ────────────────
echo ""
echo "Step 6: Starting all services..."
docker compose up -d
echo "Waiting 90 seconds for services to initialize..."
sleep 90

# ── Step 7 — Restore database ──────────────────
echo ""
echo "Step 7: Restoring database..."
if [ -f "config/postgres/atm_custom_tables.sql" ]; then
    bash scripts/restore_db.sh
else
    echo "  No database backup found, creating fresh ATM locations..."
    docker exec zabbix-db psql -U zabbix -d zabbix <<'SQLEOF'
CREATE TABLE IF NOT EXISTS atm_locations (
    atm_id VARCHAR(20) PRIMARY KEY, branch VARCHAR(100),
    district VARCHAR(100), city VARCHAR(100), region VARCHAR(100),
    latitude DECIMAL(10,7), longitude DECIMAL(10,7),
    terminal_id VARCHAR(20), vendor VARCHAR(50), model VARCHAR(50),
    install_date DATE, status VARCHAR(20) DEFAULT 'active'
);
INSERT INTO atm_locations VALUES
('ATM-001','Addis Ababa Main Branch','Kirkos','Addis Ababa','Addis Ababa',9.0300,38.7578,'TID001','NCR','SelfServ 34','2023-01-15','active')
ON CONFLICT (atm_id) DO NOTHING;
SQLEOF
    echo "  Fresh ATM locations created"
fi

# Export atm_locations.csv from the DB (file is gitignored, needed by sync script)
echo "  Exporting atm_locations.csv for Zabbix sync..."
docker exec zabbix-db psql -U zabbix -d zabbix \
    -c "COPY atm_locations TO STDOUT WITH CSV HEADER" \
    > config/postgres/atm_locations.csv

# ── Step 8 — Import Zabbix templates & hosts ───
echo ""
echo "Step 8: Importing Zabbix templates and registering hosts..."
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates || {
    echo "  WARNING: Zabbix host sync failed. Re-run:"
    echo "  python3 scripts/sync_atms_to_zabbix.py --apply --import-templates"
}

# ── Step 9 — Import mediatype XML via API ──────
echo ""
echo "Step 9: Importing GLPI Ticket mediatype into Zabbix..."
python3 -c "
import requests, os
url = os.environ.get('ZBX_URL', 'http://localhost:8080/api_jsonrpc.php')
user = os.environ.get('ZBX_USER', 'Admin')
pwd  = os.environ.get('ZBX_PASS', 'zabbix')
auth = requests.post(url, json={'jsonrpc':'2.0','method':'user.login','params':{'username':user,'password':pwd},'id':1}, headers={'Content-Type':'application/json-rpc'}, timeout=30).json()['result']
with open('config/zabbix/zbx_export_mediatypes.xml') as f:
    xml_data = f.read()
r = requests.post(url, json={'jsonrpc':'2.0','method':'configuration.import','params':{'format':'xml','rules':{'mediaTypes':{'createMissing':True,'updateExisting':True}},'source':xml_data},'auth':auth,'id':2}, headers={'Content-Type':'application/json-rpc'}, timeout=30)
res = r.json()
if 'error' in res:
    print(f'  WARNING: Mediatype import failed: {res[\"error\"]}')
else:
    print('  GLPI Ticket mediatype imported successfully')
" 2>&1 | grep -v '^<'

# ── Step 10 — Create Zabbix trigger action ─────
echo ""
echo "Step 10: Creating Zabbix trigger action for GLPI..."
python3 scripts/setup_zabbix_actions.py || {
    echo "  WARNING: Failed to create trigger action."
}

# ── Step 11 — Reload Zabbix config cache ───────
echo ""
echo "Step 11: Reloading Zabbix config cache..."
docker exec zabbix-server zabbix_server -R config_cache_reload 2>/dev/null || true
sleep 3

# ── Step 12 — Install/configure GLPI ───────────
echo ""
echo "Step 12: Configuring GLPI..."

# The diouxx/glpi image downloads GLPI from GitHub on first boot.
# Wait for the actual install files (Apache responds 200 even before GLPI exists).
echo -n "  Waiting for GLPI installation (first-boot download)..."
GLPI_FILES_OK=0
for i in $(seq 1 60); do
    if docker exec glpi sh -c '[ -f /var/www/html/glpi/bin/console ]' 2>/dev/null; then
        echo " ready"
        GLPI_FILES_OK=1
        break
    fi
    echo -n "."
    sleep 10
done

# If the image's first-boot download failed (e.g. slow GitHub), fetch it manually
if [ "$GLPI_FILES_OK" = "0" ]; then
    echo ""
    echo "  GLPI files missing — downloading GLPI 10.0.15 manually..."
    docker exec glpi sh -c '
        cd /var/www/html &&
        wget -q https://github.com/glpi-project/glpi/releases/download/10.0.15/glpi-10.0.15.tgz &&
        tar -xzf glpi-10.0.15.tgz &&
        rm -f glpi-10.0.15.tgz &&
        chown -R www-data:www-data glpi
    ' || echo "  ERROR: GLPI download failed — check network access to github.com"
    if docker exec glpi sh -c '[ -f /var/www/html/glpi/bin/console ]' 2>/dev/null; then
        GLPI_FILES_OK=1
    fi
fi

if [ "$GLPI_FILES_OK" = "0" ]; then
    echo "  FATAL: GLPI files are missing and could not be downloaded."
    echo "  Check the glpi container logs: docker logs glpi"
    echo "  And ensure github.com is reachable from this machine."
    exit 1
fi

# Check if GLPI is already installed by probing the DB
MYSQL_PASSWORD=$(grep -E '^MYSQL_PASSWORD=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
GLPI_CONSOLE="php /var/www/html/glpi/bin/console"
INSTALLED=$(docker exec glpi-db mysql -u glpi -p"$MYSQL_PASSWORD" glpi -e "SELECT COUNT(*) FROM glpi_configs WHERE name='version'" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
if [ -z "$INSTALLED" ] || [ "$INSTALLED" = "0" ]; then
    echo "  GLPI not yet installed — running CLI installer..."
    docker exec glpi $GLPI_CONSOLE db:install \
        --db-host=glpi-db --db-name=glpi \
        --db-user=glpi --db-password="$MYSQL_PASSWORD" \
        --default-language=en --no-interaction 2>&1 | grep -v "already\|already exists" || true
fi
# Set default password so the REST API can log in
# (GLPI 10.0.15 has no CLI password command; hash via Auth and UPDATE directly)
echo "  Setting GLPI admin password..."
docker exec glpi php -r "
\$_SERVER['HTTP_HOST'] = 'localhost';
\$_SERVER['REQUEST_URI'] = '/';
require '/var/www/html/glpi/inc/includes.php';
\$DB = new DB();
\$hash = Auth::getPasswordHash('DashenGLPI2024');
\$DB->query(\"UPDATE glpi_users SET password = '\" . \$DB->escape(\$hash) . \"', password_last_update = NOW() WHERE name = 'glpi'\");
echo '  GLPI password set' . PHP_EOL;
" 2>&1 | tail -1 || echo "  WARNING: could not set GLPI password"

# Enable REST API + create API client via GLPI's internal API
echo "  Enabling GLPI REST API..."
docker exec glpi php -r "
\$_SERVER['HTTP_HOST'] = 'localhost';
\$_SERVER['REQUEST_URI'] = '/';
require '/var/www/html/glpi/inc/includes.php';
\$DB = new DB();
// Enable REST API + credential login + create API client
\$DB->updateOrDie('glpi_configs', ['value' => 1], ['name' => 'enable_api']);
\$DB->updateOrDie('glpi_configs', ['value' => 1], ['name' => 'enable_api_login_credentials']);
\$DB->updateOrDie('glpi_configs', ['value' => 1], ['name' => 'enable_api_login_external_token']);
// Create API client if missing
\$res = \$DB->request('glpi_apiclients', ['WHERE' => ['name' => 'Zabbix Webhook']]);
if (\$res->count() == 0) {
    \$token = bin2hex(random_bytes(32));
    \$DB->insert('glpi_apiclients', [
        'name' => 'Zabbix Webhook',
        'is_active' => 1,
        'app_token' => \$token,
        'dolog_method' => 0,
    ]);
    echo 'App-Token: ' . \$token . PHP_EOL;
} else {
    echo 'App-Token: ' . \$res->current()['app_token'] . PHP_EOL;
}
" 2>/dev/null | tee /tmp/glpi_app_token.txt
echo "  GLPI API configured"

GLPI_APP_TOKEN=$(grep 'App-Token:' /tmp/glpi_app_token.txt | awk '{print $2}')
if [ -n "$GLPI_APP_TOKEN" ]; then
    echo "  Updating Zabbix mediatype with GLPI App-Token..."
    # Update the mediatype's app_token parameter via Zabbix API
    python3 -c "
import requests, os
url = os.environ.get('ZBX_URL', 'http://localhost:8080/api_jsonrpc.php')
user = os.environ.get('ZBX_USER', 'Admin')
pwd  = os.environ.get('ZBX_PASS', 'zabbix')
auth = requests.post(url, json={'jsonrpc':'2.0','method':'user.login','params':{'username':user,'password':pwd},'id':1}, headers={'Content-Type':'application/json-rpc'}, timeout=30).json()['result']
# Get mediatype ID
r = requests.post(url, json={'jsonrpc':'2.0','method':'mediatype.get','params':{'output':['mediatypeid','name'],'filter':{'name':'GLPI Ticket'}},'auth':auth,'id':2}, headers={'Content-Type':'application/json-rpc'}, timeout=30)
mtid = r.json()['result'][0]['mediatypeid']
# Update app_token parameter
r = requests.post(url, json={'jsonrpc':'2.0','method':'mediatype.update','params':{'mediatypeid':mtid,'parameters':[{'name':'app_token','value':'$GLPI_APP_TOKEN'}]},'auth':auth,'id':3}, headers={'Content-Type':'application/json-rpc'}, timeout=30)
if 'error' in r.json():
    print('  WARNING: Could not update app_token — do it manually in Zabbix UI')
else:
    print('  GLPI App-Token updated in Zabbix mediatype')
"
fi

# ── Step 13 — Run GLPI setup script ────────────
echo ""
echo "Step 13: Seeding GLPI categories, groups, SLAs..."
# Copy glpi_setup.py into the report-portal container and run it
# with the app token + password created in Step 12 (defaults in the script are stale)
docker cp glpi_setup.py report-portal:/tmp/ 2>/dev/null
docker exec -e GLPI_APP_TOKEN="$GLPI_APP_TOKEN" \
    -e GLPI_API_PASSWORD="DashenGLPI2024" \
    report-portal python3 /tmp/glpi_setup.py 2>&1 | tail -20

# ── Step 14 — Reload Zabbix cache again ────────
docker exec zabbix-server zabbix_server -R config_cache_reload 2>/dev/null || true

# ── Step 16 — Fix permissions ──────────────────
echo ""
echo "Step 16: Fixing filesystem permissions..."
if [ -d "ej-logs" ]; then
    sudo chown -R $USER:$USER ej-logs/ 2>/dev/null || true
    chmod 755 ej-logs/ 2>/dev/null || true
fi
if [ -f "filebeat.yml" ]; then
    sudo chown root:root filebeat.yml 2>/dev/null || true
    sudo chmod 644 filebeat.yml 2>/dev/null || true
fi
echo "Permissions OK"

# Restart Filebeat to pick up permission fix
docker compose restart filebeat 2>/dev/null || true

# ── Done ───────────────────────────────────────
echo ""
echo "======================================"
echo "SETUP COMPLETE"
echo "======================================"
echo ""
echo "Access your system:"
echo "  Zabbix:       http://localhost:8080   (Admin / zabbix)"
echo "  Grafana:      http://localhost:3002   (admin / dashen2024)"
echo "  GLPI:         http://localhost:8082   (glpi / DashenGLPI2024)"
echo "  Report Portal: http://localhost:8888  (see .env for credentials)"
echo "  OpenSearch:   http://localhost:5601   (admin / admin)"
echo "  pgAdmin:      http://localhost:5050   (admin@dashenbank.com / dashen2024)"
echo ""
echo "Next steps (manual, machine-specific):"
echo "  1. Zabbix Agent on host/WSL   → README Step 6-7"
echo "  2. EJ generator build/start   → README Step 8.4"
echo ""
