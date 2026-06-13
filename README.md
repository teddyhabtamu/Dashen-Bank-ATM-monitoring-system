# Dashen Bank ATM Monitoring System

## Quick Start on New Machine

### Prerequisites
- Ubuntu 22.04 LTS (or WSL2 on Windows)
- Git
- Internet connection for first setup

### Step 1 — Clone the repo
```bash
git clone YOUR_GITHUB_URL
cd zabbix-atm
```

### Step 2 — Run setup script
```bash
bash scripts/setup_new_machine.sh
```
This takes 5-10 minutes. It builds all Docker images,
starts all services, and restores the database.

### Step 3 — Import Zabbix configuration
1. Open http://localhost:8080 (Admin/zabbix)
2. Configuration → Templates → Import
   → select config/zabbix/template_dashen_atm_hardware.xml
3. Configuration → Hosts → Import
   → select config/zabbix/hosts_all_atms.xml
4. Administration → Media types → Import
   → select config/zabbix/media_types.xml
5. Configuration → Actions → Import
   → select config/zabbix/actions.xml

### Step 4 — Import Grafana dashboards
Grafana auto-loads datasources from config/grafana/datasources.yml
Dashboards auto-load from config/grafana/dashboards/

If dashboards did not load automatically:
1. Open http://localhost:3001 (admin/dashen2024)
2. Dashboards → Import → Upload JSON file
   → import each file from config/grafana/dashboards/

### Step 5 — Configure GLPI webhook
1. Open http://localhost:8082 (glpi/DashenGLPI2024)
2. Setup → General → API → Enable REST API
3. Add API client → copy App Token
4. In Zabbix → Media Types → GLPI Ticket
   → update app_token parameter with new token

### Service URLs
| Service | URL | Login |
|---|---|---|
| Zabbix | http://localhost:8080 | Admin/zabbix |
| Grafana | http://localhost:3002 | admin/dashen2024 |
| Kibana | http://localhost:5601 | no login |
| GLPI | http://localhost:8082 | glpi/DashenGLPI2024 |
| Report Portal | http://localhost:8888 | no login |
| pgAdmin | http://localhost:5050 | admin@dashenbank.com/dashen2024 |
