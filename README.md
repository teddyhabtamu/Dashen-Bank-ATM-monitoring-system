# Dashen Bank ATM Monitoring System

Monitors **1,227 ATMs** (798 GRG, 429 NCR) across **14 districts** in Ethiopia.
SNMPv2c polling via Dashen private OID root `1.3.6.1.4.1.99999`.

## Quick Start

**Prerequisites:** Ubuntu 22.04+, Git, Docker, internet connection.

```bash
# 1. Clone & enter
git clone <your-repo-url>
cd Dashen-Bank-ATM-monitoring-system

# 2. Create .env from example, then edit passwords
cp .env.example .env
nano .env    # at minimum set ADMIN_PASS and FLASK_SECRET_KEY

# 3. Run setup (10-15 min)
bash scripts/setup_new_machine.sh
```

The script automates everything:

| Step | What it does |
|------|-------------|
| 1-3 | Validates environment and project files |
| 4-6 | Builds Docker images and starts all services |
| 7 | Restores PostgreSQL database (1,227 ATMs) |
| 8 | Imports SNMP templates + registers hosts in Zabbix |
| 9-10 | Imports GLPI webhook mediatype + creates trigger action |
| 11-13 | Installs GLPI (if needed), enables REST API, creates API client, runs `glpi_setup.py` |
| 14 | Fixes filesystem permissions (ej-logs, filebeat) |

That's it — all services, Zabbix templates, GLPI categories/groups/SLAs, and Grafana dashboards are ready.

## Access

| Service | URL | Login |
|---------|-----|-------|
| Zabbix | http://localhost:8080 | Admin / zabbix |
| Grafana | http://localhost:3002 | admin / dashen2024 |
| GLPI | http://localhost:8082 | glpi / DashenGLPI2024 |
| Report Portal | http://localhost:8888 | admin / from `.env` |
| OpenSearch Dashboards | http://localhost:5601 | admin / admin |
| pgAdmin | http://localhost:5050 | admin@dashenbank.com / dashen2024 |

## Post-Setup (machine-specific, once per machine)

```bash
# 1. Build & start EJ generators
docker compose build atm-ej-001
docker compose up -d atm-ej-001 atm-ej-002 atm-ej-003 atm-ej-004 atm-ej-005

# 2. Install Zabbix Agent for ATM-001 (see Step 6-7 below)
```

## Ticket Lifecycle

Zabbix triggers (severity ≥ High) auto-create GLPI tickets. The webhook routes each ticket to the correct category, team, and SLA based on the trigger name:

| Trigger Pattern → | Category | Assigned To | SLA |
|---|---|---|---|
| Cash Empty / Cash Out | Cash Out | Cash Operations Team | Critical SLA (2hr) |
| Cash Low | Cash Low | Cash Operations Team | Medium SLA (8hr) |
| Cash Jam / Card Reader / Printer / High Temp | Hardware Error | ATM Field Engineers | High SLA (4hr) |
| Camera Fault / Camera Storage | Camera Failure | ATM Field Engineers | Medium SLA (8hr) |
| Network Link Down / Packet Loss / Latency | Network Issue | IT Network Team | High SLA (4hr) |
| Main Power / UPS Critical | Power / UPS Alert | ATM Field Engineers | High SLA (4hr) |
| UPS Battery Low / UPS on Battery | Power / UPS Alert | ATM Field Engineers | Medium SLA (8hr) |
| Cabinet Door / Safe Door / Intrusion / Vibration / Card Capture | Security Alert | ATM Ops Center | Critical SLA (2hr) |
| Out of Service / Offline | ATM Offline | ATM Field Engineers | Critical SLA (2hr) |
| Supervisor / Maintenance / Partial Service | ATM Offline | ATM Field Engineers | Medium SLA (8hr) |
| Transaction Failure / Anomaly | Transaction Anomaly | ATM Ops Center | High SLA (4hr) |
| Software Error | Software Error | ATM Field Engineers | High SLA (4hr) |
| *(no match)* | Transaction Anomaly | ATM Ops Center | Medium SLA (8hr) |

On recovery, the ticket is moved to **Pending** with an automated note — it is **not** auto-closed. An engineer must document a Root Cause Analysis (RCA) before closing.

## GLPI Category Tree

```
ATM Hardware Fault
  ├── NCR ATM Hardware         → Field Engineers, High SLA
  ├── GRG ATM Hardware         → Field Engineers, High SLA
  ├── Hardware Error           → Field Engineers, High SLA
  ├── Camera Failure           → Field Engineers, Medium SLA
  ├── Power / UPS Alert        → Field Engineers, High SLA
  └── ATM Offline              → Field Engineers, Critical SLA
ATM Network Issue
  └── Network Issue            → IT Network Team, High SLA
ATM Cash Issue
  ├── Cash Low                 → Cash Operations, Medium SLA
  └── Cash Out                 → Cash Operations, Critical SLA
ATM Software Error
  └── Software Error           → Field Engineers, High SLA
Transaction Anomaly            → Ops Center, High SLA
Security Alert                 → Ops Center, Critical SLA
```

## Developer Onboarding

A new developer can be fully operational with:

```bash
git clone <repo>
cd Dashen-Bank-ATM-monitoring-system
cp .env.example .env
# edit .env with their passwords
bash scripts/setup_new_machine.sh
```

The script is idempotent — safe to re-run any time.

## Re-running parts

```bash
# Re-sync Zabbix hosts/templates
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates

# Re-seed GLPI structure (idempotent)
docker cp glpi_setup.py report-portal:/tmp/
docker exec report-portal python3 /tmp/glpi_setup.py

# Backup/restore database
bash scripts/backup_db.sh
bash scripts/restore_db.sh
```

## Manual Steps (machine-specific)

### Zabbix Agent (for ATM-001 local checks)

```bash
# Install
wget https://repo.zabbix.com/zabbix/6.4/ubuntu/pool/main/z/zabbix-release/zabbix-release_6.4-1+ubuntu22.04_all.deb
sudo dpkg -i zabbix-release_6.4-1+ubuntu22.04_all.deb
sudo apt update && sudo apt install -y zabbix-agent2

# Configure
sudo nano /etc/zabbix/zabbix_agent2.conf
# Set: Server=127.0.0.1,172.16.0.0/12
# Set: ServerActive=<docker-gateway-ip>:10051
# Set: Hostname=ATM-001-Addis-Branch

sudo systemctl restart zabbix-agent2
docker exec zabbix-server zabbix_server -R config_cache_reload
```

### EJ generators (once per machine)

```bash
docker compose build atm-ej-001
docker compose up -d atm-ej-001 atm-ej-002 atm-ej-003 atm-ej-004 atm-ej-005
```

### OpenSearch index pattern (once)

1. Open http://localhost:5601
2. **Stack Management → Index Patterns → Create**
3. Pattern: `atm-electronic-journal`, Timestamp: `@timestamp`

## Credentials

| Role | Username | Password |
|------|----------|----------|
| Report Portal Admin | `admin` | from `.env` `ADMIN_PASS` |
| Report Portal Operator | `operator` | `operator123` |
| Report Portal Viewer | `viewer` | `viewer123` |
| Zabbix Admin | `Admin` | `zabbix` |
| Grafana Admin | `admin` | `dashen2024` |
| GLPI Admin | `glpi` | `DashenGLPI2024` |
| OpenSearch | `admin` | `admin` |
| PostgreSQL | `zabbix` | from `.env` `POSTGRES_PASSWORD` |

## Architecture

See `docs/architecture.md` for the full system design, data flow, and component diagram.

## Troubleshooting

See `docs/troubleshooting.md` for common issues and fixes.
