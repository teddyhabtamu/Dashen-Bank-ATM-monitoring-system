# Dashen Bank — ATM Monitoring System
# UAT Migration Guide v1 (2026-08-20)

## From Single-Host PoC to the 2-VM UAT Environment

**Companion to:** `docs/Production_Migration_Guide_v2.md` (Production Migration Guide v3) — read that first for the overall plan
**Based on:** Server Infrastructure Specification (Revised — August 2026)
**Target:** UAT — RHEL 9, **2 VMs (UAT-01, UAT-02)**, 10–20 simulated ATMs over real SNMP
**Purpose:** Validate the *exact* production architecture (cross-VM links, SNMP-native collection, ISO 8583 gateway, EJ pipeline) on isolated hardware before anything touches production.

---

# 0. Read This First — What UAT Is For (and Is Not)

1. **UAT replicates the shape of production, not its scale.** Production is 3 VMs serving 1,202 real ATMs (798 GRG + 429 NCR — see Production Guide §0.2). UAT is 2 VMs serving 10–20 *simulated* ATMs. The point is to prove the architecture works before the bank's money depends on it.

2. **The collection path in UAT must be real SNMP, not HTTP.** The old PoC templates poll `http://.../oid/...` (HTTP agent). Production will poll real ATMs over **SNMP (UDP 161)**. The repo already contains the pieces that make UAT production-accurate:
   - `simulators/snmp_agent.py` — each simulator also serves its OID values as **real SNMP** (pysnmp) on the same per-ATM UDP port (default `1161`+), community **`dashen_sim`**, synthetic enterprise OID root **`.1.3.6.1.4.1.99999`** (NCR family `.99999.8.*`, GRG family `.99999.9.*`).
   - `config/zabbix/template_ncr_snmp.xml` and `config/zabbix/template_grg_snmp.xml` — **SNMP agent** templates (items, triggers, value maps) using those OIDs.
   - So in UAT, Zabbix polls simulators with genuine SNMP-agent items — the same item type production uses. At production cutover, only the **port** (161) and the **OID tree** (real NCR/GRG MIB) change; no item-type rewrite. See `docs/collection-strategy.md` §4.

3. **Deploy UAT-02 first, then UAT-01.** UAT-02 holds OpenSearch, which UAT-01's Report Portal and Filebeat depend on. UAT-01 holds PostgreSQL, which UAT-02's ISO 8583 gateway depends on — so the gateway is started last (Phase C), after UAT-01's database is confirmed.

4. **One deliberate deviation from the Production Guide's shorthand:** Filebeat runs on **UAT-01** (where the simulators generate EJ logs) and ships to **UAT-02**'s OpenSearch over the network. In production, Filebeat stays on DATA-01 with OpenSearch (EJ logs arrive there via network share/SFTP). UAT tests the same Filebeat→OpenSearch pipeline, just with the source and sink on different VMs — which is actually the harder case. Don't "fix" UAT to match; the deviation is intentional.

---

# 1. UAT Architecture

## 1.1 The 2 UAT VMs

| VM | Spec | Runs |
|---|---|---|
| **UAT-01** (172.26.208.176) | 4 vCPU / 8 GB / 500 GB | PostgreSQL, Zabbix Server + Web + Agent, MariaDB + GLPI, Grafana + Renderer, Report Portal, Anomaly Detector, Network Correlator, State Manager, Simulators (sim/txn/EJ engines), Filebeat |
| **UAT-02** (172.26.21.50) | 4 vCPU / 8 GB / 200 GB | OpenSearch, OpenSearch Dashboards, ISO 8583 Gateway |

```
┌────────────────────────────────────────────────────────────────────┐
│                      DASHEN BANK INTERNAL NETWORK                   │
│                                                                    │
│  ┌──────────────────────────┐         ┌──────────────────────────┐ │
│  │ UAT-01  4 vCPU / 8 GB   │         │ UAT-02  4 vCPU / 8 GB   │ │
│  │ 500 GB                  │         │ 200 GB                  │ │
│  │                         │         │                          │ │
│  │ zabbix-server           │         │ opensearch               │ │
│  │ zabbix-web              │         │ os-dashboards            │ │
│  │ grafana / renderer      │         │ iso8583-gateway          │ │
│  │ glpi + mariadb          │         │                          │ │
│  │ report-portal ──────────┼─────────┼─> OS 172.26.21.50:9200     │ │
│  │ filebeat ───────────────┼─────────┼─> OS 172.26.21.50:9200     │ │
│  │ anomaly-detector        │         │  Ports: 9200, 5601, 9876 │ │
│  │ network-correlator      │         └───────────┬──────────────┘ │
│  │ state-manager           │                     │               │
│  │ atm-sim/txn/ej engines  │                     │               │
│  │ ┌─────────────────────┐ │         Gateway      │               │
│  │ │ UDP 1161+ SNMP sims │ │         writes ──────┼─> PG 172.26.208.176:5432
│  │ └─────────────────────┘ │                     │               │
│  │                         │                     │               │
│  │ Ports: 5432, 8080, 3000,│                     │               │
│  │ 8082, 8888, 1161-1260  │                     │               │
│  └──────────────────────────┘                     │               │
└────────────────────────────────────────────────────────────────────┘
```

## 1.2 Cross-VM Links (the whole point of UAT)

| From | To | Port | What |
|---|---|---|---|
| UAT-01 (Report Portal) | UAT-02 (OpenSearch) | TCP 9200 | EJ search queries |
| UAT-01 (Filebeat) | UAT-02 (OpenSearch) | TCP 9200 | EJ log shipping |
| UAT-02 (ISO 8583 Gateway) | UAT-01 (PostgreSQL) | TCP 5432 | Transactions written to `atm_transactions` |
| UAT-01 (Zabbix) | UAT-01 (simulators) | UDP 1161+ | SNMP polling (local) |

**Rule of thumb:** services on the *same* VM talk by container name (Docker DNS); services on *different* VMs talk by IP address. The only cross-VM IPs in UAT are the three rows above.

## 1.3 Firewall Rules to Request

**Division of labor (confirmed August 2026):** VM provisioning, VLANs, security groups, and firewall rules are handled by the Cloud & Core / Network / Security teams (see the Rahel Kiros & Jemil J. email thread). **You do not request these — you verify them** with the reachability checks in Phases A–C.

**Network layout (confirmed with Cloud & Core / Security, August 2026):** UAT-01 is on **VLAN 4029** (172.26.208.0/24), UAT-02 on **VLAN 4021** (172.26.21.0/24) — the UAT cross-VM links below are **inter-VLAN**. The central repo server **172.25.37.4** reaches both VMs for update activity.

| From | To | Port | Purpose |
|---|---|---|---|
| UAT-01 (VLAN 4029) | UAT-02 (VLAN 4021) | TCP 9200 | Filebeat + Report Portal → OpenSearch |
| UAT-02 (VLAN 4021) | UAT-01 (VLAN 4029) | TCP 5432 | Gateway → PostgreSQL |
| Staff workstations / laptop | UAT-01 | TCP 8080, 3000, 8082, 8888 | Zabbix web, Grafana, GLPI, Report Portal |
| Staff workstations / laptop | UAT-02 | TCP 5601 | OpenSearch Dashboards (optional) |
| Repo server 172.25.37.4 | UAT-01 + UAT-02 | TCP 443, 80 | Update activity (requested by Cloud & Core, Aug 12) |

**All rules internal-only.** Nothing exposed to the public internet — UAT included.

---

# 2. Prerequisites

- [ ] **UAT-01 and UAT-02 provisioned** — 4 vCPU / 8 GB RAM each, 500 GB / 200 GB disk (per the revised spec)
- [ ] **SSH access** to both VMs (your user in the `wheel` group)
- [ ] **RHEL 9 subscription** or local repo access
- [ ] **Static IPs** for both VMs (confirmed with IT, August 2026):

  | VM | Server Name (per IT mapping) | VLAN / Network | IP |
  |---|---|---|---|
  | UAT-01 | DBHQUATATMMONAPP | VLAN 4029 — 172.26.208.0/24 (gw 172.26.208.1) | 172.26.208.176 |
  | UAT-02 | DBHQUATATMMONDB | VLAN 4021 — 172.26.21.0/24 (gw 172.26.21.1) | 172.26.21.50 |

- [ ] **Firewall rules** (§1.3) — handled by Cloud & Core / Network / Security; you only verify reachability in Phases A–C
- [ ] **GitHub SSH key** or personal access token (to clone the repo on both VMs)
- [ ] **`net-snmp-utils`** installable on UAT-01 (for verifying simulator SNMP)

> Tip: add `/etc/hosts` entries on both VMs (`172.26.208.176 uat-01`, `172.26.21.50 uat-02`) so commands and logs are readable.

---

# 3. UAT Deploy Files (Already in the Repo)

**The files below already exist in the repo** (created August 2026) with the confirmed IPs baked in: `deploy/uat/docker-compose-uat-vm1.yml`, `deploy/uat/docker-compose-uat-vm2.yml`, `deploy/uat/.env.uat`, `config/grafana/datasources-uat.yml`, `config/postgres-uat/postgresql-custom.conf`. Read this section to understand what they contain; override only if you need a change.

## 3.1 UAT-01 compose file — `deploy/uat/docker-compose-uat-vm1.yml`

Everything except OpenSearch/Dashboards/Gateway. Within this file, services use container names (same VM). The only IPs are the two cross-VM ones: Report Portal's `OS_HOST` and Filebeat's output.

```yaml
services:
  postgres:
    image: postgres:15
    container_name: zabbix-db
    environment:
      POSTGRES_DB: "zabbix"
      POSTGRES_USER: "zabbix"
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./config/postgres-uat/postgresql-custom.conf:/etc/postgresql/postgresql.conf.d/custom.conf:ro
    ports:
      - "5432:5432"
    restart: unless-stopped

  zabbix-server:
    image: zabbix/zabbix-server-pgsql:rhel-6.4-latest
    container_name: zabbix-server
    environment:
      DB_SERVER_HOST: "postgres"
      POSTGRES_DB: "zabbix"
      POSTGRES_USER: "zabbix"
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
    ports:
      - "10051:10051"
    depends_on:
      - postgres
    restart: unless-stopped

  zabbix-web:
    image: zabbix/zabbix-web-nginx-pgsql:rhel-6.4-latest
    container_name: zabbix-web
    environment:
      DB_SERVER_HOST: "postgres"
      POSTGRES_DB: "zabbix"
      POSTGRES_USER: "zabbix"
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
      ZBX_SERVER_HOST: "zabbix-server"
      PHP_TZ: "Africa/Addis_Ababa"
    ports:
      - "8080:8080"
    depends_on:
      - zabbix-server
    restart: unless-stopped

  zabbix-agent:
    image: zabbix/zabbix-agent2:rhel-6.4-latest
    container_name: zabbix-agent
    network_mode: "host"
    privileged: true
    environment:
      ZBX_HOSTNAME: "Zabbix-Server-UAT01"
      ZBX_SERVER_HOST: "127.0.0.1"
    restart: unless-stopped
    depends_on:
      - zabbix-server

  mariadb:
    image: mariadb:10.11
    container_name: glpi-db
    environment:
      MYSQL_ROOT_PASSWORD: "${MYSQL_ROOT_PASSWORD}"
      MYSQL_DATABASE: "glpi"
      MYSQL_USER: "glpi"
      MYSQL_PASSWORD: "${MYSQL_PASSWORD}"
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
    volumes:
      - mariadb-data:/var/lib/mysql
    restart: unless-stopped

  glpi:
    image: diouxx/glpi:latest
    container_name: glpi
    environment:
      TZ: "Africa/Addis_Ababa"
      MARIADB_HOST: "mariadb"
      MARIADB_DATABASE: "glpi"
      MARIADB_USER: "glpi"
      MARIADB_PASSWORD: "${MYSQL_PASSWORD}"
    ports:
      - "8082:80"
    volumes:
      - glpi-root:/var/www/html/glpi
    depends_on:
      - mariadb
    restart: unless-stopped

  grafana:
    image: grafana/grafana:10.2.6
    container_name: grafana
    user: "root"
    environment:
      GF_SECURITY_ADMIN_USER: "admin"
      GF_SECURITY_ADMIN_PASSWORD: "${GRAFANA_ADMIN_PASSWORD}"
      GF_INSTALL_PLUGINS: "alexanderzobnin-zabbix-app 4.4.5"
      GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS: "alexanderzobnin-zabbix-datasource,alexanderzobnin-zabbix-app"
      GF_PANELS_DISABLE_SANITIZE_HTML: "true"
      GF_RENDERING_SERVER_URL: "http://grafana-renderer:8081/render"
      GF_RENDERING_CALLBACK_URL: "http://grafana:3000/"
    ports:
      - "3000:3000"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./index.html:/usr/share/grafana/public/views/index.html:ro
      - ./logo.png:/usr/share/grafana/public/img/dashen-logo.png:ro
      - ./entrypoint.sh:/entrypoint.sh:ro
      - ./config/grafana/datasources-uat.yml:/etc/grafana/provisioning/datasources/datasources.yml:ro
      - ./config/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
    entrypoint: ["sh", "/entrypoint.sh"]
    restart: unless-stopped

  grafana-renderer:
    image: grafana/grafana-image-renderer:latest
    container_name: grafana-renderer
    environment:
      ENABLE_METRICS: "true"
    ports:
      - "8081:8081"
    restart: unless-stopped

  report-portal:
    build:
      context: ./report-portal
      dockerfile: Dockerfile
    container_name: report-portal
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      GRAFANA_URL: "http://172.26.208.176:3000"
      REPORT_PORTAL_PORT: "8888"
      OS_HOST: "172.26.21.50:9200"
      OS_INDEX: "atm-ej-live-*,atm-electronic-journal"
    volumes:
      - ./report-portal:/app:rw
    ports:
      - "8888:8888"
    depends_on:
      - postgres
    restart: unless-stopped

  anomaly-detector:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: anomaly-detector
    command: python3 /app/anomaly_detector.py
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      CHECK_INTERVAL: "60"
      VELOCITY_WINDOW: "10"
      VELOCITY_LIMIT: "3"
      LARGE_TXN_ETB: "8000"
      RAPID_WINDOW: "5"
      RAPID_LIMIT: "5"
      FAILURE_WINDOW: "15"
      FAILURE_THRESHOLD: "0.4"
    volumes:
      - ./anomaly_detector.py:/app/anomaly_detector.py:ro
    depends_on:
      - postgres
    restart: unless-stopped

  network-correlator:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: network-correlator
    command: python3 /app/network_correlator.py
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      ZABBIX_URL: "http://zabbix-web:8080/api_jsonrpc.php"
      ZABBIX_USER: "Admin"
      ZABBIX_PASS: "zabbix"
      CHECK_INTERVAL: "120"
      LATENCY_THRESHOLD: "200"
      LOSS_THRESHOLD: "10"
    volumes:
      - ./network_correlator.py:/app/network_correlator.py:ro
    depends_on:
      - postgres
    restart: unless-stopped

  state-manager:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: state-manager
    command: python3 /app/state_manager.py
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
    depends_on:
      - postgres
    restart: unless-stopped

  atm-sim-engine:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: atm-sim-engine
    command: python3 -u sim_engine.py
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      SIM_PORT_MIN: "1161"
      SIM_PORT_MAX: "1260"
      ATM_COUNT: "10"
    ports:
      - "1161-1260:1161-1260/udp"
    depends_on:
      - postgres
    restart: unless-stopped

  atm-txn-engine:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: atm-txn-engine
    command: python3 -u txn_engine.py
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
    depends_on:
      - postgres
    restart: unless-stopped

  atm-ej-engine:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: atm-ej-engine
    command: python3 -u ej_engine.py
    environment:
      DB_HOST: "postgres"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
    volumes:
      - ./ej-logs:/app/ej-logs
    depends_on:
      - postgres
    restart: unless-stopped

  filebeat:
    image: docker.elastic.co/beats/filebeat-oss:7.12.1
    container_name: filebeat
    user: root
    volumes:
      - ./filebeat.yml:/usr/share/filebeat/filebeat.yml:ro
      - ./ej-logs:/var/log/atm-ej:ro
    depends_on:
      - postgres
    restart: unless-stopped

volumes:
  pgdata:
  mariadb-data:
  glpi-root:
  grafana-data:
```

> **Why do simulators expose UDP ports on the host (`1161-1260/udp`)?** Zabbix polls them over SNMP on those ports. Real production ATMs are reached over UDP 161 on the ATM network — same mechanism, different port.

## 3.2 UAT-02 compose file — `deploy/uat/docker-compose-uat-vm2.yml`

```yaml
services:
  opensearch:
    image: opensearchproject/opensearch:2.14.0
    container_name: opensearch
    environment:
      - discovery.type=single-node
      - OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g
      - DISABLE_SECURITY_PLUGIN=true
      - compatibility.override_main_response_version=true
    volumes:
      - os-data:/usr/share/opensearch/data
    ports:
      - "9200:9200"
    restart: unless-stopped

  opensearch-dashboards:
    image: opensearchproject/opensearch-dashboards:2.14.0
    container_name: opensearch-dashboards
    environment:
      - OPENSEARCH_HOSTS=http://opensearch:9200
      - DISABLE_SECURITY_DASHBOARDS_PLUGIN=true
    ports:
      - "5601:5601"
    depends_on:
      - opensearch
    restart: unless-stopped

  iso8583-gateway:
    build:
      context: ./camel
      dockerfile: Dockerfile.gateway
    container_name: iso8583-gateway
    environment:
      MODE: "simulation"
      DB_HOST: "172.26.208.176"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      INTERVAL: "10"
    ports:
      - "9876:9876"
    restart: unless-stopped

volumes:
  os-data:
```

> **UAT-02 only has 8 GB RAM**, so OpenSearch's JVM heap is 2 GB (the production 16 GB on DATA-01 follows the same 50%-of-RAM rule, just at scale). Don't copy the production `-Xms16g` here; it will OOM.

## 3.3 UAT environment file — `deploy/uat/.env.uat`

```bash
# ============================
# Dashen Bank ATM Monitoring
# UAT Environment Variables
# ============================
# NOTE: Change passwords before UAT goes live; chmod 600 this file

POSTGRES_PASSWORD=<uat-strong-password>
GRAFANA_ADMIN_PASSWORD=<uat-strong-password>
MYSQL_ROOT_PASSWORD=<uat-strong-password>
MYSQL_PASSWORD=<uat-strong-password>
DB_PASS=<must-match-POSTGRES_PASSWORD>
GLPI_APP_TOKEN=<generated-during-glpi-setup>
GLPI_API_PASSWORD=<password-for-glpi-api>
GRAFANA_URL=http://172.26.208.176:3000
REPORT_PORTAL_PORT=8888
```

```bash
openssl rand -base64 16   # run 4 times — 4 different passwords
```

## 3.4 UAT PostgreSQL tuning — `config/postgres-uat/postgresql-custom.conf`

UAT-01 has 8 GB total RAM shared with Zabbix, Grafana, GLPI, Report Portal and the simulators — PostgreSQL gets a modest share:

```
# Dashen Bank ATM — UAT PostgreSQL tuning
# Target: UAT-01 with 8 GB RAM (shared with the rest of the stack)
# Production uses config/postgres-production/postgresql-custom.conf instead

shared_buffers = '512MB'
effective_cache_size = '3GB'
work_mem = '16MB'
maintenance_work_mem = '128MB'
wal_buffers = '4MB'
max_connections = '100'
checkpoint_completion_target = '0.9'
max_wal_size = '2GB'
min_wal_size = '512MB'
random_page_cost = '1.1'
autovacuum_max_workers = '3'
```

## 3.5 UAT Grafana datasources — `config/grafana/datasources-uat.yml`

The PoC's `datasources.yml` uses container names; this version uses real IPs (only the OpenSearch URL crosses VMs — the rest is local to UAT-01):

```yaml
apiVersion: 1

datasources:
  - name: Zabbix-ATM
    type: alexanderzobnin-zabbix-datasource
    access: proxy
    url: http://172.26.208.176:8080/api_jsonrpc.php
    jsonData:
      username: Admin
    secureJsonData:
      password: zabbix

  - name: ATM-Transactions
    type: postgres
    url: 172.26.208.176:5432
    database: zabbix
    user: zabbix
    secureJsonData:
      password: <uat-postgres-password>
    jsonData:
      sslmode: disable
      postgresVersion: 1500

  - name: EJ-OpenSearch
    type: elasticsearch
    url: http://172.26.21.50:9200
    jsonData:
      index: atm-*
      timeField: "@timestamp"
      esVersion: "7.10.0"
      logMessageField: message
      logLevelField: status
```

## 3.6 UAT Filebeat — `filebeat.yml` (repo root, unchanged)

The repo's `filebeat.yml` already points at local paths; the only production-grade change is the output host — **edit the repo `filebeat.yml`** so the output targets UAT-02:

```yaml
filebeat.inputs:
  - type: log
    enabled: true
    paths:
      - /var/log/atm-ej/ATM-*.log
    fields:
      log_type: atm_ej
    fields_under_root: true
    multiline.pattern: '^\d{4}-\d{2}-\d{2}'
    multiline.negate: true
    multiline.match: after

output.elasticsearch:
  hosts: ["172.26.21.50:9200"]
  index: "atm-ej-live-%{+yyyy.MM.dd}"
  pipeline: "atm_ej_parser"

setup.ilm.enabled: false
setup.template.name: "atm-ej-live"
setup.template.pattern: "atm-ej-live-*"
```

> In production, Filebeat's output becomes `["localhost:9200"]` (same VM as OpenSearch on DATA-01) and the input paths add `/data/real-ej-logs/*.log` — see Production Guide §4.3.4. UAT deliberately exercises the harder network-output case.
---

# 4. Phase A — Deploy UAT-02 (OpenSearch + OpenSearch Dashboards)

## Step A.1 — SSH in and install Docker

RHEL 9 does not ship Docker. Install it (identical to Production Guide §5.1.2):

```bash
ssh <your-username>@172.26.21.50

# Remove podman if present (it conflicts with Docker)
sudo dnf remove -y podman buildah

# Add Docker's official RHEL repository
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo

# Install Docker
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Start Docker and enable on boot
sudo systemctl enable docker
sudo systemctl start docker

# Add your user to the docker group
sudo usermod -aG docker $USER
exit   # then SSH back in for the group change
```

Verify after reconnecting:

```bash
docker --version
docker compose version
```

## Step A.2 — Configure sysctl for OpenSearch

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
sudo swapoff -a
```

## Step A.3 — Clone the repo and set up .env

```bash
sudo dnf install -y git
cd /opt
sudo mkdir -p atm-monitoring
sudo chown $USER:$USER atm-monitoring
git clone <YOUR_GITHUB_URL> atm-monitoring
cd atm-monitoring

cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env          # set the UAT passwords (same values as UAT-01)
```

## Step A.4 — Start OpenSearch + OpenSearch Dashboards

```bash
docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d opensearch opensearch-dashboards

echo "Waiting 60 seconds for OpenSearch to initialize..."
sleep 60

curl -s http://localhost:9200
# Expected: JSON with "cluster_name" and "version"
```

## Step A.5 — Verify (from UAT-01, after Phase B starts)

OpenSearch is ready when these work from **UAT-01** (this validates the firewall rule `UAT-01 → UAT-02:9200`):

```bash
curl -s http://172.26.21.50:9200
curl -s "http://172.26.21.50:9200/_cat/indices?v"
```

---

# 5. Phase B — Deploy UAT-01 (Everything Else)

## Step B.1 — SSH in, install Docker, clone repo

Same as Steps A.1 and A.3, on UAT-01:

```bash
ssh <your-username>@172.26.208.176
```

## Step B.2 — Set up environment and directories

```bash
cd /opt/atm-monitoring

cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env          # SAME passwords as UAT-02's .env

mkdir -p ej-logs config/postgres-uat config/grafana/dashboards
sudo mkdir -p /data/real-ej-logs
sudo chown $USER:$USER /data/real-ej-logs
```

## Step B.3 — Start PostgreSQL first

```bash
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d postgres

echo "Waiting for PostgreSQL..."
for i in {1..30}; do
  if docker exec zabbix-db pg_isready -U zabbix -d zabbix &>/dev/null; then
    echo "PostgreSQL is ready!"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 3
done
```

## Step B.4 — Create the custom tables

```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql

docker exec zabbix-db psql -U zabbix -d zabbix -c "\dt"
# Should show: atm_locations, atm_transactions, atm_anomalies, atm_network_*, etc.
```

## Step B.5 — Allow the ISO 8583 gateway to connect from UAT-02

The gateway on UAT-02 writes transactions to this database over the network. PostgreSQL in the container must listen on all interfaces and accept UAT-02:

```bash
ss -tlnp | grep 5432
# Should show 0.0.0.0:5432; if only 127.0.0.1:5432:
docker exec zabbix-db bash -c "echo \"listen_addresses = '*'\" >> /var/lib/postgresql/data/postgresql.conf"

docker exec zabbix-db bash -c "echo 'host all all 172.26.21.0/24 md5' >> /var/lib/postgresql/data/pg_hba.conf"

docker compose -f deploy/uat/docker-compose-uat-vm1.yml restart postgres
```

## Step B.6 — Build custom images and start everything

```bash
# Build the images that come from this repo's source (not a registry)
docker compose -f deploy/uat/docker-compose-uat-vm1.yml build --no-cache \
  atm-sim-engine atm-txn-engine atm-ej-engine state-manager \
  report-portal

# Start everything else
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d
sleep 60
```

## Step B.7 — Verify all containers

```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v "Exited"
```

You should see: postgres (as `zabbix-db`), zabbix-server, zabbix-web, zabbix-agent, mariadb (as `glpi-db`), glpi, grafana, grafana-renderer, report-portal, anomaly-detector, network-correlator, state-manager, atm-sim-engine, atm-txn-engine, atm-ej-engine, filebeat — all `Up`.

## Step B.8 — Verify the simulators answer SNMP

This is the **critical UAT check**: the simulators must respond to genuine SNMP GETs before you create Zabbix hosts.

```bash
sudo dnf install -y net-snmp-utils

# Walk the NCR-family OIDs served by the first simulator (UDP 1161)
snmpwalk -v2c -c dashen_sim -On 127.0.0.1:1161 .1.3.6.1.4.1.99999.8

# GRG family on the next port
snmpwalk -v2c -c dashen_sim -On 127.0.0.1:1162 .1.3.6.1.4.1.99999.9

# Expected: a row per OID, e.g.
# .1.3.6.1.4.1.99999.8.1.0 = INTEGER: 1
```

If this returns nothing: check the simulator container logs (`docker logs atm-sim-engine --tail 50` for `[SNMP] ... responder on UDP :1161`), the UDP port mapping, and that `snmp_agent.py`'s `pysnmp` import worked (it's in `Dockerfile.atm-simulator`).

> **What you just proved:** Zabbix will poll simulators with SNMP-agent items (production-accurate). At cutover the only changes are port 161 and the real vendor OID tree. This is the mitigation called out in Production Guide §0.1 — do it here in UAT, not at cutover.

---

# 6. Phase C — Start the ISO 8583 Gateway (UAT-02)

The gateway needs UAT-01's PostgreSQL, so it starts now — after Phase B.

## Step C.1 — Start the gateway

```bash
ssh <your-username>@172.26.21.50
cd /opt/atm-monitoring

docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d iso8583-gateway
sleep 15
docker ps | grep iso8583-gateway
docker logs iso8583-gateway --tail 5
# Expected: "MODE: SIMULATION" and "generating 1 transaction every 10 seconds"
```

## Step C.2 — Verify transactions arrive at UAT-01's database

```bash
ssh <your-username>@172.26.208.176
docker exec zabbix-db psql -U zabbix -d zabbix -c \
  "SELECT source, COUNT(*), MAX(recorded_at) FROM atm_transactions GROUP BY source;"
# Expected: source = 'ISO8583_SIM', count growing
```

**What you just proved:** the cross-VM path `UAT-02 → UAT-01:5432` works — the same path production uses (`GWY-01 → DATA-01:5432`).

---

# 7. Phase D — Import Zabbix Configuration (SNMP-Native)

## Step D.1 — Import templates, hosts, media types

Browse to `http://172.26.208.176:8080`, log in `Admin` / `zabbix`:

1. **Configuration → Templates → Import**:
   - `config/zabbix/template_ncr_snmp.xml` → template `Dashen Bank ATM Hardware`
   - `config/zabbix/template_grg_snmp.xml` → template `Dashen Bank ATM Hardware - GRG`
   - (optional) `config/zabbix/zbx_export_templates.xml` — the old HTTP template, for reference only. Do not link it to hosts.
2. **Configuration → Hosts → Import** → `config/zabbix/zbx_export_hosts.xml` (Zabbix agent host for the VM itself — fine as-is)
3. **Administration → Media types → Import** → `config/zabbix/zbx_export_mediatypes.xml`

> These SNMP templates use item type **SNMP agent** with OIDs under `.1.3.6.1.4.1.99999.8.*` (NCR) / `.99999.9.*` (GRG) — matching what `snmp_agent.py` serves.

## Step D.2 — Create the simulator hosts in Zabbix

For each simulator ATM (start with 3–5), create a host:

1. **Configuration → Hosts → Create host**
2. **Host name:** `ATM-001 | NCR | Sim` (convention: `<ID> | <VENDOR> | <BRANCH>`)
3. **Host groups:** `Dashen Bank ATMs` (create the group if missing)
4. **Interfaces → Add → SNMP:**
   - IP: `127.0.0.1` (simulators run on this VM)
   - Port: `1161` (each ATM gets the next port: 1161, 1162, ...)
   - SNMP version: `SNMPv2`, community: `dashen_sim`
5. **Templates:** link `Dashen Bank ATM Hardware` (NCR sims) or `Dashen Bank ATM Hardware - GRG` (GRG sims)
6. **Macros:**
   - `{$ATM_PORT}` = the ATM's UDP port (e.g. `1161`)
   - `{$ATM_VENDOR}` = `NCR` or `GRG`
7. **Add**

> If you later run `scripts/sync_atms_to_zabbix.py`, verify it creates SNMP interfaces (not the old HTTP ones) — or fix the interfaces manually after sync. The UAT hosts must be SNMP, or nothing validates.

## Step D.3 — Verify collection

After 1–2 minutes:

1. **Monitoring → Latest data** — filter by `ATM-001`: values populating for all ~30 items
2. **Monitoring → Problems** — simulators occasionally inject faults; a trigger should fire and (after GLPI wiring, Phase E) create a ticket
3. **Zabbix → Reports → Availability** — expect 100% availability for simulator hosts

**What you just proved:** SNMP-agent templates + Zabbix SNMP polling work end-to-end. This is the exact mechanism production uses for 1,202 real ATMs.

---

# 8. Phase E — Verify Grafana, Report Portal, GLPI, EJ

## Step E.1 — Grafana

- Browse to `http://172.26.208.176:3000`
- All 6 dashboards show simulator data
- **Connections → Data sources:** `Zabbix-ATM` (172.26.208.176:8080), `ATM-Transactions` (172.26.208.176:5432), `EJ-OpenSearch` (172.26.21.50:9200) all return "Success" on Save & Test

## Step E.2 — Report Portal

- Browse to `http://172.26.208.176:8888`
- Generate a PDF/Excel/CSV report — the transactions come from local PostgreSQL, the EJ counts come from UAT-02's OpenSearch (cross-VM, via `OS_HOST: 172.26.21.50:9200`)

## Step E.3 — GLPI

- Browse to `http://172.26.208.176:8082` and complete the install wizard (this was fixed on the sandbox — same flow)
- **Setup → General → API:** enable REST API; create an API client (get `app_token`)
- Update `.env` values `GLPI_APP_TOKEN` / `GLPI_API_PASSWORD`, restart the containers that use them
- **Administration → Media types:** the GLPI Ticket media type — set `glpi_url` to `http://172.26.208.176:8082`
- Trigger a fault on a simulator ATM → a GLPI ticket should auto-create

## Step E.4 — EJ logs in OpenSearch

```bash
# On UAT-01: EJ logs are generated here and shipped by Filebeat to UAT-02
ls ej-logs/          # ATM-*.log files appear

# On UAT-02: confirm indices exist
curl -s http://localhost:9200/_cat/indices?v
# Expected: atm-ej-live-YYYY.MM.DD with docs

# Browse OpenSearch Dashboards: http://172.26.21.50:5601
# Create index pattern: atm-ej-live-*, time field @timestamp
```

If no docs: check `docker logs filebeat --tail 20` on UAT-01 (permissions on `ej-logs/` — see Troubleshooting).

---

# 9. UAT End-to-End Sign-Off Checklist

- [ ] All containers running on both VMs (`docker ps` shows nothing Exited)
- [ ] `snmpwalk -v2c -c dashen_sim 127.0.0.1:1161 .1.3.6.1.4.1.99999.8` returns data
- [ ] Zabbix hosts show data for every item (no "Not supported", no timeouts)
- [ ] A simulator fault produces a Zabbix problem, and a GLPI ticket auto-creates
- [ ] Grafana: 6 dashboards show live data; all 3 datasources test OK
- [ ] Report Portal: PDF + Excel + CSV reports generate with real UAT data
- [ ] ISO 8583 Gateway: `ISO8583_SIM` rows growing in `atm_transactions` on UAT-01
- [ ] EJ: `atm-ej-live-*` indices have documents; OpenSearch Dashboards searchable
- [ ] Anomaly detector + network correlator produce records (`atm_anomalies`, `atm_network_*`)
- [ ] Restart a VM, confirm everything comes back (`restart: unless-stopped`)

---

# 10. UAT Exit Criteria (Gate Before Production)

Do **not** start the production deployment (Production Guide Phase 2) until all of these are true:

1. **SNMP path proven.** Zabbix polls simulators with SNMP-agent items; no HTTP-agent items in use on any monitored host.
2. **Templates final.** `template_ncr_snmp.xml` / `template_grg_snmp.xml` are the templates you will clone for real ATMs — item names, triggers, value maps all validated against real fault behavior you injected.
3. **Cross-VM links proven.** Report Portal + Filebeat → OpenSearch (UAT-02) and Gateway → PostgreSQL (UAT-01) all work over IPs, not container names.
4. **Gateway parser validated.** If the switch team provided sample ISO 8583 messages, the parser handles them in UAT before production. (Otherwise this becomes the first production-risk item — see Production Guide §10.4.)
5. **GLPI ticketing proven.** A Zabbix trigger auto-creates a GLPI ticket (media type + API token + url all correct).
6. **Reporting proven.** Report Portal generates all report types; recipients can view them.
7. **Backup/restore rehearsed.** `pg_dump` + restore into a scratch database works (this is your RTO rehearsal — Production Guide §14.5).
8. **Docs updated.** Any template/host/config change made during UAT is committed to the repo — production deploys from git, not from memory.

---

# 11. What Changes Between UAT and Production

| Thing | UAT | Production | Where it changes |
|---|---|---|---|
| VMs | 2 (UAT-01, UAT-02) | 3 (APPS-01, DATA-01, GWY-01) | Spec + Migration Guide §2 |
| Zabbix + apps | UAT-01 (172.26.208.176) | APPS-01 (172.26.18.74) | .env + compose |
| PostgreSQL + OpenSearch | Postgres on UAT-01, OS on UAT-02 | Both on DATA-01 (172.26.18.102) | compose files |
| ISO 8583 Gateway | UAT-02 (172.26.21.50) | GWY-01 (172.26.18.76) | compose files |
| SNMP target | UDP 1161+, `127.0.0.1` | UDP 161, real ATM IPs | Zabbix host interfaces |
| SNMP community | `dashen_sim` | real bank community (per ATM) | Zabbix host interfaces |
| SNMP OID tree | `.1.3.6.1.4.1.99999.8/9` (synthetic) | real NCR/GRG MIB | template clone + re-map (Production Guide §8.3) |
| EJ source | `ej-logs/` on UAT-01 | `/data/real-ej-logs/` on DATA-01 (share/SFTP) | filebeat.yml |
| Simulators | running | removed (Phase 9) | — |
| Firewall | internal only | internal only + ATM subnet UDP 161 | IT request |
| OpenSearch heap | 2 GB | 16 GB | compose env |

**The only collection-path changes at cutover: port, community, and OID tree.** Item types, triggers, dashboards, reports, GLPI — unchanged. If you find yourself changing those in production, you missed a UAT step.

---

# 12. Troubleshooting (UAT-Specific)

| Problem | Likely Cause | Fix |
|---|---|---|
| `snmpwalk` returns nothing on 1161 | Simulator container didn't start SNMP responder, or UDP port not published | `docker logs atm-sim-engine --tail 50` → look for `[SNMP]` lines; check `ports: 1161-1260/udp` in compose |
| Zabbix item "Not supported" | Host interface is HTTP/agent, or wrong port/OID | Verify SNMP interface on host, `{$ATM_PORT}`, community `dashen_sim`, template OID family matches vendor |
| Report Portal can't reach OpenSearch | Firewall or wrong `OS_HOST` | `curl http://172.26.21.50:9200` from UAT-01; check `OS_HOST: 172.26.21.50:9200` |
| Gateway crash-loops | `DB_HOST` wrong or PostgreSQL not accepting remote | `docker logs iso8583-gateway`; check UAT-01 postgres listens on 0.0.0.0 and `pg_hba.conf` has the 172.26.21.0/24 line |
| Filebeat ships nothing | `ej-logs/` permissions or filebeat.yml ownership | `sudo chown -R $USER:$USER ej-logs/`; `sudo chown root:root filebeat.yml`; `docker logs filebeat` |
| OpenSearch Dashboards "No data" | No indices yet | Wait for Filebeat; `curl -s http://172.26.21.50:9200/_cat/indices?v` |
| OpenSearch won't start | `vm.max_map_count` missing | `sudo sysctl -w vm.max_map_count=262144` (Step A.2) |
| GLPI 502 Bad Gateway | Install wizard not finished, or PHP worker timeout | Complete the wizard at `http://172.26.208.176:8082`; restart the GLPI container |

For anything else, use Production Guide §16 (Quick Troubleshooting Reference) — the stack is identical.

---

# 13. Summary of UAT Files

| Purpose | File |
|---|---|
| UAT-01 compose (all-in-one) | `deploy/uat/docker-compose-uat-vm1.yml` |
| UAT-02 compose (OpenSearch + gateway) | `deploy/uat/docker-compose-uat-vm2.yml` |
| UAT env vars | `deploy/uat/.env.uat` |
| UAT Grafana datasources | `config/grafana/datasources-uat.yml` |
| UAT PostgreSQL tuning | `config/postgres-uat/postgresql-custom.conf` |
| SNMP templates | `config/zabbix/template_ncr_snmp.xml`, `config/zabbix/template_grg_snmp.xml` |
| Simulator SNMP responder | `simulators/snmp_agent.py` |
| Filebeat config | `filebeat.yml` (output → UAT-02 in UAT; → localhost in production) |
| Full production reference | `docs/Production_Migration_Guide_v2.md` |
| 45-day plan / priorities | `docs/uat-pilot-checklist.md`, `docs/collection-strategy.md` |

---

*End of guide. Last updated: 2026-08-20.*
