# Dashen Bank — ATM Monitoring System
# Production Migration Guide v2

## From PoC (Single Laptop / 5 Simulated ATMs) to Production (5 VMs / 2,700 Real ATMs)

**Based on:** Server Infrastructure Specification (June 2026)
**Target:** Production — RHEL 9, 5 VMs, 2,300–2,700 ATMs, 3-Year Data Retention

---

# 1. Introduction

## 1.1 What This Guide Does

This guide takes the ATM Monitoring System — currently running on your laptop with 5 simulated ATMs (all in one Docker Compose file) — and moves it onto Dashen Bank's production infrastructure: **5 separate RHEL 9 virtual machines** serving **2,300–2,700 real ATMs**.

This is a **complete rewrite** of the original production migration guide because the infrastructure is fundamentally different:

| **Original Guide Assumed** | **Actual Production Spec** |
|---|---|
| 1 server (Ubuntu 22.04) | 5 VMs (RHEL 9) |
| 8 CPU / 32 GB RAM total | 60 vCPU / 184 GB RAM total |
| 500 GB disk total | 3.5 TB storage total |
| 20–30 ATMs | 2,300–2,700 ATMs |
| All services in one Docker Compose | Services split across 5 VMs |
| PostgreSQL in a container | Dedicated PostgreSQL VM (64 GB RAM, 1 TB) |
| 90-day retention | 3-year data retention |

## 1.2 Who This Guide Is For

You built the PoC on your laptop. You know Docker, Zabbix, Grafana, and the codebase. But you have **not** deployed a multi-VM distributed system before. Every step explains the "why" as well as the "how".

## 1.3 What Stays Exactly the Same

These do **not** change during migration:

- **Database schema** (`atm_transactions`, `atm_locations`, `atm_anomalies`, `atm_network_*` tables)
- **Grafana dashboards** (all 6 dashboards work as-is)
- **Report Portal** (all report routes: transactions, cash, errors, performance, availability, full report)
- **Anomaly detection rules** (velocity, failure spike, large withdrawal, rapid sequential, off-hours)
- **Network correlator logic** (transaction failure ↔ network degradation correlation)
- **Zabbix template structure** (item names, triggers, value maps — only the item *type* changes from HTTP to SNMP)
- **GLPI ticketing integration** (media type, action, template)

If you need to edit a Grafana dashboard query or Report Portal route to make it "work with real ATMs" — stop. That means your data is not arriving in the expected shape. Fix the data source, not the dashboard.

## 1.4 The Big Change: From One Compose File to Five

Your PoC has one `docker-compose.yml` with ~40 services all talking to each other by container name (`postgres`, `opensearch`, `zabbix-server`).

In production, services are spread across 5 VMs. They cannot use Docker's internal DNS. Instead, they use **IP addresses** across the bank's network.

Here is the connection diagram:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         DASHEN BANK INTERNAL NETWORK                          │
│                                                                              │
│  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐       │
│  │   VM1: ZABBIX    │    │   VM2: DB        │    │  VM3: OPENSEARCH STACK  │       │
│  │   16 vCPU, 32 GB │    │   16 vCPU, 64 GB │    │  12 vCPU, 48 GB  │       │
│  │   200 GB NVMe    │    │   1 TB NVMe      │    │   2 TB NVMe      │       │
│  │                  │    │                  │    │                  │       │
│  │  zabbix-server ──┼────┼─> VM2:5432      │    │  opensearch      │       │
│  │  zabbix-web   ───┼────┼─> VM2:5432      │    │  os-dashboards   │       │
│  │  Port: 8080,10051│    │  Port: 5432      │    │  filebeat        │       │
│  └────────┬─────────┘    └──────────────────┘    │  Ports: 9200,5601│       │
│           │                                      └────────┬─────────┘       │
│           │ VM1:8080 (Zabbix API)                          │                │
│           ▼                                                │ OS:9200        │
│  ┌──────────────────────────────────────────────────────────┘                │
│  │                                                                           │
│  │  ┌──────────────────┐    ┌──────────────────┐                             │
│  │  │   VM4: GRAFANA   │    │   VM5: GATEWAY   │                            │
│  │  │   8 vCPU, 24 GB  │    │   8 vCPU, 16 GB  │                             │
│  │  │   200 GB NVMe    │    │   100 GB NVMe    │                             │
│  │  │                  │    │                  │                             │
│  │  │  grafana ────────┼────┼─> VM1:8080 (API)│                             │
│  │  │  grafana ────────┼────┼─> VM2:5432 (DB) │  iso8583-gateway ───────────┼─> VM2:5432
│  │  │  report-portal ──┼────┼─> VM2:5432 (DB) │  anomaly-detector ─────────┼─> VM2:5432
│  │  │  report-portal ──┼────┼─> VM3:9200 (ES) │  network-correlator ───────┼─> VM2:5432
│  │  │  glpi            │    │                  │  network-correlator ───────┼─> VM1:8080
│  │  │  mariadb         │    │                  │                            │
│  │  └──────────────────┘    └──────────────────┘                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

Every arrow is a **firewall rule you must request from Dashen IT**. Section 3.2 lists them all.

---

# 2. Your Production Architecture

## 2.1 The 5 Production VMs

### VM1 — Zabbix Server + Web
- **Spec:** 16 vCPU, 32 GB RAM, 200 GB NVMe SSD
- **Runs:** Zabbix Server, Zabbix Web (UI), Zabbix Agent (for the VM itself)
- **Why:** Zabbix needs CPU to poll 2,700+ ATMs and evaluate triggers. 16 vCPU handles 30–60s polling intervals at scale.
- **Open ports:** 8080 (web), 10051 (trapper/proxy), 10050 (agent)

### VM2 — PostgreSQL (Monitoring DB + Transaction DB)
- **Spec:** 16 vCPU, 64 GB RAM, 1 TB NVMe SSD
- **Runs:** PostgreSQL 15 — **single instance**, no Docker container for the database itself
- **Why 64 GB / 1 TB:** This VM holds Zabbix history, trends, events, plus `atm_transactions` for 2,700 ATMs × 3 years ≈ billions of rows. PostgreSQL needs huge `shared_buffers` and `effective_cache_size` for analytical queries on transaction data.
- **Open port:** 5432 — **only** to VMs 1, 4, and 5

### VM3 — OpenSearch + OpenSearch Dashboards + Filebeat
- **Spec:** 12 vCPU, 48 GB RAM, 2 TB NVMe SSD
- **Runs:** OpenSearch, OpenSearch Dashboards, Filebeat
- **Why 2 TB:** EJ logs from 2,700 ATMs over 3 years ≈ 886M+ documents. 2 TB covers storage + indexing overhead + replicas.
- **Open ports:** 9200 (OS API — to VM4 only), 5601 (OpenSearch Dashboards — staff workstations)

### VM4 — Grafana + GLPI + Report Portal + Renderer
- **Spec:** 8 vCPU, 24 GB RAM, 200 GB NVMe SSD
- **Runs:** Grafana (with Zabbix plugin), Grafana Image Renderer, GLPI, MariaDB (for GLPI), Report Portal (Flask)
- **Why 8 vCPU:** PDF rendering with headless Chromium is CPU-intensive. Reports are generated on-demand.
- **Open ports:** 3000 (Grafana), 8082 (GLPI), 8888 (Report Portal)

### VM5 — ISO 8583 Gateway + Anomaly Detector + Network Correlator
- **Spec:** 8 vCPU, 16 GB RAM, 100 GB NVMe SSD
- **Runs:** ISO 8583 Gateway (TCP listener), Anomaly Detector, Network Correlator
- **Why relatively small:** These are lightweight Python processes running periodic SQL queries and a TCP listener.
- **Open port:** 9876 (ISO 8583 — to ATM switch network only)

## 2.2 The 2 UAT VMs

### UAT VM1 — Zabbix + PostgreSQL + Grafana + GLPI + Report Portal (all-in-one)
- **Spec:** 8 vCPU, 32 GB RAM, 500 GB NVMe SSD
- **Purpose:** A compact environment to test everything before touching production.

### UAT VM2 — OpenSearch + OpenSearch Dashboards + ISO 8583 Gateway
- **Spec:** 8 vCPU, 32 GB RAM, 500 GB NVMe SSD
- **Purpose:** EJ log testing and ISO 8583 message validation.

## 2.3 Why Not One Big Server?

Three reasons:

1. **I/O isolation** — PostgreSQL and OpenSearch are both I/O-hungry. Competing for the same disk would slow down transaction queries _and_ EJ searches simultaneously.
2. **Security zones** — The ISO 8583 gateway (VM5) must be reachable from the ATM switch network. That port should **not** be on the same VM as 3 years of transaction history. If VM5 is compromised, the attacker hits a gateway, not your database.
3. **Bank standard** — Dashen IT specified this architecture. The VM provisioning, firewall rules, and monitoring tooling are designed around this split.

---

# 3. What You Need Before Starting

## 3.1 Access Checklist

- [ ] **SSH access** to all 7 VMs — ask IT to create your user and add to `wheel` group
- [ ] **RHEL 9 subscription** or local repo access
- [ ] **Docker** installed (instructions in Phase 1 — same for all VMs)
- [ ] **Git** installed on all VMs
- [ ] **GitHub SSH key** or personal access token to clone the repo
- [ ] **One real ATM** for testing — ask the ATM hardware team for a test ATM with known IP and SNMP community string
- [ ] **Contact person** on the ATM Switch team (for ISO 8583)
- [ ] **Contact person** on the ATM Hardware/Vendor team (for SNMP OIDs and EJ paths)
- [ ] **Branch network spreadsheet** with ATM IDs, branch names, GPS coordinates, terminal IDs, vendor, model, install dates

## 3.2 Firewall Rules to Request

| From | To | Port | Purpose |
|---|---|---|---|
| VM1 (Zabbix) | VM2 (PostgreSQL) | TCP 5432 | Zabbix reads/writes monitoring data |
| VM4 (Grafana) | VM2 (PostgreSQL) | TCP 5432 | Grafana PostgreSQL datasource |
| VM4 (Report Portal) | VM2 (PostgreSQL) | TCP 5432 | Report Portal reads transactions |
| VM5 (Gateway) | VM2 (PostgreSQL) | TCP 5432 | ISO 8583 writes transactions |
| VM5 (Anomaly Detector) | VM2 (PostgreSQL) | TCP 5432 | Reads transactions for scanning |
| VM5 (Correlator) | VM2 (PostgreSQL) | TCP 5432 | Reads transactions for correlation |
| VM4 (Grafana) | VM1 (Zabbix) | TCP 8080 | Zabbix API calls from Grafana |
| VM5 (Correlator) | VM1 (Zabbix) | TCP 8080 | Zabbix API for event data |
| VM4 (Report Portal) | VM3 (OpenSearch) | TCP 9200 | EJ search queries |
| VM5 (Gateway) | ATM Switch | TCP 9876 | ISO 8583 messages |
| VM1 (Zabbix) | ATM Network | UDP 161 | SNMP polling |
| Staff workstations | VM1 | TCP 8080 | Zabbix web UI |
| Staff workstations | VM4 | TCP 3000, 8082, 8888 | Grafana, GLPI, Report Portal |
| Staff workstations | VM3 | TCP 5601 | OpenSearch Dashboards (optional) |

**All rules must be internal-only.** No port exposed to the public internet.

## 3.3 Information from the ATM Switch Team

Ask the Channel Support / ATM Switch team for these 5 things:

1. **Connection direction** — Does the switch connect to our listener (TCP server mode), or must we connect to the switch (client mode)?
2. **ISO 8583 variant** — Different switches (Base24, Postilion, etc.) have different bitmap/field definitions.
3. **Sample message captures** — Hex dumps or pcap files of 5–10 real messages to validate the parser.
4. **Switch IP and port** — Must be reachable from VM5.
5. **Test/UAT switch environment** — To validate before hitting production.

## 3.4 Information from the ATM Hardware Team

Open a ticket with the ATM vendor support team for:

1. **SNMP MIB and OIDs** — The enterprise OID and specific OIDs for: cassette levels, door sensors, card reader, printer, temperature, cameras, network, power. Also whether SNMP v3 is available (preferred over v2 for security).
2. **SNMP community string** — Must be bank-specific, not "public".
3. **EJ log file paths and format** — Exact path on the ATM OS (e.g., `C:\Program Files\NCR\APTRA\Journal\`), naming convention, log format (pipe-delimited, fixed-width, etc.).
4. **Existing EJ collection process** — Does the bank already collect EJ files centrally? If yes, we point Filebeat there instead of per-ATM installs.

---

# 4. Phase 0 — Create Per-VM Deploy Files

Before touching any server, you need to **split** the single PoC `docker-compose.yml` into separate files — one per production VM, plus combined files for UAT.

## 4.1 Service-to-VM Mapping

| Container | PoC Name | Prod VM | Key Change |
|---|---|---|---|
| Zabbix Server | `zabbix-server` | VM1 | DB_HOST → VM2 IP |
| Zabbix Web | `zabbix-web` | VM1 | DB_HOST → VM2 IP |
| Zabbix Agent | `zabbix-agent` | VM1 | Monitors the VM itself |
| PostgreSQL | `zabbix-db` | VM2 | Exposed on port 5432 |
| OpenSearch | `elasticsearch` | VM3 | 16 GB JVM heap |
| OpenSearch Dashboards | `kibana` | VM3 | Points to local ES |
| Filebeat | `filebeat` | VM3 | Watches local + real EJ log dirs |
| Grafana | `grafana` | VM4 | Datasources use VM IPs |
| Grafana Renderer | `grafana-renderer` | VM4 | For PDF generation |
| MariaDB (GLPI) | `glpi-db` | VM4 | Local to VM4 |
| GLPI | `glpi` | VM4 | Points to local MariaDB |
| Report Portal | `report-portal` | VM4 | DB_HOST → VM2, OS_HOST → VM3 |
| ISO 8583 Gateway | `iso8583-gateway` | VM5 | DB_HOST → VM2 |
| Anomaly Detector | `anomaly-detector` | VM5 | DB_HOST → VM2 |
| Network Correlator | `network-correlator` | VM5 | DB_HOST → VM2, ZABBIX_URL → VM1 |
| Simulators (15 containers) | `atm-sim*`, `txn-feed*`, `atm-ej*` | UAT only | Not in production |

## 4.2 The Critical Change: Container Names → IP Addresses

In the PoC, services reference each other by Docker container name:
```yaml
# PoC — works only when all containers share one Docker network
environment:
  DB_HOST: postgres
  OS_HOST: opensearch:9200
  ZABBIX_URL: http://zabbix-web:8080/api_jsonrpc.php
```

In production, each VM has a different IP. Replace container names with real IPs:
```yaml
# Production — uses real network IPs
environment:
  DB_HOST: 10.200.1.2          # VM2's IP
  OS_HOST: 10.200.1.3:9200     # VM3's IP
  ZABBIX_URL: http://10.200.1.1:8080/api_jsonrpc.php  # VM1's IP
```

Fill in your actual IPs here:

| VM | Placeholder IP | Your Actual IP |
|---|---|---|
| VM1 (Zabbix) | 10.200.1.1 | _____________ |
| VM2 (PostgreSQL) | 10.200.1.2 | _____________ |
| VM3 (OpenSearch) | 10.200.1.3 | _____________ |
| VM4 (Grafana/GLPI) | 10.200.1.4 | _____________ |
| VM5 (Gateway) | 10.200.1.5 | _____________ |

## 4.3 Step-by-Step: Create Per-VM Compose Files

### Step 4.3.1 — Create directories

On your laptop (where the GitHub repo lives):

```bash
mkdir -p deploy/production
mkdir -p deploy/uat
```

### Step 4.3.2 — VM1 compose file (Zabbix)

Create `deploy/production/docker-compose-vm1-zabbix.yml`:

```yaml
services:
  zabbix-server:
    image: zabbix/zabbix-server-pgsql:rhel-6.4-latest
    container_name: zabbix-server
    environment:
      DB_SERVER_HOST: "10.200.1.2"
      POSTGRES_DB: "zabbix"
      POSTGRES_USER: "zabbix"
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
    ports:
      - "10051:10051"
    restart: unless-stopped

  zabbix-web:
    image: zabbix/zabbix-web-nginx-pgsql:rhel-6.4-latest
    container_name: zabbix-web
    environment:
      DB_SERVER_HOST: "10.200.1.2"
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
      ZBX_HOSTNAME: "Zabbix-Server-VM1"
      ZBX_SERVER_HOST: "127.0.0.1"
    restart: unless-stopped
    depends_on:
      - zabbix-server
```

**Why `rhel-6.4-latest` images?** The PoC uses `ubuntu-6.4-latest`. RHEL-9-native images avoid compatibility issues with RHEL's kernel and library versions.

**Why `DB_SERVER_HOST: 10.200.1.2`?** This is the entire point of the distributed architecture — Zabbix connects to PostgreSQL on VM2 over the network, not to a local container.

### Step 4.3.3 — VM2 compose file (PostgreSQL)

Create `deploy/production/docker-compose-vm2-postgres.yml`:

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
      - ./config/postgres-production/postgresql-custom.conf:/etc/postgresql/postgresql.conf.d/custom.conf:ro
    ports:
      - "5432:5432"
    restart: unless-stopped

volumes:
  pgdata:
```

**You must tune PostgreSQL for 64 GB RAM.** Create the performance config:

```bash
mkdir -p config/postgres-production
```

Create `config/postgres-production/postgresql-custom.conf`:

```
# Dashen Bank ATM — PostgreSQL Performance Tuning
# Target: VM2 with 64 GB RAM, 16 vCPU, 1 TB NVMe SSD

shared_buffers = '16GB'
effective_cache_size = '48GB'
work_mem = '64MB'
maintenance_work_mem = '2GB'
wal_buffers = '64MB'
max_connections = '200'
checkpoint_completion_target = '0.9'
checkpoint_timeout = '15min'
max_wal_size = '16GB'
min_wal_size = '4GB'
random_page_cost = '1.1'
effective_io_concurrency = '200'
max_parallel_workers_per_gather = '4'
max_parallel_workers = '8'
autovacuum_max_workers = '6'
autovacuum_naptime = '30s'
autovacuum_vacuum_scale_factor = '0.01'
autovacuum_analyze_scale_factor = '0.005'
```

### Step 4.3.4 — VM3 compose file (OpenSearch + OpenSearch Dashboards + Filebeat)

**Note:** These instructions use **OpenSearch** (which the PoC already uses and the bank has standardized on). The configuration is identical to what runs in the PoC today, just tuned for production resources.

Create `deploy/production/docker-compose-vm3-opensearch.yml`:

```yaml
services:
  opensearch:
    image: opensearch:8.11.0
    container_name: opensearch
    environment:
      - discovery.type=single-node
      - OPENSEARCH_JAVA_OPTS=-Xms16g -Xmx16g
      - DISABLE_SECURITY_PLUGIN=true
      
      
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
    ports:
      - "5601:5601"
    depends_on:
      - opensearch
    restart: unless-stopped

  filebeat:
    image: docker.elastic.co/beats/filebeat-oss:7.12.1
    container_name: filebeat
    user: root
    volumes:
      - ./filebeat.yml:/usr/share/filebeat/filebeat.yml:ro
      - ./ej-logs:/var/log/atm-ej:ro
      - /data/real-ej-logs:/data/real-ej-logs:ro
    depends_on:
      - opensearch
    restart: unless-stopped

volumes:
  os-data:
```

**Why `OPENSEARCH_JAVA_OPTS=-Xms16g -Xmx16g`?** OpenSearch's JVM heap should not exceed 50% of available RAM (the rest is for the OS filesystem cache, which OpenSearch relies on heavily). 16 GB of 48 GB is the sweet spot.

### Step 4.3.5 — Update `filebeat.yml` for production

Replace the PoC's `filebeat.yml` with this version that watches both simulator logs (if any) and real ATM logs:

```yaml
filebeat.inputs:
  - type: log
    enabled: true
    paths:
      - /var/log/atm-ej/ATM-*.log
      - /data/real-ej-logs/*.log
    fields:
      log_type: atm_ej
    fields_under_root: true
    multiline.pattern: '^\d{4}-\d{2}-\d{2}'
    multiline.negate: true
    multiline.match: after

output.elasticsearch:
  hosts: ["localhost:9200"]
  index: "atm-ej-live-%{+yyyy.MM.dd}"
  pipeline: "atm_ej_parser"

setup.ilm.enabled: false
setup.template.name: "atm-ej-live"
setup.template.pattern: "atm-ej-live-*"

# Note: Filebeat's "output.elasticsearch" works with OpenSearch
# because OpenSearch is API-compatible with Elasticsearch 7.x
```

**`hosts: ["localhost:9200"]`** because both Filebeat and OpenSearch run on VM3.

### Step 4.3.6 — VM4 compose file (Grafana + GLPI + Report Portal + Renderer)

Create `deploy/production/docker-compose-vm4-dashboards.yml`:

```yaml
services:
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
      VERSION_GLPI: "10.0.15"
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
      GF_LOG_FILTERS: "rendering:debug"
    ports:
      - "3000:3000"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./index.html:/usr/share/grafana/public/views/index.html:ro
      - ./logo.png:/usr/share/grafana/public/img/dashen-logo.png:ro
      - ./entrypoint.sh:/entrypoint.sh:ro
      - ./config/grafana/datasources-production.yml:/etc/grafana/provisioning/datasources/datasources.yml:ro
      - ./config/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
    entrypoint: ["sh", "/entrypoint.sh"]
    depends_on: []
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
      DB_HOST: "10.200.1.2"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      GRAFANA_URL: "http://10.200.1.4:3000"
      REPORT_PORTAL_PORT: "8888"
      OS_HOST: "10.200.1.3:9200"
      OS_INDEX: "atm-ej-live-*,atm-electronic-journal"
    volumes:
      - ./report-portal:/app:rw
    ports:
      - "8888:8888"
    depends_on: []
    restart: unless-stopped

volumes:
  mariadb-data:
  glpi-root:
  grafana-data:
```

### Step 4.3.7 — Create production Grafana datasources

The PoC's `config/grafana/datasources.yml` uses container names. Create a production version at `config/grafana/datasources-production.yml`:

```yaml
apiVersion: 1

datasources:
  - name: Zabbix-ATM
    type: alexanderzobnin-zabbix-datasource
    access: proxy
    url: http://10.200.1.1:8080/api_jsonrpc.php
    jsonData:
      username: Admin
    secureJsonData:
      password: zabbix

  - name: ATM-Transactions
    type: postgres
    url: 10.200.1.2:5432
    database: zabbix
    user: zabbix
    secureJsonData:
      password: zabbix_pass
    jsonData:
      sslmode: disable
      postgresVersion: 1500

  - name: EJ-OpenSearch
    type: elasticsearch
    url: http://10.200.1.3:9200
    jsonData:
      index: atm-*
      timeField: "@timestamp"
      esVersion: "7.10.0"
      logMessageField: message
      logLevelField: status
```

### Step 4.3.8 — VM5 compose file (Gateway + Anomaly + Correlator)

Create `deploy/production/docker-compose-vm5-gateway.yml`:

```yaml
services:
  iso8583-gateway:
    build:
      context: ./camel
      dockerfile: Dockerfile.gateway
    container_name: iso8583-gateway
    environment:
      MODE: "simulation"
      DB_HOST: "10.200.1.2"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      INTERVAL: "10"
    ports:
      - "9876:9876"
    restart: unless-stopped

  anomaly-detector:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: anomaly-detector
    command: python3 /app/anomaly_detector.py
    environment:
      DB_HOST: "10.200.1.2"
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
      - /tmp:/tmp
    restart: unless-stopped

  network-correlator:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: network-correlator
    command: python3 /app/network_correlator.py
    environment:
      DB_HOST: "10.200.1.2"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      ZABBIX_URL: "http://10.200.1.1:8080/api_jsonrpc.php"
      ZABBIX_USER: "Admin"
      ZABBIX_PASS: "zabbix"
      CHECK_INTERVAL: "120"
      LATENCY_THRESHOLD: "200"
      LOSS_THRESHOLD: "10"
    volumes:
      - ./network_correlator.py:/app/network_correlator.py:ro
    restart: unless-stopped
```

### Step 4.3.9 — UAT VM1 compose file (all-in-one)

UAT VM1 runs everything except OpenSearch/OpenSearch Dashboards — similar to the PoC but with RHEL-based images.

Create `deploy/uat/docker-compose-uat-vm1.yml`. This is the most complex file because it includes all 5 simulators + all services.

The file should contain ALL services from the original `docker-compose.yml` (postgres, zabbix-server, zabbix-web, zabbix-agent, grafana, mariadb, glpi, report-portal, iso8583-gateway, anomaly-detector, network-correlator, all 5 atm-sim, all 5 txn-feed, all 5 atm-ej, pgadmin) — **same as your current PoC** — with these changes:

1. Use `rhel-6.4-latest` images instead of `ubuntu-6.4-latest` for Zabbix
2. Grafana port stays `3000:3000`
3. Report Portal's `OS_HOST` points to UAT VM2's IP

```bash
# Copy your existing docker-compose.yml as the base
cp docker-compose.yml deploy/uat/docker-compose-uat-vm1.yml

# Then edit it to change:
#   1. Zabbix images to rhel-6.4-latest
#   2. Grafana port from 3002:3000 to 3000:3000
#   3. Report Portal OS_HOST to UAT VM2 IP
```

### Step 4.3.10 — UAT VM2 compose file (OpenSearch + OpenSearch Dashboards)

Create `deploy/uat/docker-compose-uat-vm2.yml`:

```yaml
services:
  opensearch:
    image: opensearchproject/opensearch:2.14.0
    container_name: opensearch
    environment:
      - discovery.type=single-node
      - OPENSEARCH_JAVA_OPTS=-Xms16g -Xmx16g
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

  filebeat:
    image: docker.elastic.co/beats/filebeat-oss:7.12.1
    container_name: filebeat
    user: root
    volumes:
      - ./filebeat.yml:/usr/share/filebeat/filebeat.yml:ro
      - ./ej-logs:/var/log/atm-ej:ro
      - /data/real-ej-logs:/data/real-ej-logs:ro
    depends_on:
      - opensearch
    restart: unless-stopped

  opensearch-dashboards:
    image: opensearchproject/opensearch-dashboards:2.14.0
    container_name: opensearch-dashboards
    environment:
      - OPENSEARCH_HOSTS=http://opensearch:9200
    ports:
      - "5601:5601"
    depends_on:
      - opensearch
    restart: unless-stopped

  filebeat:
    image: docker.elastic.co/beats/filebeat-oss:7.12.1
    container_name: filebeat
    user: root
    volumes:
      - ./filebeat.yml:/usr/share/filebeat/filebeat.yml:ro
      - ./ej-logs:/var/log/atm-ej:ro
    depends_on:
      - opensearch
    restart: unless-stopped

volumes:
  os-data:
```

### Step 4.3.11 — Production .env file

Create `deploy/production/.env.production`:

```bash
# ============================
# Dashen Bank ATM Monitoring
# PRODUCTION Environment Variables
# ============================
# WARNING: Change ALL passwords before going live!
# chmod 600 this file — it contains secrets

POSTGRES_PASSWORD=<generate-strong-password>
GRAFANA_ADMIN_PASSWORD=<generate-strong-password>
MYSQL_ROOT_PASSWORD=<generate-strong-password>
MYSQL_PASSWORD=<generate-strong-password>
DB_PASS=<must-match-POSTGRES_PASSWORD>
GLPI_APP_TOKEN=<generated-during-glpi-setup>
GLPI_API_PASSWORD=<password-for-glpi-api>
GRAFANA_URL=http://localhost:3000
REPORT_PORTAL_PORT=8888
```

```bash
# Generate passwords:
openssl rand -base64 16   # Run this 4 times for 4 different passwords
```

Create `deploy/uat/.env.uat` with similar but different passwords.

---

# 5. Phase 1 — Deploy UAT (Practice First)

**Why UAT first?** The UAT environment is isolated. You can make mistakes, test connections, develop SNMP mappings, and validate the ISO 8583 parser — without affecting production data or alarming operations staff.

## 5.1 Deploy UAT VM2 (OpenSearch + OpenSearch Dashboards)

Start with VM2 because it is simpler and will be needed by the Report Portal on VM1.

### Step 5.1.1 — SSH into UAT VM2

```bash
ssh <your-username>@<uat-vm2-ip>
```

### Step 5.1.2 — Install Docker on RHEL 9

RHEL 9 **does not** come with Docker. You must add Docker's repository:

```bash
# Remove podman if installed (it conflicts with Docker)
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

# Log out and back in for the group change
exit
```

Then SSH back in and verify:

```bash
docker --version
docker compose version
```

### Step 5.1.3 — Clone the repository

```bash
# Install git if needed
sudo dnf install -y git

# Create project directory
cd /opt
sudo mkdir -p atm-monitoring
sudo chown $USER:$USER atm-monitoring
git clone <YOUR_GITHUB_URL> atm-monitoring
cd atm-monitoring
```

### Step 5.1.4 — Configure sysctl for OpenSearch

OpenSearch requires increased mmap limits:

```bash
# Set temporarily
sudo sysctl -w vm.max_map_count=262144

# Make permanent
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf

# Disable swap (ES hates swap)
sudo swapoff -a
```

### Step 5.1.5 — Create required directories and set up .env

```bash
cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env
# Set your passwords

mkdir -p ej-logs
```

### Step 5.1.6 — Start OpenSearch + OpenSearch Dashboards + Filebeat

```bash
docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d

echo "Waiting 60 seconds for OpenSearch to initialize..."
sleep 60

# Verify OpenSearch is running
curl -s http://localhost:9200
# Expected: JSON response with "cluster_name" and "version"

# Check indices (should be empty initially)
curl -s http://localhost:9200/_cat/indices?v
```

## 5.2 Deploy UAT VM1 (Everything Else)

### Step 5.2.1 — SSH into UAT VM1

```bash
ssh <your-username>@<uat-vm1-ip>
```

### Step 5.2.2 — Install Docker (same as step 5.1.2)

### Step 5.2.3 — Clone the repo

```bash
cd /opt
sudo mkdir -p atm-monitoring
sudo chown $USER:$USER atm-monitoring
git clone <YOUR_GITHUB_URL> atm-monitoring
cd atm-monitoring
```

### Step 5.2.4 — Set up environment

```bash
cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env

# Create required directories
mkdir -p ej-logs reports config/zabbix config/grafana/dashboards config/postgres
sudo mkdir -p /data/real-ej-logs
sudo chown $USER:$USER /data/real-ej-logs
```

### Step 5.2.5 — Fix Filebeat and EJ log permissions

```bash
# Fix EJ logs directory ownership
sudo chown -R $USER:$USER ej-logs/
chmod 755 ej-logs/

# Fix Filebeat config ownership
sudo chown root:root filebeat.yml
sudo chmod 644 filebeat.yml
```

### Step 5.2.6 — Start all services

```bash
# Build custom images
docker compose -f deploy/uat/docker-compose-uat-vm1.yml build --no-cache \
  atm-sim-001 report-portal iso8583-gateway

# Start PostgreSQL first (everything depends on it)
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d postgres

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL..."
for i in {1..30}; do
  if docker exec zabbix-db pg_isready -U zabbix -d zabbix &>/dev/null; then
    echo "PostgreSQL is ready!"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 3
done

# Create the custom database tables
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql

# Start everything else
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d

# Wait for services to initialize
sleep 60
```

### Step 5.2.7 — Verify all containers are running

```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v "Exited"
```

You should see approximately 25 containers all showing "Up":
- postgres, zabbix-server, zabbix-web, zabbix-agent
- mariadb, glpi
- grafana, grafana-renderer, report-portal
- iso8583-gateway, anomaly-detector, network-correlator
- 5× atm-sim, 5× txn-feed, 5× atm-ej
- pgadmin

### Step 5.2.8 — Verify PostgreSQL has data

```bash
# Check transaction count from simulators
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*) FROM atm_transactions;"

# Check ATM locations
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT atm_id, branch FROM atm_locations;"
```

## 5.3 Import Zabbix Configuration

Now load the monitoring templates and hosts into Zabbix.

### Step 5.3.1 — Import the template

1. Browse to `http://<uat-vm1-ip>:8080`
2. Log in: `Admin` / `zabbix`
3. **Configuration → Templates → Import** (top-right button)
4. Choose `config/zabbix/zbx_export_templates.xml`
5. Leave all checkboxes checked → **Import**
6. Confirm green "Imported successfully" message

### Step 5.3.2 — Import hosts

1. **Configuration → Hosts → Import**
2. Choose `config/zabbix/zbx_export_hosts.xml`
3. **Import**

### Step 5.3.3 — Import media types

1. **Administration → Media types → Import**
2. Choose `config/zabbix/zbx_export_mediatypes.xml`
3. **Import**

### Step 5.3.4 — Verify hosts

1. **Configuration → Hosts**
2. You should see ATM-001 through ATM-005
3. ATM-002 through ATM-005 should show green **ZBX** (they use HTTP agent against simulator containers)
4. ATM-001 may show red (needs Zabbix agent on host — skip for now)

## 5.4 Verify Grafana

### Step 5.4.1 — Check dashboards loaded

1. Browse to `http://<uat-vm1-ip>:3000`
2. Log in: `admin` / `<your-GRAFANA_ADMIN_PASSWORD>`
3. **Dashboards → Browse**
4. You should see 6 dashboards

If not, check provisioning:

```bash
docker logs grafana | grep -i provision
```

### Step 5.4.2 — Check data sources

1. **Configuration → Data Sources**
2. Should show: Zabbix-ATM, ATM-Transactions, EJ-OpenSearch
3. Test Zabbix-ATM: Click **Save & Test** → should succeed
4. Test ATM-Transactions: Click **Save & Test** → should succeed
5. Test EJ-OpenSearch: Will fail (UAT VM2 not connected to EJ logs yet) — skip

### Step 5.4.3 — View ATM Operations Centre dashboard

1. Open **Dashboards → ATM Operations Centre — Dashen Bank**
2. You should see:
   - Geo-map with 5 markers (Addis Ababa, Bole, Merkato, Hawassa, Dire Dawa)
   - KPI cards showing transaction counts, ATM status
   - Cassette level gauges
   - Temperature readings
   - Transaction volume time-series

## 5.5 Verify Report Portal

1. Browse to `http://<uat-vm1-ip>:8888`
2. Click **Generate Report** → select any report type
3. Choose "Last 7 days" → **Generate**
4. Verify PDF/Excel/CSV downloads with data

## 5.6 Verify GLPI

1. Browse to `http://<uat-vm1-ip>:8082`
2. Log in: `glpi` / `DashenGLPI2024`
3. Complete the installation wizard (one-time):
   - Accept license → Continue
   - Database should be pre-configured → Continue
   - Set admin password
4. **Setup → General → API** → Enable REST API
5. **Setup → API → Add API Client**:
   - Name: `Zabbix Integration`
   - Generate App Token → **Copy it**
6. In Zabbix: **Administration → Media types → GLPI Ticket**:
   - Update `app_token` with the copied token
   - Update `glpi_url` to `http://<uat-vm1-ip>:8082`
   - **Update**

## 5.7 Verify End-to-End on UAT

Before moving to production, confirm:

- [ ] All 25+ containers running
- [ ] Zabbix shows 5 hosts with data
- [ ] Grafana dashboards show live simulator data
- [ ] Report Portal generates PDF/Excel/CSV reports
- [ ] GLPI is accessible and API is enabled
- [ ] Transactions are being written to `atm_transactions`
- [ ] EJ logs are being written to `ej-logs/` directory

---

# 6. Phase 2 — Deploy Production VMs

Now that UAT is working, repeat the deployment on the 5 production VMs. **Deploy in this order:**

1. **VM2 (PostgreSQL)** — everything depends on the database
2. **VM1 (Zabbix)** — needs DB, then provides Zabbix API
3. **VM3 (OpenSearch)** — needs to be ready for Filebeat and Report Portal
4. **VM4 (Grafana + GLPI + Report Portal)** — needs DB, Zabbix API, ES
5. **VM5 (Gateway)** — needs DB and Zabbix API

## 6.1 Deploy VM2 — PostgreSQL

### Step 6.1.1 — SSH in and install Docker

```bash
ssh <your-username>@10.200.1.2

# Install Docker (same commands as step 5.1.2)
sudo dnf remove -y podman buildah
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
exit
# Reconnect
```

### Step 6.1.2 — Clone the repo

```bash
cd /opt
sudo mkdir -p atm-monitoring
sudo chown $USER:$USER atm-monitoring
git clone <YOUR_GITHUB_URL> atm-monitoring
cd atm-monitoring
```

### Step 6.1.3 — Set up environment and PostgreSQL tuning

```bash
cp deploy/production/.env.production .env
chmod 600 .env
nano .env
# Generate and set strong passwords

mkdir -p config/postgres-production

cat > config/postgres-production/postgresql-custom.conf << 'CONF'
shared_buffers = '16GB'
effective_cache_size = '48GB'
work_mem = '64MB'
maintenance_work_mem = '2GB'
wal_buffers = '64MB'
max_connections = '200'
checkpoint_completion_target = '0.9'
checkpoint_timeout = '15min'
max_wal_size = '16GB'
min_wal_size = '4GB'
random_page_cost = '1.1'
effective_io_concurrency = '200'
max_parallel_workers_per_gather = '4'
max_parallel_workers = '8'
autovacuum_max_workers = '6'
autovacuum_naptime = '30s'
autovacuum_vacuum_scale_factor = '0.01'
autovacuum_analyze_scale_factor = '0.005'
CONF
```

### Step 6.1.4 — Start PostgreSQL

```bash
docker compose -f deploy/production/docker-compose-vm2-postgres.yml up -d

# Wait for it
echo "Waiting for PostgreSQL..."
sleep 15

docker exec zabbix-db pg_isready -U zabbix -d zabbix
```

### Step 6.1.5 — Create custom tables

```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql

# Verify
docker exec zabbix-db psql -U zabbix -d zabbix -c "\dt"
# Should show: atm_locations, atm_transactions, atm_anomalies, atm_network_events, etc.
```

### Step 6.1.6 — Allow remote connections

By default, PostgreSQL inside the container only listens on localhost. Tell it to listen on all interfaces:

```bash
# Check if PostgreSQL is listening externally
ss -tlnp | grep 5432
# Should show: 0.0.0.0:5432

# If it only shows 127.0.0.1:5432, you need to update postgresql.conf:
docker exec zabbix-db bash -c "echo \"listen_addresses = '*'\" >> /var/lib/postgresql/data/postgresql.conf"
docker compose -f deploy/production/docker-compose-vm2-postgres.yml restart postgres

# Also check pg_hba.conf allows connections from your VM IPs
docker exec zabbix-db bash -c "echo 'host all all 10.200.1.0/24 md5' >> /var/lib/postgresql/data/pg_hba.conf"
docker compose -f deploy/production/docker-compose-vm2-postgres.yml restart postgres
```

### Step 6.1.7 — Test remote connection (from your laptop)

```bash
# Install psql on your laptop if needed
# Then:
psql -h 10.200.1.2 -U zabbix -d zabbix -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
```

## 6.2 Deploy VM1 — Zabbix Server + Web

### Step 6.2.1 — SSH in, install Docker, clone repo

(Repeat steps 6.1.1–6.1.2)

### Step 6.2.2 — Set up .env

```bash
cp deploy/production/.env.production .env
chmod 600 .env
nano .env
# IMPORTANT: POSTGRES_PASSWORD must match what you set on VM2!
```

### Step 6.2.3 — Start Zabbix

```bash
docker compose -f deploy/production/docker-compose-vm1-zabbix.yml up -d

# Wait 30 seconds for Zabbix to initialize its database schema
sleep 30

# Check logs
docker logs zabbix-server --tail 20
```

Look for:
```
connecting to database 'zabbix' on '10.200.1.2' port '5432'
database connection established
```

If you see "Cannot connect to database":
- Double-check the password in `.env`
- Verify firewall between VM1 and VM2 on port 5432
- Check `pg_hba.conf` on VM2 allows VM1's IP

### Step 6.2.4 — Verify Zabbix web UI

```bash
# From your browser:
# http://10.200.1.1:8080
# Log in: Admin / zabbix
```

## 6.3 Deploy VM3 — OpenSearch + OpenSearch Dashboards + Filebeat

### Step 6.3.1 — SSH in, install Docker, clone repo

### Step 6.3.2 — Configure sysctl and start

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
sudo swapoff -a

cp deploy/production/.env.production .env
chmod 600 .env

mkdir -p ej-logs
sudo mkdir -p /data/real-ej-logs
sudo chown $USER:$USER /data/real-ej-logs

# Fix filebeat permissions
sudo chown root:root filebeat.yml
sudo chmod 644 filebeat.yml

docker compose -f deploy/production/docker-compose-vm3-opensearch.yml up -d

echo "Waiting 60 seconds for OpenSearch..."
sleep 60

curl -s http://localhost:9200
```

## 6.4 Deploy VM4 — Grafana + GLPI + Report Portal + Renderer

### Step 6.4.1 — SSH in, install Docker, clone repo

### Step 6.4.2 — Set up and start

```bash
cp deploy/production/.env.production .env
chmod 600 .env
nano .env
# POSTGRES_PASSWORD must match VM2
# GRAFANA_ADMIN_PASSWORD, MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD must be set

# Copy production datasources into place
cp config/grafana/datasources-production.yml config/grafana/datasources.yml

docker compose -f deploy/production/docker-compose-vm4-dashboards.yml up -d

sleep 30
docker ps --format "table {{.Names}}\t{{.Status}}"
```

### Step 6.4.3 — Verify

```bash
# Check Grafana
curl -s http://localhost:3000/api/health

# Check Report Portal
curl -s http://localhost:8888

# Check GLPI
curl -s -o /dev/null -w "%{http_code}" http://localhost:8082
# Should return 200
```

## 6.5 Deploy VM5 — Gateway + Anomaly + Correlator

### Step 6.5.1 — SSH in, install Docker, clone repo

### Step 6.5.2 — Start

```bash
cp deploy/production/.env.production .env
chmod 600 .env
# POSTGRES_PASSWORD must match VM2

docker compose -f deploy/production/docker-compose-vm5-gateway.yml up -d

sleep 15
docker ps --format "table {{.Names}}\t{{.Status}}"
```

### Step 6.5.3 — Verify gateway is writing transactions

```bash
# Check gateway logs
docker logs iso8583-gateway --tail 5
# Expected: "MODE: SIMULATION" and "generating 1 transaction every 10 seconds"

# Wait 30 seconds, then check DB from VM2:
# On VM2:
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*), source FROM atm_transactions GROUP BY source;"
# Should show ~3 transactions with source = 'ISO8583_SIM'
```

## 6.6 Production Deployment Verification

Before proceeding, confirm:

- [ ] All 5 production VMs have Docker installed and running
- [ ] VM2: PostgreSQL running, accessible from other VMs, custom tables exist
- [ ] VM1: Zabbix web UI accessible at `http://10.200.1.1:8080`, connected to VM2's DB
- [ ] VM3: OpenSearch responding at `http://10.200.1.3:9200`
- [ ] VM4: Grafana at `http://10.200.1.4:3000`, datasources connecting to VM1 and VM2
- [ ] VM4: Report Portal at `http://10.200.1.4:8888`, GLPI at `http://10.200.1.4:8082`
- [ ] VM5: ISO 8583 Gateway running and writing transactions to VM2's DB
- [ ] All containers set to `restart: unless-stopped`

---

# 7. Phase 3 — Import Zabbix Template and Hosts (Production)

Repeat the import steps from UAT (section 5.3) on the production Zabbix (VM1):

1. **Configuration → Templates → Import** → `config/zabbix/zbx_export_templates.xml`
2. **Configuration → Hosts → Import** → `config/zabbix/zbx_export_hosts.xml`
3. **Administration → Media types → Import** → `config/zabbix/zbx_export_mediatypes.xml`
4. Update GLPI media type:
   - `glpi_url`: `http://10.200.1.4:8082`
   - `app_token`: from GLPI API client setup on VM4

---

# 8. Phase 4 — Connect First Real ATM (SNMP Hardware Monitoring)

This is the most technically important phase. You will take one real ATM and make Zabbix display its real hardware status in the same Grafana dashboards.

## 8.1 Understanding the Change

In the PoC, Zabbix items use **HTTP agent** type, polling URLs like:
```
http://172.17.0.1:1161/oid/1.1.0
```

For real ATMs, items must use **SNMP agent** type, polling UDP port 161:
```
SNMP GET 10.10.1.50:161 .1.3.6.1.4.1.37513.1.1.0
```

The item **names**, **triggers**, **value maps**, and **Grafana dashboards** do not change. Only the item type and target address change.

## 8.2 SNMP Walk the Test ATM

### Step 8.2.1 — Install SNMP tools on VM1

```bash
sudo dnf install -y net-snmp-utils
```

### Step 8.2.2 — Walk the ATM's MIB

Ask the ATM hardware team for a test ATM IP and community string. Replace `10.10.1.50` and `dashen_atm_2024` with your actual values:

```bash
# First, test basic SNMP connectivity
snmpwalk -v2c -c dashen_atm_2024 10.10.1.50 .1.3.6.1.2.1.1

# Then walk the vendor-specific OID (common enterprise OIDs):
# NCR:          .1.3.6.1.4.1.37513
# Diebold:      .1.3.6.1.4.1.55  or  .1.3.6.1.4.1.6359
# Wincor:       .1.3.6.1.4.1.425
# Fujitsu:      .1.3.6.1.4.1.211
# Try the most likely one:
snmpwalk -v2c -c dashen_atm_2024 10.10.1.50 .1.3.6.1.4.1.37513
```

**Save the output — it is your map:**

```bash
snmpwalk -v2c -c dashen_atm_2024 10.10.1.50 .1.3.6.1.4.1.37513 > /tmp/atm_snmpwalk_output.txt
```

### Step 8.2.3 — Interpret the output

Each line of the snmpwalk output looks like:
```
.1.3.6.1.4.1.37513.2.1.1.0 = INTEGER: 1842
```

This means: OID `.1.3.6.1.4.1.37513.2.1.1.0` has a current value of `1842`. The description of the OID (if the MIB is loaded) might be something like "Cash Cassette 1 Note Count".

Map each OID to the corresponding simulator OID. Build a table:

```
| Simulator OID | Simulator Item Name              | Real OID (example — YOURS WILL DIFFER)  |
|---|---|---|
| 1.1.0         | ATM Operational Status            | .1.3.6.1.4.1.37513.1.1.0               |
| 1.2.0         | Cassette 1 Notes Remaining        | .1.3.6.1.4.1.37513.2.1.1.0             |
| 1.3.0         | Cassette 2 Notes Remaining        | .1.3.6.1.4.1.37513.2.1.2.0             |
| 2.1.0         | Card Reader Status                | .1.3.6.1.4.1.37513.3.1.1.0             |
| 4.1.0         | Safe Door Status                  | .1.3.6.1.4.1.37513.5.1.1.0             |
| 4.3.0         | Temperature                       | .1.3.6.1.4.1.37513.5.3.0               |
```

**Your real OIDs will be different.** This table is just an example for NCR ATMs.

## 8.3 Create the SNMP Template

You must **clone** the existing HTTP template rather than modifying it. The HTTP template stays for simulators in UAT.

### Step 8.3.1 — Clone the template

In Zabbix web UI:
1. **Configuration → Templates**
2. Click "Dashen Bank ATM Hardware" (the name)
3. Click **Full clone** (top of page)
4. Name: `Dashen Bank ATM Hardware - SNMP (Real)`
5. Click **Add**

### Step 8.3.2 — Change items from HTTP to SNMP

For each item in the cloned template:
1. Click the item name
2. Change **Type** from "HTTP agent" to "SNMP agent"
3. Enter the **SNMP OID** from your mapping table
4. Set **SNMP community** to `{$SNMP_COMMUNITY}` (macro)
5. Set **Port** to `161`
6. **Remove** the URL field
7. Leave everything else unchanged
8. Click **Update**

There are ~30 items. This takes 30–45 minutes.

### Step 8.3.3 — Add template macros

Go to the cloned template → **Macros** → Add:
- `{$SNMP_COMMUNITY}` = `public` (default — override per host)
- `{$SNMP_PORT}` = `161`

Click **Update**.

## 8.4 Create the First Real ATM Host

### Step 8.4.1 — Create the host

1. **Configuration → Hosts → Create host**
2. **Host name:** `ATM-006 | NCR | Adama Main Branch` (convention: `<ID> | <VENDOR> | <BRANCH>`)
3. **Host groups:** Select "Dashen Bank ATMs"
4. **Interfaces → Add → SNMP:**
   - IP: `10.10.1.50` (your real ATM's IP)
   - Port: `161`
   - SNMP version: `SNMPv2`
   - Community: `{$SNMP_COMMUNITY}`
5. **Templates:** Link `Dashen Bank ATM Hardware - SNMP (Real)`
6. Click **Add**

### Step 8.4.2 — Override the community string

1. Click the new host's name
2. **Macros** tab
3. Add: `{$SNMP_COMMUNITY}` = `dashen_atm_2024` (your actual community string)
4. **Update**

### Step 8.4.3 — Wait and verify

After 1–2 minutes:
1. **Monitoring → Latest data**
2. Filter by the new host
3. You should see values populating for all configured items

## 8.5 SNMP Troubleshooting

| Problem | Likely Cause | Fix |
|---|---|---|
| "Timeout" | Network/firewall blocking UDP 161 | Ping ATM from VM1. Check firewall rules. |
| "Cannot connect" | Wrong IP or port | Verify IP and port 161 |
| "Unknown SNMP error" | Wrong community string | Run `snmpwalk` from VM1 to confirm |
| Item shows wrong value | Wrong OID in item config | Re-check snmpwalk output and mapping table |
| Some items work, some don't | Not all OIDs supported by this ATM model | Normal. Disable unsupported items. |

## 8.6 Security: SNMP v3

SNMP v2 community strings are sent in **plain text**. For a banking environment, ask the ATM team if SNMP v3 (with authentication and encryption) is available.

If yes, change the host interface from SNMPv2 to SNMPv3:
- **Security name:** username for SNMP v3
- **Security level:** authPriv (recommended — both auth and encryption)
- **Auth protocol:** SHA
- **Auth passphrase:** (set by ATM team)
- **Privacy protocol:** AES
- **Privacy passphrase:** (set by ATM team)

The item-level configuration (OIDs, types) does not change — only the interface authentication settings.

---

# 9. Phase 5 — Connect Electronic Journal (EJ) Logs

The PoC's EJ generators write fake logs. For real ATMs, EJ files exist on the ATM's Windows OS and must reach VM3's Filebeat.

## 9.1 Find the EJ File Location

Ask the ATM hardware/vendor team:

1. **Exact path on ATM:** e.g., `C:\Program Files\NCR\APTRA\Journal\`
2. **File naming:** One file per day? `EJ_YYYYMMDD.log`?
3. **Format:** Pipe-delimited? Fixed-width? Custom binary?
4. **Existing collection:** Does the bank already pull EJ files to a central server?

## 9.2 Choose a Delivery Method

| Method | How It Works | Best When |
|---|---|---|
| **Filebeat on ATM** | Install lightweight Filebeat for Windows on each ATM | Vendor allows software install |
| **Network share** | ATM writes to a Windows share; VM3's Filebeat mounts it | No agent on ATM needed |
| **SFTP push** | ATM/collection process SFTPs files to VM3 | Bank already has EJ collection |
| **Central collector** | Point Filebeat at bank's existing EJ server | Simplest if it exists |

### Option A: Filebeat on ATM (Long-term best)

On each ATM's Windows OS:

1. Download Filebeat for Windows
2. Create `C:\Program Files\Filebeat\filebeat.yml`:

```yaml
filebeat.inputs:
  - type: log
    enabled: true
    paths:
      - C:\Program Files\NCR\APTRA\Journal\*.log
    fields:
      log_type: atm_ej
    fields_under_root: true

output.elasticsearch:
  hosts: ["10.200.1.3:9200"]
  index: "atm-ej-live-%{+yyyy.MM.dd}"
```

3. Install as Windows service:
```
powershell -ExecutionPolicy Unrestricted -File .\install-service-filebeat.ps1
```

### Option B: Network Share (No agent needed)

On VM3:
```bash
sudo mkdir -p /mnt/atm-ej-share
sudo mount -t cifs //<file-server>/atm-ej-logs /mnt/atm-ej-share \
  -o username=<user>,password=<pass>,domain=DASHEN
```

Then add to Filebeat volume in `docker-compose-vm3-opensearch.yml`:
```yaml
volumes:
  - /mnt/atm-ej-share:/data/real-ej-logs:ro
```

### Option C: SFTP Push

On VM3:
```bash
sudo useradd -m -s /sbin/nologin atm-ej-sftp
sudo mkdir -p /home/atm-ej-sftp/ej-logs
sudo chown atm-ej-sftp:atm-ej-sftp /home/atm-ej-sftp/ej-logs
```

Configure SFTP-only access in `/etc/ssh/sshd_config.d/atm-ej-sftp.conf`:
```
Match User atm-ej-sftp
    PasswordAuthentication yes
    ChrootDirectory /home/atm-ej-sftp
    ForceCommand internal-sftp
```

## 9.3 PCI DSS / Card Number Masking

**CRITICAL:** Before real EJ data enters OpenSearch, verify with Dashen's compliance team:

- **Do real EJ logs contain unmasked PANs?** The simulators generate already-masked cards (`************1234`). Real ATM logs may contain full card numbers.
- **If yes, must mask them.** Options:
  - Configure ATM to mask PANs in EJ output (best)
  - Add OpenSearch ingest pipeline
  - Add Filebeat processor

Example ingest pipeline for PAN masking:
```bash
curl -X PUT "localhost:9200/_ingest/pipeline/atm_ej_parser" -H 'Content-Type: application/json' -d '{
  "description": "Mask PANs in ATM EJ logs",
  "processors": [
    {
      "grok": {
        "field": "message",
        "patterns": ["%{TIMESTAMP_ISO8601:timestamp} \\\\| %{DATA:atm_id} .* CARD=%{DATA:card_raw}"]
      }
    },
    {
      "script": {
        "source": "if (ctx.card_raw != null) { ctx.card_masked = ctx.card_raw.substring(0,4) + \"************\" + ctx.card_raw.substring(ctx.card_raw.length()-4); }"
      }
    },
    { "remove": { "field": "card_raw" } }
  ]
}'
```

## 9.4 Verify EJ Data

```bash
# Check indices
curl -s http://10.200.1.3:9200/_cat/indices?v

# Check document count
curl -s "http://10.200.1.3:9200/atm-ej-live-*/_count"

# Browse OpenSearch Dashboards
# http://10.200.1.3:5601
# Create index pattern: atm-ej-live-*
# Time field: @timestamp
```

---

# 10. Phase 6 — Connect ATM Switch (ISO 8583)

## 10.1 Get Switch Information

From the ATM Switch team:

1. **Connection direction:** Does the switch connect to us (server mode) or must we connect to it (client mode)?
2. **ISO 8583 variant:** Which fields, bitmaps, length headers?
3. **Sample messages:** Hex dumps or pcap to validate the parser
4. **Switch IP and port**

## 10.2 Switch to TCP Mode

On VM5, update the gateway environment:

```yaml
  iso8583-gateway:
    environment:
      MODE: "tcp"
      SWITCH_HOST: "0.0.0.0"
      SWITCH_PORT: "9876"
      DB_HOST: "10.200.1.2"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
```

Then rebuild and restart:
```bash
docker compose -f deploy/production/docker-compose-vm5-gateway.yml build iso8583-gateway
docker compose -f deploy/production/docker-compose-vm5-gateway.yml up -d iso8583-gateway
```

Verify:
```bash
docker logs iso8583-gateway --tail 5
# Expected: "ISO 8583 TCP server listening on 0.0.0.0:9876"
ss -tlnp | grep 9876
# Expected: LISTEN
```

## 10.3 Client Mode (If Needed)

Some switches require our gateway to connect to them (client mode). If the switch team confirms this:

The gateway's `start_tcp_server()` function (server mode) must be swapped for a client connection function. Add this to `camel/iso8583_gateway.py`:

```python
def connect_to_switch(db_conn):
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            print(f"Connecting to switch at {SWITCH_HOST}:{SWITCH_PORT}...")
            sock.connect((SWITCH_HOST, SWITCH_PORT))
            print(f"Connected!")
            while True:
                length_bytes = sock.recv(2)
                if not length_bytes:
                    break
                msg_len = struct.unpack('>H', length_bytes)[0]
                raw_msg = b''
                while len(raw_msg) < msg_len:
                    chunk = sock.recv(msg_len - len(raw_msg))
                    if not chunk:
                        break
                    raw_msg += chunk
                txn = parse_iso8583_message(raw_msg)
                if txn:
                    store_transaction(db_conn, txn)
        except Exception as e:
            print(f"Connection error: {e}")
        finally:
            try:
                sock.close()
            except:
                pass
        time.sleep(10)
```

Then update the main section:
```python
if MODE == 'tcp':
    if SWITCH_HOST and SWITCH_HOST != '0.0.0.0':
        connect_to_switch(conn)
    else:
        start_tcp_server(conn)
```

And use these environment variables for client mode:
```yaml
environment:
  MODE: "tcp"
  SWITCH_HOST: "10.200.100.50"   # Switch's IP
  SWITCH_PORT: "15000"            # Switch's port
```

## 10.4 Validate the Parser

Create a test script with real switch messages:

```bash
cat > /tmp/test_parser.py << 'EOF'
import sys
sys.path.insert(0, '/opt/atm-monitoring/camel')
from iso8583_gateway import parse_iso8583_message

# Paste real hex dump here
hex_message = "006F020038200000..."  # REPLACE with actual switch message
raw_bytes = bytes.fromhex(hex_message.replace(' ', ''))

result = parse_iso8583_message(raw_bytes)
if result:
    print("Parsed OK:")
    for k, v in result.items():
        print(f"  {k}: {v}")
else:
    print("Parse FAILED")
EOF

docker cp /tmp/test_parser.py iso8583-gateway:/app/
docker exec iso8583-gateway python3 /app/test_parser.py
```

If parsing fails, adjust `parse_iso8583_message()` for:
- Different message length (2 bytes → 4 bytes)
- ASCII hex bitmap instead of binary
- Additional fields (48, 49, etc.)
- EBCDIC instead of ASCII encoding

## 10.5 Verify Real Transactions

```bash
# Check source counts
psql -h 10.200.1.2 -U zabbix -d zabbix -c \
  "SELECT source, COUNT(*), MAX(recorded_at) FROM atm_transactions GROUP BY source;"
```

You should see `ISO8583_REAL` appearing alongside `ISO8583_SIM`.

---

# 11. Phase 7 — Populate ATM Location Data

The Grafana geo-map reads from the `atm_locations` table. Populate it with real ATM data.

## 11.1 Gather the Spreadsheet

Request from the branch network team:

| Column | Example |
|---|---|
| atm_id | ATM-006 |
| branch | Adama Main Branch |
| district | Adama |
| city | Adama |
| region | Oromia |
| latitude | 8.5400 |
| longitude | 39.2700 |
| terminal_id | TID0006 |
| vendor | NCR |
| model | SelfServ 84 |
| install_date | 2024-01-10 |

## 11.2 Manual Entry (First Few ATMs)

```bash
psql -h 10.200.1.2 -U zabbix -d zabbix
```

```sql
INSERT INTO atm_locations VALUES
('ATM-006', 'Adama Main Branch', 'Adama', 'Adama', 'Oromia',
 8.5400, 39.2700, 'TID0006', 'NCR', 'SelfServ 84',
 '2024-01-10', 'active')
ON CONFLICT (atm_id) DO NOTHING;
```

Or use the web form: `http://10.200.1.4:8888/admin/atm`

## 11.3 Bulk Import (Full Fleet)

Save the branch network team's spreadsheet as CSV matching the `atm_locations` column order:

```csv
atm_id,branch,district,city,region,latitude,longitude,terminal_id,vendor,model,install_date,status
ATM-006,Adama Main Branch,Adama,Adama,Oromia,8.5400,39.2700,TID0006,NCR,SelfServ 84,2024-01-10,active
ATM-007,Bahir Dar Branch,Bahir Dar,Bahir Dar,Amhara,11.5850,37.3900,TID0007,NCR,SelfServ 84,2024-02-15,active
...
```

Then import:

```bash
# Copy CSV to VM2
scp atm_locations_bulk.csv <user>@10.200.1.2:/tmp/

# On VM2:
docker cp /tmp/atm_locations_bulk.csv zabbix-db:/tmp/
docker exec zabbix-db psql -U zabbix -d zabbix -c \
  "\copy atm_locations FROM '/tmp/atm_locations_bulk.csv' WITH CSV HEADER"

# Verify
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*) FROM atm_locations;"
```

---

# 12. Phase 8 — Scale to Full Fleet (Auto-Discovery)

Manually creating hosts for 2,700 ATMs is not feasible. Use Zabbix auto-discovery.

## 12.1 Create the Discovery Rule

1. **Configuration → Discovery → Create discovery rule**
2. **Name:** `Dashen ATM Network Scan`
3. **IP range:** The ATM network subnet (ask IT), e.g., `10.10.1.1-254`
4. **Checks → Add → Type:** SNMPv2 agent
   - **Port:** 161
   - **OID:** The operational status OID from your snmpwalk (e.g., `.1.3.6.1.4.1.37513.1.1.0`)
   - **SNMP community:** `{$SNMP_COMMUNITY}`
5. **Device uniqueness:** IP address
6. **Update interval:** 1h
7. **Enable**

## 12.2 Create the Discovery Action

1. **Configuration → Actions → Discovery actions → Create action**
2. **Name:** `Auto-register Dashen ATM`
3. **Conditions:** "Service type equals SNMPv2 agent" AND "Discovery rule equals Dashen ATM Network Scan"
4. **Operations:**
   - Add host
   - Add to host group "Dashen Bank ATMs"
   - Link template "Dashen Bank ATM Hardware - SNMP (Real)"
5. **Enable**

## 12.3 What Auto-Discovery Does Not Do

Auto-discovery handles Zabbix host creation only. For each newly discovered ATM, you still need:
- **Location data** → Enter via admin form at `http://10.200.1.4:8888/admin/atm`
- **EJ log shipping** → Configure per ATM (or use the centralized method from Phase 5)
- **ISO 8583 transactions** → Automatic (the gateway is switch-wide, not per-ATM)

---

# 13. Phase 9 — Decommission Simulators

Only once real ATMs are confirmed working. No rush — simulators do not interfere with real data.

## 13.1 Stop Simulator Containers (UAT/Production)

```bash
docker compose -f deploy/uat/docker-compose-uat-vm1.yml stop \
  atm-sim-001 atm-sim-002 atm-sim-003 atm-sim-004 atm-sim-005 \
  atm-ej-001 atm-ej-002 atm-ej-003 atm-ej-004 atm-ej-005 \
  txn-feed-001 txn-feed-002 txn-feed-003 txn-feed-004 txn-feed-005
```

## 13.2 Disable Simulator Hosts in Zabbix

1. **Configuration → Hosts**
2. Select ATM-001 through ATM-005
3. Click **Disable** (not delete — keeps historical data)

## 13.3 Clean Up

- Keep the HTTP template for historical reference
- Remove simulator code from production only after 1 full quarter of stable real-ATM operation
- The SNMP template (`Dashen Bank ATM Hardware - SNMP (Real)`) becomes the primary template

---

# 14. Phase 10 — Backup and Disaster Recovery

## 14.1 What Needs Backing Up

| Data | Method | Frequency | Location |
|---|---|---|---|
| PostgreSQL (VM2) | pg_dump | Daily | Off-server |
| OpenSearch (VM3) | Snapshot API | Weekly | Off-server |
| Zabbix config (VM1) | XML export | Per change | Git repo |
| Grafana dashboards | Git (already in repo) | Per change | Git repo |
| GLPI database (VM4) | mysqldump | Daily | Off-server |

## 14.2 PostgreSQL Backup (VM2)

Create `/opt/atm-monitoring/scripts/backup-production.sh`:

```bash
#!/bin/bash
BACKUP_DIR="/backups/postgres"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# Full database dump (compressed)
docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl | \
  gzip > "$BACKUP_DIR/zabbix_full_$DATE.sql.gz"

# Custom tables only (for quick restore)
docker exec zabbix-db pg_dump -U zabbix zabbix --no-owner --no-acl \
  -t atm_locations -t atm_transactions -t atm_anomalies \
  -t atm_network_events -t atm_network_correlation -t atm_network_metrics | \
  gzip > "$BACKUP_DIR/atm_custom_tables_$DATE.sql.gz"

# Keep only last 14 full backups
ls -t "$BACKUP_DIR"/zabbix_full_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm

echo "Backup complete: $BACKUP_DIR"
```

Schedule via crontab on VM2:
```bash
crontab -e
# Add:
0 2 * * * bash /opt/atm-monitoring/scripts/backup-production.sh >> /var/log/atm-backup.log 2>&1
```

**Off-server storage:** Coordinate with IT to mount an NFS/CIFS share to `/backups/`. A backup on the same server is not a backup.

## 14.3 OpenSearch Snapshots (VM3)

Register a snapshot repository (one-time):
```bash
curl -X PUT "localhost:9200/_snapshot/atm_ej_backup" -H 'Content-Type: application/json' -d '{
  "type": "fs",
  "settings": { "location": "/mnt/os-backups" }
}'
```

The `/mnt/os-backups` path must point to off-server storage and be mounted into the OpenSearch container.

Take a snapshot:
```bash
curl -X PUT "localhost:9200/_snapshot/atm_ej_backup/snapshot_$(date +%Y%m%d)?wait_for_completion=false"
```

## 14.4 GLPI Backup (VM4)

```bash
docker exec glpi-db mysqldump -u root -p"${MYSQL_ROOT_PASSWORD}" glpi | gzip > /backups/glpi-backup-$(date +%Y%m%d).sql.gz
```

## 14.5 Restore Testing

A backup that has never been restored is not verified. At least once before going live:

1. Provision a test VM
2. Install Docker + clone repo
3. Restore PostgreSQL: `gunzip -c backup.sql.gz | docker exec -i zabbix-db psql -U zabbix zabbix`
4. Verify data: `SELECT COUNT(*) FROM atm_transactions;`
5. Document recovery time (this is your RTO)

---

# 15. Production Readiness Checklist

## Infrastructure
- [ ] Server provisioned with agreed specifications (5 VMs)
- [ ] All Docker containers running and set to `restart: unless-stopped`
- [ ] Firewall rules allow only necessary internal traffic
- [ ] No ports exposed to public internet

## Zabbix (VM1)
- [ ] Real ATM(s) report hardware status via SNMP
- [ ] Triggers fire for: cash low/empty, door open, printer fault, network down
- [ ] Auto-discovery configured and tested
- [ ] GLPI tickets created automatically on trigger firing

## Grafana (VM4)
- [ ] Real ATMs appear on geo-map with correct locations
- [ ] ATM Fleet Overview table shows real data
- [ ] Drill-down dashboards work for real ATMs

## Transactions (VM5 → VM2)
- [ ] ISO 8583 Gateway receives real switch messages
- [ ] Transaction amounts, statuses match switch's records
- [ ] Source tagging (`ISO8583_REAL`) works

## EJ Logs (VM3)
- [ ] Real EJ logs searchable in OpenSearch Dashboards
- [ ] PCI DSS card masking verified
- [ ] Retention policy configured (90 days minimum)

## Reporting (VM4)
- [ ] Report Portal generates correct reports with real ATM data
- [ ] Scheduled reports delivering to correct recipients

## Security & Compliance
- [ ] SNMP community strings are bank-specific (not "public")
- [ ] SNMP v3 used where available
- [ ] PCI DSS card masking confirmed for real EJ data
- [ ] Access restricted to authorized staff

## Backup & Recovery
- [ ] Daily PostgreSQL backups running and stored off-server
- [ ] Weekly OpenSearch snapshots running
- [ ] Full restore tested at least once with documented RTO
- [ ] Zabbix configuration XML committed to git repo

## Documentation & Handover
- [ ] OID mapping table saved in repository
- [ ] Operations staff walked through onboarding workflow
- [ ] Troubleshooting guide available for common failures

---

# 16. Quick Troubleshooting Reference

| Problem | Likely Cause | Fix |
|---|---|---|
| `zabbix-server` won't start, "Cannot connect to database" | PostgreSQL password mismatch or firewall | Check `.env` password matches VM2. Check port 5432 firewall. |
| Zabbix items show "Not supported" | Wrong item type (HTTP vs SNMP) or wrong OID | Verify the item is SNMP type. Check OID with snmpwalk. |
| Report Portal shows "DB connection error" | DB_HOST points to wrong IP | Ensure VM4's `DB_HOST: 10.200.1.2` |
| Grafana datasource "Zabbix-ATM" fails | VM1's Zabbix web not accessible from VM4 | Check firewall port 8080 between VM4 and VM1 |
| ISO 8583 Gateway "Address already in use" | Port 9876 already occupied | `ss -tlnp | grep 9876` to find the process, then stop it |
| OpenSearch won't start | `vm.max_map_count` not set | `sudo sysctl -w vm.max_map_count=262144` |
| Filebeat won't start | `filebeat.yml` not owned by root | `sudo chown root:root filebeat.yml` |
| OpenSearch Dashboards shows "No data" | No indices created yet | Wait for Filebeat to ship logs. Check `curl localhost:9200/_cat/indices` |
| GLPI 502 Bad Gateway | PHP worker timeout, or GLPI not fully installed | Complete GLPI installation wizard. Restart GLPI container. |
| Grafana PDF export fails | Renderer not running | Check `docker ps | grep grafana-renderer`. Verify `GF_RENDERING_SERVER_URL` |
| Anomaly Detector writes no anomalies | Too few transactions to trigger rules | In simulation mode, wait for ~50+ transactions. In production, check thresholds. |
| Network Correlator shows no data | Zabbix API not reachable | Check `ZABBIX_URL` env var on VM5. Verify firewall. |

---

# 17. Summary of Files

| Purpose | File |
|---|---|
| Original PoC compose | `docker-compose.yml` |
| Production VM1 (Zabbix) | `deploy/production/docker-compose-vm1-zabbix.yml` |
| Production VM2 (PostgreSQL) | `deploy/production/docker-compose-vm2-postgres.yml` |
| Production VM3 (OpenSearch) | `deploy/production/docker-compose-vm3-opensearch.yml` |
| Production VM4 (Dashboards) | `deploy/production/docker-compose-vm4-dashboards.yml` |
| Production VM5 (Gateway) | `deploy/production/docker-compose-vm5-gateway.yml` |
| UAT VM1 (All-in-one) | `deploy/uat/docker-compose-uat-vm1.yml` |
| UAT VM2 (OpenSearch) | `deploy/uat/docker-compose-uat-vm2.yml` |
| Production env vars | `deploy/production/.env.production` |
| UAT env vars | `deploy/uat/.env.uat` |
| Production Grafana datasources | `config/grafana/datasources-production.yml` |
| PostgreSQL tuning config | `config/postgres-production/postgresql-custom.conf` |
| Filebeat config | `filebeat.yml` |
| Database schema + seed data | `config/postgres/atm_custom_tables.sql` |
| Zabbix template (HTTP) | `config/zabbix/zbx_export_templates.xml` |
| Zabbix hosts | `config/zabbix/zbx_export_hosts.xml` |
| Zabbix media types | `config/zabbix/zbx_export_mediatypes.xml` |
| SNMP template | Create in Zabbix UI via clone |
| ISO 8583 Gateway | `camel/iso8583_gateway.py` |
| Report Portal | `report-portal/app.py` and `routes.py` |
| Anomaly Detector | `anomaly_detector.py` |
| Network Correlator | `network_correlator.py` |
| Admin form (ATM registration) | `report-portal/blueprints/admin.py` |
| EJ search | `report-portal/blueprints/ej_search.py` |
| Grafana dashboards (6) | `config/grafana/dashboards/*.json` |
| Backup script | `scripts/backup_db.sh` |
| Restore script | `scripts/restore_db.sh` |
| PoC setup script | `scripts/setup_new_machine.sh` |

---

*End of guide. Last updated: July 2026.*
