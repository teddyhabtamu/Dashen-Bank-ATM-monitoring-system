#!/bin/bash
# ============================================
# Dashen Bank ATM Monitoring System
# New Machine Setup Script
# Run this after cloning the repo
# ============================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "======================================"
echo "Dashen Bank ATM Monitoring Setup"
echo "======================================"

# Step 1 - Check Docker is installed
echo ""
echo "Step 1: Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker installed. Please log out and back in, then run this script again."
    exit 1
fi
echo "Docker OK"

# Step 2 - Check required files exist
echo ""
echo "Step 2: Checking project files..."
cd "$PROJECT_DIR"

required_files=(
    "docker-compose.yml"
    "filebeat.yml"
    "simulators/sim_engine.py"
    "simulators/txn_engine.py"
    "simulators/ej_engine.py"
    "simulators/snmp_agent.py"
    "simulators/state_manager.py"
    "simulators/common.py"
    "simulators/Dockerfile.atm-simulator"
    "report-portal/app.py"
    "report-portal/Dockerfile"
    "camel/iso8583_gateway.py"
    "camel/Dockerfile.gateway"
)

for f in "${required_files[@]}"; do
    if [ ! -f "$f" ]; then
        echo "Missing: $f"
        echo "Make sure you cloned the full repo."
        exit 1
    fi
done
echo "All files present OK"

# Step 3 - Create required directories
echo ""
echo "Step 3: Creating directories..."
mkdir -p ej-logs reports config/zabbix \
  config/grafana/dashboards config/postgres
echo "Directories OK"

# Step 4 - Build Docker images
echo ""
echo "Step 4: Building Docker images..."
docker compose build \
  atm-sim-engine atm-txn-engine atm-ej-engine state-manager \
  report-portal iso8583-gateway
echo "Build OK"

# Step 5 - Start all services
echo ""
echo "Step 5: Starting all services..."
docker compose up -d

echo ""
echo "Waiting 60 seconds for services to initialize..."
sleep 60

# Step 6 - Restore database
echo ""
echo "Step 6: Restoring database..."
if [ -f "config/postgres/atm_custom_tables.sql" ]; then
    bash scripts/restore_db.sh
else
    echo "No database backup found."
    echo "Creating fresh ATM locations..."
    docker exec zabbix-db psql -U zabbix \
      -d zabbix << 'SQLEOF'
CREATE TABLE IF NOT EXISTS atm_locations (
    atm_id VARCHAR(20) PRIMARY KEY,
    branch VARCHAR(100),
    district VARCHAR(100),
    city VARCHAR(100),
    region VARCHAR(100),
    latitude DECIMAL(10,7),
    longitude DECIMAL(10,7),
    terminal_id VARCHAR(20),
    vendor VARCHAR(50),
    model VARCHAR(50),
    install_date DATE,
    status VARCHAR(20) DEFAULT 'active'
);

INSERT INTO atm_locations VALUES
('ATM-001','Addis Ababa Main Branch',
 'Kirkos','Addis Ababa','Addis Ababa',
 9.0300,38.7578,'TID001','NCR',
 'SelfServ 34','2023-01-15','active'),
('ATM-002','Bole International Branch',
 'Bole','Addis Ababa','Addis Ababa',
 8.9806,38.7894,'TID002','NCR',
 'SelfServ 34','2023-03-20','active'),
('ATM-003','Merkato Branch',
 'Addis Ketema Sub-City','Addis Ababa',
 'Addis Ababa',9.0350,38.7469,'TID003',
 'NCR','SelfServ 34','2023-02-10','active'),
('ATM-004','Hawassa Branch',
 'Hawassa Central','Hawassa',
 'Sidama Region',7.0621,38.4760,'TID004',
 'NCR','SelfServ 34','2023-04-05','active'),
('ATM-005','Dire Dawa Branch',
 'Dire Dawa Central','Dire Dawa',
 'Dire Dawa',9.5931,41.8661,'TID005',
 'NCR','SelfServ 34','2023-05-12','active')
ON CONFLICT (atm_id) DO NOTHING;
SQLEOF
    echo "ATM locations created OK"
fi

# Step 7 - Verify everything running
echo ""
echo "Step 7: Verifying services..."
echo ""
docker ps --format \
  "table {{.Names}}\t{{.Status}}\t{{.Ports}}" \
  | grep -v "Exited"


# Step 8 - Print access URLs
echo ""
echo "======================================"
echo "SETUP COMPLETE"
echo "======================================"
echo ""
echo "Access your system:"
echo ""
echo "  Zabbix:       http://localhost:8080"
echo   "  Grafana:      http://localhost:3002"
echo "  OpenSearch Dashboards: http://localhost:5601"
echo "  GLPI:         http://localhost:8082"
echo "  Report Portal: http://localhost:8888"
echo "  pgAdmin:      http://localhost:5050"
echo ""
echo "Default credentials:"
echo "  Zabbix:  Admin / zabbix"
echo "  Grafana: admin / dashen2024"
echo "  GLPI:    glpi / DashenGLPI2024"
echo "  pgAdmin: admin@dashenbank.com / dashen2024"
echo ""
# Step 8 — Import Zabbix templates and register ATM hosts
echo ""
echo "Step 8: Importing Zabbix templates and registering ATM hosts..."
echo "  (may take 2-3 minutes for 1,200+ ATMs)"
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates || {
    echo "  WARNING: Zabbix host sync failed. You can re-run later:"
    echo "  python3 scripts/sync_atms_to_zabbix.py --apply --import-templates"
}
echo ""

echo ""
echo "IMPORTANT: You still need to manually configure:"
echo "  1. GLPI webhook (see README Step 5)"
echo "  2. Zabbix Agent on host/WSL (see README Step 6)"
echo ""
echo "See README.md for complete steps."
