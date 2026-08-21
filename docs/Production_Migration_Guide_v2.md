# Dashen Bank — ATM Monitoring System
# Production Migration Guide v3 (Revised 2026-08-06)

## From PoC (Single Laptop / Simulated ATMs) to Production (3 VMs / Real NCR + GRG ATMs)

**Based on:** Server Infrastructure Specification (Revised — August 2026) + Collection Strategy decision (`docs/collection-strategy.md`)
**Target:** Production — RHEL 9, **3 VMs (APPS-01, DATA-01, GWY-01)**, real Dashen ATM fleet (NCR + GRG only), 3-Year Data Retention
**Supersedes:** v2 (which assumed the old 5-VM layout — all VM references below follow the revised 3-VM specification)

---

# 0. Read This First — Corrections & Re-prioritization

1. **The PoC simulators are collected over HTTP, not SNMP.** The Zabbix templates use `HTTP agent` items hitting `http://172.17.0.1:{$ATM_PORT}/oid/...`. Real ATMs must be collected over **SNMP** (UDP 161). The production cutover is *not* "only change the address" — it is "change item type HTTP→SNMP **and** re-map OIDs to the real NCR/GRG MIB trees." See §8.1 and `docs/collection-strategy.md` §4.
   - **Mitigation (do this during the build phase, not at cutover):** make the simulators emit **real SNMP** (e.g. `snmp-simulator` / `snmp4arts`) so the Zabbix items are SNMP-native from day one. Then production is truly "point Zabbix at real ATMs."

2. **Fleet size is inconsistent between planning docs.** Earlier guides said 2,300–2,700 ATMs; Abinet's inventory confirmed **1,202 ATMs** (798 GRG + 429 NCR). Capacity planning and VM sizing use this confirmed count — the revised spec (24 vCPU / 72 GB / ~4 TB) has comfortable headroom even for growth toward 5,000.

3. **Full-fleet cutover in 2 months is not realistic.** Build the platform + connect a **pilot wave (10–50 ATMs)** over SNMP, run **parallel to NetXMS**, validate, then phase the rest. See §8.7 (Parallel Run) and `docs/collection-strategy.md` §5.

4. **Zabbix proxies are deferred to Phase 2.** A single centralized Zabbix server (8 vCPU on APPS-01) easily handles 1,202 ATMs × 40 items = 48,000 items. Proxies are considered only if real performance monitoring proves they're needed. See `docs/proxy-topology.md` and `docs/uat-pilot-checklist.md`.

5. **Server layout changed from 5 VMs to 3 VMs** (revised spec, August 2026). Services that previously had their own VM now share tiered servers because they are not all busy at the same time:
   - **APPS-01** — Zabbix server + web, Grafana, GLPI, Report Portal, anomaly detector, network correlator, state manager (8 vCPU / 16 GB / 400 GB)
   - **DATA-01** — PostgreSQL + OpenSearch + OpenSearch Dashboards + Filebeat (12 vCPU / 48 GB / 3.5 TB)
   - **GWY-01** — ISO 8583 Gateway only (4 vCPU / 8 GB / 100 GB) — kept separate for network isolation from the bank's switch

---

# 1. Introduction

## 1.1 What This Guide Does

This guide takes the ATM Monitoring System — currently running on your laptop with simulated ATMs (all in one Docker Compose file) — and moves it onto Dashen Bank's production infrastructure: **3 RHEL 9 virtual machines** serving the real Dashen ATM fleet.

| **PoC Assumed** | **Production Spec** |
|---|---|
| 1 laptop (Docker Compose) | 3 VMs (RHEL 9) |
| ~18 services in one compose | Services split across 3 VMs |
| Simulated ATMs over HTTP | Real NCR/GRG ATMs over SNMP |
| 5–22 ATMs | Real fleet (1,202 ATMs confirmed — see §0.2) |
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

## 1.4 The Big Change: From One Compose File to Three

Your PoC has one `docker-compose.yml` with ~18 services all talking to each other by container name (`postgres`, `opensearch`, `zabbix-server`).

In production, services are spread across 3 VMs. They cannot use Docker's internal DNS. Instead, they use **IP addresses** across the bank's network.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         DASHEN BANK INTERNAL NETWORK                          │
│                                                                              │
│  ┌───────────────────────────┐       ┌───────────────────────────┐           │
│  │ APPS-01  8 vCPU / 16 GB   │       │ DATA-01  12 vCPU / 48 GB  │           │
│  │ 400 GB NVMe               │       │ 3.5 TB NVMe               │           │
│  │                           │       │                           │           │
│  │ zabbix-server ────────────┼───────┼─> PG 172.26.18.102:5432     │           │
│  │ zabbix-web                │       │ opensearch                │           │
│  │ grafana / renderer        │       │ os-dashboards             │           │
│  │ glpi + mariadb            │       │ filebeat ──> local OS:9200│           │
│  │ report-portal ────────────┼───────┼─> PG :5432, OS :9200      │           │
│  │ anomaly-detector          │       │                           │           │
│  │ network-correlator        │       │ Ports: 5432, 9200, 5601   │           │
│  │ state-manager             │       └───────────────────────────┘           │
│  │                           │                                              │
│  │ Ports: 8080, 3000, 8082,  │        ┌───────────────────────────┐          │
│  │ 8888                      │        │ GWY-01   4 vCPU / 8 GB    │          │
│  └───────────┬───────────────┘        │ 100 GB NVMe               │          │
│              │                        │                           │          │
│              │ Zabbix API (local)     │ iso8583-gateway ──────────┼─> PG:5432│
│              │                        │ Port: 9876 (to switch)   │          │
│              ▼                        └───────────────────────────┘          │
│  UDP 161 → ATM network (SNMP polling)                                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

Every arrow is a **firewall rule you must request from Dashen IT**. Section 3.2 lists them all.

---

# 2. Your Production Architecture

## 2.1 The 3 Production VMs

### APPS-01 — Zabbix + Dashboards + Reports + Intelligence
- **Spec:** 8 vCPU, 16 GB RAM, 400 GB NVMe SSD
- **Runs:** Zabbix Server, Zabbix Web (UI), Zabbix Agent (for the VM itself), Grafana, Grafana Image Renderer, GLPI + MariaDB, Report Portal, Anomaly Detector, Network Correlator, State Manager
- **Why one VM for all of these:** they are never busy at the same time (dashboards in the morning, reports at month-end, alerts only on problems). Zabbix's own sizing guide puts 5,000 hosts at ~4 vCPU / 8 GB — 8/16 gives double headroom.
- **Open ports:** 8080 (Zabbix web), 10051 (trapper/proxy), 10050 (agent), 3000 (Grafana), 8082 (GLPI), 8888 (Report Portal)

### DATA-01 — PostgreSQL + OpenSearch (the storage tier)
- **Spec:** 12 vCPU, 48 GB RAM, 3.5 TB NVMe SSD
- **Runs:** PostgreSQL 15 (single instance — monitoring history, trends, `atm_transactions`), OpenSearch + OpenSearch Dashboards, Filebeat
- **Why 48 GB / 3.5 TB:** PostgreSQL cache (shared_buffers ~12–16 GB) + OpenSearch JVM heap (16 GB) + ~2.5 years of searchable EJ journals + 30-day history + 3-yr transactions ≈ 3.5 TB.
- **Open ports:** 5432 (PostgreSQL — **only** to APPS-01 and GWY-01), 9200 (OpenSearch — to APPS-01 only), 5601 (OpenSearch Dashboards — staff workstations, optional)

### GWY-01 — ISO 8583 Gateway
- **Spec:** 4 vCPU, 8 GB RAM, 100 GB NVMe SSD
- **Runs:** ISO 8583 Gateway (TCP listener)
- **Why its own VM:** it is the **only ingress from the bank's switch**. Network isolation (DMZ/blast-radius) is standard for banking integrations — if this VM is compromised, the attacker hits a gateway, not the database.
- **Open port:** 9876 (ISO 8583 — to ATM switch network only)

## 2.2 The 2 UAT VMs

| VM | Services | Spec |
|---|---|---|
| **UAT-01** | Zabbix + PostgreSQL + Grafana + GLPI + Report Portal (all-in-one) | 4 vCPU, 8 GB RAM, 500 GB |
| **UAT-02** | OpenSearch + OpenSearch Dashboards + ISO 8583 Gateway | 4 vCPU, 8 GB RAM, 200 GB |

UAT replicates the **shape** of the system, not its 1,202-ATM scale — 10–20 simulated ATMs are enough to validate templates, dashboards, and reports. Full UAT deployment steps are in **`docs/UAT_Migration_Guide.md`**.

## 2.3 Why Not One Big Server? (And Why Not Five?)

**Why not one big server:**
1. **I/O isolation** — PostgreSQL and OpenSearch are both I/O-hungry. DATA-01 owns that tier alone, so Zabbix polling and EJ indexing never compete with dashboard/report queries for the same disk.
2. **Security zones** — the ISO 8583 gateway (GWY-01) must be reachable from the ATM switch network. That port should **not** be on the same VM as 3 years of transaction history.
3. **Recovery scope** — if the data tier fails, apps stay up; if the gateway is compromised, the DB is untouched.

**Why not five VMs (as v2 assumed):** every extra VM is an OS to patch, a firewall rule-set, a backup stream, and a failure point. Zabbix server + web + app services on one VM is a standard, documented deployment at this fleet size. The two separations that matter are kept (data tier + DMZ). The revised spec says so explicitly — the same services, the same coverage, fewer machines.

---

# 3. What You Need Before Starting

## 3.1 Access Checklist

- [ ] **SSH access** to all 5 VMs (3 production + 2 UAT) — ask IT to create your user and add to `wheel` group
- [ ] **RHEL 9 subscription** or local repo access
- [ ] **Docker** installed (instructions in Phase 1 — same for all VMs)
- [ ] **Git** installed on all VMs
- [ ] **GitHub SSH key** or personal access token to clone the repo
- [ ] **One real ATM** for testing — ask the ATM hardware team for a test ATM with known IP and SNMP community string
- [ ] **Contact person** on the ATM Switch team (for ISO 8583)
- [ ] **Contact person** on the ATM Hardware/Vendor team (for SNMP OIDs and EJ paths)
- [ ] **Branch network spreadsheet** with ATM IDs, branch names, GPS coordinates, terminal IDs, vendor, model, install dates

## 3.2 Firewall Rules — Handled by Other Departments (Verify Only)

> **Division of labor (confirmed August 2026):** VM provisioning, VLANs, security groups, and firewall rules are handled by the Cloud & Core / Network / Security teams (see the Rahel Kiros & Jemil J. email thread). **You do not request these — you verify them.** The tables below are the reference; the phases include a connectivity check after each deployment step.

**Network layout (confirmed with Cloud & Core / Security, August 2026):** APPS-01 and GWY-01 sit on **VLAN 4055** (172.26.18.64/28), DATA-01 on **VLAN 4056** (172.26.18.96/28) — production traffic between APPS/GWY and DATA-01 is therefore **inter-VLAN**. UAT-01 is on VLAN 4029 (172.26.208.0/24), UAT-02 on VLAN 4021 (172.26.21.0/24) — also inter-VLAN. The central repo server **172.25.37.4** reaches all 5 RHEL servers for update activity.

| From | To | Port | Purpose |
|---|---|---|---|
| APPS-01 (VLAN 4055) | DATA-01 (VLAN 4056) | TCP 5432 | Zabbix / Grafana / Report Portal / anomaly detector / correlator → PostgreSQL |
| APPS-01 (VLAN 4055) | DATA-01 (VLAN 4056) | TCP 9200 | Report Portal EJ search queries |
| GWY-01 (VLAN 4055) | DATA-01 (VLAN 4056) | TCP 5432 | ISO 8583 writes transactions |
| GWY-01 (Gateway) | ATM Switch | TCP 9876 | ISO 8583 messages |
| APPS-01 (Zabbix) | ATM Network | UDP 161 | SNMP polling |
| Staff workstations | APPS-01 | TCP 8080, 3000, 8082, 8888 | Zabbix web, Grafana, GLPI, Report Portal |
| Staff workstations | DATA-01 | TCP 5601 | OpenSearch Dashboards (optional) |
| Repo server 172.25.37.4 | All 5 RHEL servers | TCP 443, 80 | Update activity (requested by Cloud & Core, Aug 12) |

**All rules must be internal-only.** No port exposed to the public internet.

## 3.3 Information from the ATM Switch Team

Ask the Channel Support / ATM Switch team for these 5 things:

1. **Connection direction** — Does the switch connect to our listener (TCP server mode), or must we connect to the switch (client mode)?
2. **ISO 8583 variant** — Different switches (Base24, Postilion, etc.) have different bitmap/field definitions.
3. **Sample message captures** — Hex dumps or pcap files of 5–10 real messages to validate the parser.
4. **Switch IP and port** — Must be reachable from GWY-01.
5. **Test/UAT switch environment** — To validate before hitting production.

## 3.4 Information from the ATM Hardware Team

Open a ticket with the ATM vendor support team for:

1. **SNMP MIB and OIDs** — The enterprise OID and specific OIDs for: cassette levels, door sensors, card reader, printer, temperature, cameras, network, power. Also whether SNMP v3 is available (preferred over v2 for security).
2. **SNMP community string** — Must be bank-specific, not "public".
3. **EJ log file paths and format** — Exact path on the ATM OS (e.g., `C:\Program Files\NCR\APTRA\Journal\`), naming convention, log format (pipe-delimited, fixed-width, etc.).
4. **Existing EJ collection process** — Does the bank already collect EJ files centrally? If yes, we point Filebeat there instead of per-ATM installs.

---

# 3.5 Revised Priorities (per Senior Engineer Review)

The original phase ordering (deploy VMs → import templates → connect ATMs) assumed the main risk was infrastructure deployment. The **highest risks are elsewhere**:

## Priority 1 — Validate One Real ATM End-to-End (Highest Risk)

Before deploying any VMs or splitting compose files, validate that a real ATM can be monitored:

1. Get **one real ATM IP** and its SNMP community string from the ATM/Channel Support team
2. From the sandbox (or any Linux machine): `snmpwalk -v2c -c <community> <atm-ip> .1.3.6.1.4.1.37513` (NCR) or similar vendor OID
3. Map real OIDs to our sim OIDs — build the translation table
4. Confirm the real ATM responds to SNMP at all (some vendors disable SNMP by default)

**If this fails, nothing else matters.** The entire collection model depends on SNMP working against real NCR/GRG hardware.

## Priority 2 — Confirm SNMP Credentials and Network Path

1. Verify SNMP community string with the bank (is it v2c? v3? one community across all ATMs or per-vendor?)
2. Request firewall rules: UDP 161 from the Zabbix server IP (APPS-01) to ATM subnet(s)
3. Test reachability to a representative ATM from the UAT VM1 (not just sandbox)

## Priority 3 — Pilot 20–50 ATMs Across Different Districts

Once 1 ATM works, expand to a pilot set covering multiple districts, vendors (NCR + GRG), and network paths. Validate:
- Polling intervals are achievable (30s for critical items)
- Timeout rates (expect some unreachable ATMs)
- Template OID mappings work across the fleet
- No district has systematic connectivity issues

## Priority 4 — Scale to Full Fleet

Only after the pilot is stable. This means:
- Import all 1,202 hosts into Zabbix
- Tune polling intervals (critical items 30s, normal hourly, static daily)
- Monitor server load (CPU, DB connections, disk I/O)
- Build the operational dashboard (online/offline, cash low, alerts, availability %)

## What This Means for the Phase Order

The phase ordering below (Phases 1–10) is still valid as a **reference**, but the **actual sequence** should be:

1. **Validate 1 real ATM** (wasn't in original plan — now top priority)
2. **Deploy UAT VMs** (Phase 1 — to have a proper test environment)
3. **Pilot 20–50 ATMs** (was §8.7, now moves earlier)
4. **Deploy Production VMs** (Phase 2)
5. **Scale to full fleet**

Proxies are removed from this timeline entirely — they are a Phase 2 consideration after the centralized deployment is stable.

---

# 4. Phase 0 — Per-VM Deploy Files (Already in the Repo)

**The split is done.** The files below already exist in the repo (created August 2026) with the confirmed IPs baked in. You deploy by **`git clone` → copy `.env` → `docker compose up`** — you do not hand-write compose files. Read the steps to understand the layout; override a file only if you need a change (e.g. real switch IP in Phase 6).

## 4.1 Service-to-VM Mapping

| Container | PoC Name | Prod VM | Key Change |
|---|---|---|---|
| Zabbix Server | `zabbix-server` | APPS-01 | DB_HOST → DATA-01 IP |
| Zabbix Web | `zabbix-web` | APPS-01 | DB_HOST → DATA-01 IP |
| Zabbix Agent | `zabbix-agent` | APPS-01 | Monitors the VM itself |
| Grafana | `grafana` | APPS-01 | Datasources use VM IPs |
| Grafana Renderer | `grafana-renderer` | APPS-01 | For PDF generation |
| MariaDB (GLPI) | `glpi-db` | APPS-01 | Local to APPS-01 |
| GLPI | `glpi` | APPS-01 | Points to local MariaDB |
| Report Portal | `report-portal` | APPS-01 | DB_HOST → DATA-01, OS_HOST → DATA-01 |
| Anomaly Detector | `anomaly-detector` | APPS-01 | DB_HOST → DATA-01 |
| Network Correlator | `network-correlator` | APPS-01 | DB_HOST → DATA-01, Zabbix local |
| State Manager | `state-manager` | APPS-01 | DB_HOST → DATA-01 |
| PostgreSQL | `zabbix-db` | DATA-01 | Exposed on port 5432 |
| OpenSearch | `opensearch` | DATA-01 | 16 GB JVM heap |
| OpenSearch Dashboards | `opensearch-dashboards` | DATA-01 | Points to local OS |
| Filebeat | `filebeat` | DATA-01 | Watches local + real EJ log dirs |
| ISO 8583 Gateway | `iso8583-gateway` | GWY-01 | DB_HOST → DATA-01 |
| Simulators (multi-tenant engines) | `atm-sim-engine`, `atm-txn-engine`, `atm-ej-engine` | UAT only | Not in production |

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
  DB_HOST: 172.26.18.102          # DATA-01's IP
  OS_HOST: 172.26.18.102:9200     # DATA-01's IP (same VM as the DB now)
  ZABBIX_URL: http://172.26.18.74:8080/api_jsonrpc.php  # APPS-01's IP
```

Your actual IPs (confirmed with IT, August 2026):

| VM | Server Name (per IT mapping) | VLAN / Network | IP |
|---|---|---|---|
| APPS-01 (Zabbix/dashboards) | DBHQPRODATMMONAPP | VLAN 4055 — 172.26.18.64/28 (gw 172.26.18.65) | 172.26.18.74 |
| DATA-01 (PostgreSQL/OpenSearch) | DBHQPRODATMMONDB | VLAN 4056 — 172.26.18.96/28 (gw 172.26.18.97) | 172.26.18.102 |
| GWY-01 (ISO 8583 Gateway) | DBHQPRODATMMONGW | VLAN 4055 — 172.26.18.64/28 (gw 172.26.18.65) | 172.26.18.76 |
| UAT-01 (all-in-one) | DBHQUATATMMONAPP | VLAN 4029 — 172.26.208.0/24 (gw 172.26.208.1) | 172.26.208.176 |
| UAT-02 (OpenSearch) | DBHQUATATMMONDB | VLAN 4021 — 172.26.21.0/24 (gw 172.26.21.1) | 172.26.21.50 |

## 4.3 Step-by-Step: Create Per-VM Compose Files

### Step 4.3.1 — Create directories

On your laptop (where the GitHub repo lives):

```bash
mkdir -p deploy/production
mkdir -p deploy/uat
```

### Step 4.3.2 — APPS-01 compose file (Zabbix + dashboards + reports)

Create `deploy/production/docker-compose-apps.yml`:

```yaml
services:
  zabbix-server:
    image: zabbix/zabbix-server-pgsql:rhel-6.4-latest
    container_name: zabbix-server
    environment:
      DB_SERVER_HOST: "172.26.18.102"
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
      DB_SERVER_HOST: "172.26.18.102"
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
      ZBX_HOSTNAME: "Zabbix-Server-APPS01"
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
      - ./config/grafana/datasources-production.yml:/etc/grafana/provisioning/datasources/datasources.yml:ro
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
      DB_HOST: "172.26.18.102"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      GRAFANA_URL: "http://172.26.18.74:3000"
      REPORT_PORTAL_PORT: "8888"
      OS_HOST: "172.26.18.102:9200"
      OS_INDEX: "atm-ej-live-*,atm-electronic-journal"
    volumes:
      - ./report-portal:/app:rw
    ports:
      - "8888:8888"
    restart: unless-stopped

  anomaly-detector:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: anomaly-detector
    command: python3 /app/anomaly_detector.py
    environment:
      DB_HOST: "172.26.18.102"
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
      DB_HOST: "172.26.18.102"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      ZABBIX_URL: "http://172.26.18.74:8080/api_jsonrpc.php"
      ZABBIX_USER: "Admin"
      ZABBIX_PASS: "zabbix"
      CHECK_INTERVAL: "120"
      LATENCY_THRESHOLD: "200"
      LOSS_THRESHOLD: "10"
    volumes:
      - ./network_correlator.py:/app/network_correlator.py:ro
    restart: unless-stopped

  state-manager:
    build:
      context: ./simulators
      dockerfile: Dockerfile.atm-simulator
    container_name: state-manager
    command: python3 /app/state_manager.py
    environment:
      DB_HOST: "172.26.18.102"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
    restart: unless-stopped

volumes:
  mariadb-data:
  glpi-root:
  grafana-data:
```

### Step 4.3.3 — DATA-01 compose file (PostgreSQL + OpenSearch)

Create `deploy/production/docker-compose-data.yml`:

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

volumes:
  pgdata:
  os-data:
```

**You must tune PostgreSQL for the DATA-01 specs.** Create the performance config:

```bash
mkdir -p config/postgres-production
```

Create `config/postgres-production/postgresql-custom.conf`:

```
# Dashen Bank ATM — PostgreSQL Performance Tuning
# Target: DATA-01 with 48 GB RAM (shared with OpenSearch), 12 vCPU, 3.5 TB NVMe SSD

shared_buffers = '12GB'
effective_cache_size = '32GB'
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

> Note: `shared_buffers = 12 GB` (not 16 GB) because OpenSearch's 16 GB JVM heap lives on the same VM. PostgreSQL + OpenSearch share the 48 GB budget: ~12 GB shared_buffers + ~16 GB heap + OS cache headroom.

**Why `OPENSEARCH_JAVA_OPTS=-Xms16g -Xmx16g`?** OpenSearch's JVM heap should not exceed 50% of available RAM (the rest is for the OS filesystem cache, which OpenSearch relies on heavily). 16 GB of 48 GB is the sweet spot.

### Step 4.3.4 — Update `filebeat.yml` for production

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

**`hosts: ["localhost:9200"]`** because both Filebeat and OpenSearch run on DATA-01.

### Step 4.3.5 — GWY-01 compose file (ISO 8583 Gateway)

Create `deploy/production/docker-compose-gateway.yml`:

```yaml
services:
  iso8583-gateway:
    build:
      context: ./camel
      dockerfile: Dockerfile.gateway
    container_name: iso8583-gateway
    environment:
      MODE: "simulation"
      DB_HOST: "172.26.18.102"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
      INTERVAL: "10"
    ports:
      - "9876:9876"
    restart: unless-stopped
```

### Step 4.3.6 — Create production Grafana datasources

The PoC's `config/grafana/datasources.yml` uses container names. Create a production version at `config/grafana/datasources-production.yml`:

```yaml
apiVersion: 1

datasources:
  - name: Zabbix-ATM
    type: alexanderzobnin-zabbix-datasource
    access: proxy
    url: http://172.26.18.74:8080/api_jsonrpc.php
    jsonData:
      username: Admin
    secureJsonData:
      password: zabbix

  - name: ATM-Transactions
    type: postgres
    url: 172.26.18.102:5432
    database: zabbix
    user: zabbix
    secureJsonData:
      password: zabbix_pass
    jsonData:
      sslmode: disable
      postgresVersion: 1500

  - name: EJ-OpenSearch
    type: elasticsearch
    url: http://172.26.18.102:9200
    jsonData:
      index: atm-*
      timeField: "@timestamp"
      esVersion: "7.10.0"
      logMessageField: message
      logLevelField: status
```

### Step 4.3.7 — UAT compose files

The UAT deployment is covered in full by **`docs/UAT_Migration_Guide.md`**. In short:

- `deploy/uat/docker-compose-uat-vm1.yml` — all services except OpenSearch/Dashboards (same as the current PoC, with `rhel-6.4-latest` Zabbix images and Report Portal's `OS_HOST` pointing to UAT-02's IP)
- `deploy/uat/docker-compose-uat-vm2.yml` — OpenSearch + OpenSearch Dashboards + Filebeat + ISO 8583 Gateway

### Step 4.3.8 — Production .env file

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

> **This phase is fully documented in `docs/UAT_Migration_Guide.md`.** It covers, step by step:
> 1. UAT-02 (OpenSearch) deployment
> 2. UAT-01 (everything else) deployment
> 3. Zabbix template/host/media-type imports
> 4. Grafana, Report Portal, and GLPI verification
> 5. End-to-end UAT sign-off checklist
> 6. UAT exit criteria (what must be true before you touch production)

The rest of this section is the quick version.

## 5.1 Deploy UAT-02 (OpenSearch + OpenSearch Dashboards)

### Step 5.1.1 — SSH into UAT-02

```bash
ssh <your-username>@172.26.21.50
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
sudo dnf install -y git
cd /opt
sudo mkdir -p atm-monitoring
sudo chown $USER:$USER atm-monitoring
git clone <YOUR_GITHUB_URL> atm-monitoring
cd atm-monitoring
```

### Step 5.1.4 — Configure sysctl for OpenSearch

OpenSearch requires increased mmap limits:

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
sudo swapoff -a
```

### Step 5.1.5 — Create required directories and set up .env

```bash
cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env
mkdir -p ej-logs
```

### Step 5.1.6 — Start OpenSearch + OpenSearch Dashboards + Filebeat

```bash
docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d

echo "Waiting 60 seconds for OpenSearch to initialize..."
sleep 60

curl -s http://localhost:9200
# Expected: JSON response with "cluster_name" and "version"

curl -s http://localhost:9200/_cat/indices?v
```

## 5.2 Deploy UAT-01 (Everything Else)

### Step 5.2.1 — SSH into UAT-01

```bash
ssh <your-username>@172.26.208.176
```

### Step 5.2.2 — Install Docker + clone repo (same as steps 5.1.2–5.1.3)

### Step 5.2.3 — Set up environment

```bash
cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env

mkdir -p ej-logs reports config/zabbix config/grafana/dashboards config/postgres
sudo mkdir -p /data/real-ej-logs
sudo chown $USER:$USER /data/real-ej-logs
```

### Step 5.2.4 — Fix Filebeat and EJ log permissions

```bash
sudo chown -R $USER:$USER ej-logs/
chmod 755 ej-logs/
sudo chown root:root filebeat.yml
sudo chmod 644 filebeat.yml
```

### Step 5.2.5 — Start all services

```bash
# Build custom images
docker compose -f deploy/uat/docker-compose-uat-vm1.yml build --no-cache \
  atm-sim-engine atm-txn-engine atm-ej-engine state-manager \
  report-portal iso8583-gateway

# Start PostgreSQL first (everything depends on it)
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

# Create the custom database tables
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql

# Start everything else
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d
sleep 60
```

### Step 5.2.6 — Verify all containers are running

```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v "Exited"
```

You should see containers all showing "Up": postgres, zabbix-server, zabbix-web, zabbix-agent, mariadb, glpi, grafana, grafana-renderer, report-portal, iso8583-gateway, anomaly-detector, network-correlator, atm-sim-engine, atm-txn-engine, atm-ej-engine, state-manager, pgadmin.

### Step 5.2.7 — Verify PostgreSQL has data

```bash
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*) FROM atm_transactions;"
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT atm_id, branch FROM atm_locations;"
```

## 5.3 Import Zabbix Configuration

1. Browse to `http://172.26.208.176:8080`, log in `Admin` / `zabbix`
2. **Configuration → Templates → Import** → `config/zabbix/zbx_export_templates.xml` → Import
3. **Configuration → Hosts → Import** → `config/zabbix/zbx_export_hosts.xml` → Import
4. **Administration → Media types → Import** → `config/zabbix/zbx_export_mediatypes.xml` → Import
5. Verify hosts appear (ATM-001 through ATM-005)

## 5.4 Verify Grafana, Report Portal, GLPI

- Grafana at `http://172.26.208.176:3000` — 6 dashboards, Zabbix-ATM + ATM-Transactions datasources test OK
- Report Portal at `http://172.26.208.176:8888` — generate a PDF/Excel/CSV report
- GLPI at `http://172.26.208.176:8082` — complete install wizard, enable REST API, create API client, wire the Zabbix media type

## 5.5 UAT End-to-End Sign-Off

- [ ] All containers running
- [ ] Zabbix shows hosts with data
- [ ] Grafana dashboards show live simulator data
- [ ] Report Portal generates reports
- [ ] GLPI accessible + API enabled + tickets auto-create from a trigger
- [ ] Transactions written to `atm_transactions`
- [ ] EJ logs written to `ej-logs/` and searchable in OpenSearch Dashboards
- [ ] ISO 8583 gateway writes `ISO8583_SIM` transactions

**Full UAT steps + exit criteria: see `docs/UAT_Migration_Guide.md`.**

---

# 6. Phase 2 — Deploy Production VMs

Now that UAT is working, repeat the deployment on the 3 production VMs. **Deploy in this order:**

1. **DATA-01** — everything depends on the database (and OpenSearch for Filebeat/reporting)
2. **APPS-01** — needs DB, then provides Zabbix API + dashboards
3. **GWY-01** — needs DB

## 6.1 Deploy DATA-01 — PostgreSQL + OpenSearch

### Step 6.1.1 — SSH in and install Docker

```bash
ssh <your-username>@172.26.18.102

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

mkdir -p config/postgres-production

cat > config/postgres-production/postgresql-custom.conf << 'CONF'
shared_buffers = '12GB'
effective_cache_size = '32GB'
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

### Step 6.1.4 — Configure sysctl for OpenSearch

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
sudo swapoff -a
```

### Step 6.1.5 — Start PostgreSQL + OpenSearch

```bash
docker compose -f deploy/production/docker-compose-data.yml up -d

echo "Waiting for PostgreSQL..."
sleep 15
docker exec zabbix-db pg_isready -U zabbix -d zabbix

echo "Waiting 60 seconds for OpenSearch..."
sleep 60
curl -s http://localhost:9200
```

### Step 6.1.6 — Create custom tables

```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql

docker exec zabbix-db psql -U zabbix -d zabbix -c "\dt"
# Should show: atm_locations, atm_transactions, atm_anomalies, atm_network_events, etc.
```

### Step 6.1.7 — Allow remote connections

By default, PostgreSQL inside the container only listens on localhost:

```bash
ss -tlnp | grep 5432
# Should show: 0.0.0.0:5432

# If it only shows 127.0.0.1:5432:
docker exec zabbix-db bash -c "echo \"listen_addresses = '*'\" >> /var/lib/postgresql/data/postgresql.conf"
docker compose -f deploy/production/docker-compose-data.yml restart postgres

# Allow connections from your VM IPs (VLAN 4055 — APPS-01 + GWY-01)
docker exec zabbix-db bash -c "echo 'host all all 172.26.18.64/28 md5' >> /var/lib/postgresql/data/pg_hba.conf"
docker compose -f deploy/production/docker-compose-data.yml restart postgres
```

### Step 6.1.8 — Test remote connection (from your laptop)

```bash
psql -h 172.26.18.102 -U zabbix -d zabbix -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
```

## 6.2 Deploy APPS-01 — Zabbix + Dashboards + Reports

### Step 6.2.1 — SSH in, install Docker, clone repo

(Repeat steps 6.1.1–6.1.2)

### Step 6.2.2 — Set up .env

```bash
cp deploy/production/.env.production .env
chmod 600 .env
nano .env
# IMPORTANT: POSTGRES_PASSWORD must match what you set on DATA-01!
```

### Step 6.2.3 — Start Zabbix

```bash
docker compose -f deploy/production/docker-compose-apps.yml up -d zabbix-server zabbix-web zabbix-agent

sleep 30
docker logs zabbix-server --tail 20
```

Look for:
```
connecting to database 'zabbix' on '172.26.18.102' port '5432'
database connection established
```

If you see "Cannot connect to database":
- Double-check the password in `.env`
- Verify firewall between APPS-01 and DATA-01 on port 5432
- Check `pg_hba.conf` on DATA-01 allows APPS-01's IP

### Step 6.2.4 — Verify Zabbix web UI

```bash
# From your browser:
# http://172.26.18.74:8080
# Log in: Admin / zabbix
```

### Step 6.2.5 — Start the rest

```bash
# Copy production datasources into place BEFORE Grafana starts
cp config/grafana/datasources-production.yml config/grafana/datasources.yml

docker compose -f deploy/production/docker-compose-apps.yml up -d
sleep 30
docker ps --format "table {{.Names}}\t{{.Status}}"
```

### Step 6.2.6 — Verify

```bash
curl -s http://localhost:3000/api/health          # Grafana
curl -s http://localhost:8888                     # Report Portal
curl -s -o /dev/null -w "%{http_code}" http://localhost:8082   # GLPI (expect 200)
```

## 6.3 Deploy GWY-01 — ISO 8583 Gateway

### Step 6.3.1 — SSH in, install Docker, clone repo

### Step 6.3.2 — Start

```bash
cp deploy/production/.env.production .env
chmod 600 .env
# POSTGRES_PASSWORD must match DATA-01

docker compose -f deploy/production/docker-compose-gateway.yml up -d
sleep 15
docker ps --format "table {{.Names}}\t{{.Status}}"
```

### Step 6.3.3 — Verify gateway is writing transactions

```bash
docker logs iso8583-gateway --tail 5
# Expected: "MODE: SIMULATION" and "generating 1 transaction every 10 seconds"

# Wait 30 seconds, then check DB from DATA-01:
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*), source FROM atm_transactions GROUP BY source;"
# Should show ~3 transactions with source = 'ISO8583_SIM'
```

## 6.4 Production Deployment Verification

- [ ] All 3 production VMs have Docker installed and running
- [ ] DATA-01: PostgreSQL running, accessible from other VMs, custom tables exist; OpenSearch responding at `http://172.26.18.102:9200`
- [ ] APPS-01: Zabbix web UI at `http://172.26.18.74:8080` connected to DATA-01's DB; Grafana datasources connecting; Report Portal + GLPI up
- [ ] GWY-01: ISO 8583 Gateway running and writing transactions to DATA-01's DB
- [ ] All containers set to `restart: unless-stopped`

---

# 7. Phase 3 — Import Zabbix Template and Hosts (Production)

Repeat the import steps from UAT (section 5.3) on the production Zabbix (APPS-01):

1. **Configuration → Templates → Import** → `config/zabbix/zbx_export_templates.xml`
2. **Configuration → Hosts → Import** → `config/zabbix/zbx_export_hosts.xml`
3. **Administration → Media types → Import** → `config/zabbix/zbx_export_mediatypes.xml`
4. Update GLPI media type:
   - `glpi_url`: `http://172.26.18.74:8082`
   - `app_token`: from GLPI API client setup (GLPI runs on APPS-01 now)

---

# 8. Phase 4 — Connect First Real ATM (SNMP Hardware Monitoring)

This is the most technically important phase. You will take one real ATM and make Zabbix display its real hardware status in the same Grafana dashboards.

## 8.1 Understanding the Change (READ CAREFULLY)

**The PoC simulators are collected over HTTP today, NOT SNMP.** Zabbix items use `HTTP agent` type, polling URLs like:
```
http://172.17.0.1:1161/oid/1.1.0
```

For real ATMs, items must use **SNMP agent** type, polling UDP port 161 against the real vendor MIB:
```
SNMP GET 10.10.1.50:161 .1.3.6.1.4.1.37513.1.1.0   (real NCR OID — NOT the sim's 1.1.0)
```

Two things change, not one:
1. **Item type:** `HTTP agent` → `SNMP agent`.
2. **OID/transport:** the sim's `1.1.0`-style OIDs are *shaped like* NCR/GRG but are **not** the real MIB trees. You must re-map each item to the **real** NCR/GRG OID obtained from the vendor MIB (see §8.2). The item **names**, **triggers**, **value maps**, and **Grafana dashboards** stay identical.

> **Best practice (do during build, before cutover):** make the simulators emit **real SNMP** so the Zabbix templates are SNMP-native from day one. If you do this, Phase 4 becomes "clone template, point at real ATM IP, set community" — no item-type rewrite. If you skip it, you rewrite ~30 items per template (NCR + GRG) at cutover. See `docs/collection-strategy.md` §4.

## 8.2 SNMP Walk the Test ATM

### Step 8.2.1 — Install SNMP tools on APPS-01

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
| "Timeout" | Network/firewall blocking UDP 161 | Ping ATM from APPS-01. Check firewall rules. |
| "Cannot connect" | Wrong IP or port | Verify IP and port 161 |
| "Unknown SNMP error" | Wrong community string | Run `snmpwalk` from APPS-01 to confirm |
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

## 8.7 Pilot Wave + Parallel Run (DO NOT big-bang)

Full-fleet cutover in the first 2 months is not realistic (vendor coordination, field work, validation). Instead:

1. **Pilot wave (10–50 ATMs):** connect a small, representative set (mix of NCR + GRG, several branches) over SNMP per §8.2–8.6.
2. **Run parallel to NetXMS** for at least 2–4 weeks. Daily compare: does our system show the same status/cash/faults as NetXMS for these ATMs? Fix discrepancies before expanding.
3. **Phased expansion:** after the pilot is trusted, onboard the rest in waves (e.g. by region/district). Each wave repeats §8.2–8.6.
4. **Keep simulators + NetXMS running** until the pilot is validated. Do not decommission (see Phase 9) until one full quarter of stable real-ATM operation.

This is the only safe path to "a system better than the current one" — you prove it on real data before touching the whole fleet.

---

# 9. Phase 5 — Connect Electronic Journal (EJ) Logs

The PoC's EJ generators write fake logs. For real ATMs, EJ files exist on the ATM's Windows OS and must reach DATA-01's Filebeat.

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
| **Network share** | ATM writes to a Windows share; DATA-01's Filebeat mounts it | No agent on ATM needed |
| **SFTP push** | ATM/collection process SFTPs files to DATA-01 | Bank already has EJ collection |
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
  hosts: ["172.26.18.102:9200"]
  index: "atm-ej-live-%{+yyyy.MM.dd}"
```

3. Install as Windows service:
```
powershell -ExecutionPolicy Unrestricted -File .\install-service-filebeat.ps1
```

### Option B: Network Share (No agent needed)

On DATA-01:
```bash
sudo mkdir -p /mnt/atm-ej-share
sudo mount -t cifs //<file-server>/atm-ej-logs /mnt/atm-ej-share \
  -o username=<user>,password=<pass>,domain=DASHEN
```

Then add to Filebeat volume in `docker-compose-data.yml`:
```yaml
volumes:
  - /mnt/atm-ej-share:/data/real-ej-logs:ro
```

### Option C: SFTP Push

On DATA-01:
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
curl -s http://172.26.18.102:9200/_cat/indices?v

# Check document count
curl -s "http://172.26.18.102:9200/atm-ej-live-*/_count"

# Browse OpenSearch Dashboards
# http://172.26.18.102:5601
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

On GWY-01, update the gateway environment:

```yaml
  iso8583-gateway:
    environment:
      MODE: "tcp"
      SWITCH_HOST: "0.0.0.0"
      SWITCH_PORT: "9876"
      DB_HOST: "172.26.18.102"
      DB_NAME: "zabbix"
      DB_USER: "zabbix"
      DB_PASS: "${DB_PASS}"
```

Then rebuild and restart:
```bash
docker compose -f deploy/production/docker-compose-gateway.yml build iso8583-gateway
docker compose -f deploy/production/docker-compose-gateway.yml up -d iso8583-gateway
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
psql -h 172.26.18.102 -U zabbix -d zabbix -c \
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
psql -h 172.26.18.102 -U zabbix -d zabbix
```

```sql
INSERT INTO atm_locations VALUES
('ATM-006', 'Adama Main Branch', 'Adama', 'Adama', 'Oromia',
 8.5400, 39.2700, 'TID0006', 'NCR', 'SelfServ 84',
 '2024-01-10', 'active')
ON CONFLICT (atm_id) DO NOTHING;
```

Or use the web form: `http://172.26.18.74:8888/admin/atm`

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
# Copy CSV to DATA-01
scp atm_locations_bulk.csv <user>@172.26.18.102:/tmp/

# On DATA-01:
docker cp /tmp/atm_locations_bulk.csv zabbix-db:/tmp/
docker exec zabbix-db psql -U zabbix -d zabbix -c \
  "\copy atm_locations FROM '/tmp/atm_locations_bulk.csv' WITH CSV HEADER"

# Verify
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*) FROM atm_locations;"
```

---

# 12. Phase 8 — Scale to Full Fleet (Auto-Discovery, Proxies as Phase 2)

Manually creating hosts for 1,202 ATMs is not feasible. Two mechanisms:

1. **Auto-discovery + auto-registration** (this phase) — Zabbix finds ATMs on the network and creates hosts automatically.
2. **Zabbix proxies** (Phase 2, only if measured performance demands it) — see `docs/proxy-topology.md`.

## 12.1 Create the Discovery Rule

1. **Configuration → Discovery → Create discovery rule**
2. **Name:** `Dashen ATM Network Scan`
3. **IP range:** The ATM network subnet(s) (ask IT), e.g., `10.10.1.1-254` per branch/zone
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
- **Location data** → Enter via admin form at `http://172.26.18.74:8888/admin/atm` (or bulk import, Phase 7)
- **EJ log shipping** → Configure per ATM (or use the centralized method from Phase 5)
- **ISO 8583 transactions** → Automatic (the gateway is switch-wide, not per-ATM)

## 12.4 When to Add Proxies (Phase 2 Decision Gate)

| Measure | Threshold | Action |
|---------|-----------|--------|
| Poll duration | >3s average | Consider proxy for that district |
| Timeout rate | >10% | Investigate network, then consider proxy |
| Server CPU | >80% sustained | Tune intervals, then consider proxy |
| WAN bandwidth | >50 Mbps sustained | Consider proxy |

If triggered, deploy **one** proxy for the worst-performing district first, measure improvement, then expand. See `docs/proxy-topology.md`.

---

# 13. Phase 9 — Decommission Simulators

Only once real ATMs are confirmed working. No rush — simulators do not interfere with real data.

## 13.1 Stop Simulator Containers (UAT)

```bash
docker compose stop \
  atm-sim-engine atm-txn-engine atm-ej-engine state-manager
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

| Data | VM | Method | Frequency | Location |
|---|---|---|---|---|
| PostgreSQL | DATA-01 | pg_dump | Daily | Off-server |
| OpenSearch | DATA-01 | Snapshot API | Weekly | Off-server |
| Zabbix config | APPS-01 | XML export | Per change | Git repo |
| Grafana dashboards | APPS-01 | Git (already in repo) | Per change | Git repo |
| GLPI database | APPS-01 | mysqldump | Daily | Off-server |

## 14.2 PostgreSQL Backup (DATA-01)

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

Schedule via crontab on DATA-01:
```bash
crontab -e
# Add:
0 2 * * * bash /opt/atm-monitoring/scripts/backup-production.sh >> /var/log/atm-backup.log 2>&1
```

**Off-server storage:** Coordinate with IT to mount an NFS/CIFS share to `/backups/`. A backup on the same server is not a backup.

## 14.3 OpenSearch Snapshots (DATA-01)

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

## 14.4 GLPI Backup (APPS-01)

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
- [ ] Server provisioned with agreed specifications (3 VMs)
- [ ] All Docker containers running and set to `restart: unless-stopped`
- [ ] Firewall rules allow only necessary internal traffic
- [ ] No ports exposed to public internet

## Zabbix (APPS-01)
- [ ] Real ATM(s) report hardware status via SNMP
- [ ] Triggers fire for: cash low/empty, door open, printer fault, network down
- [ ] Auto-discovery configured and tested
- [ ] GLPI tickets created automatically on trigger firing

## Grafana (APPS-01)
- [ ] Real ATMs appear on geo-map with correct locations
- [ ] ATM Fleet Overview table shows real data
- [ ] Drill-down dashboards work for real ATMs

## Transactions (GWY-01 → DATA-01)
- [ ] ISO 8583 Gateway receives real switch messages
- [ ] Transaction amounts, statuses match switch's records
- [ ] Source tagging (`ISO8583_REAL`) works

## EJ Logs (DATA-01)
- [ ] Real EJ logs searchable in OpenSearch Dashboards
- [ ] PCI DSS card masking verified
- [ ] Retention policy configured

## Reporting (APPS-01)
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
| `zabbix-server` won't start, "Cannot connect to database" | PostgreSQL password mismatch or firewall | Check `.env` password matches DATA-01. Check port 5432 firewall. |
| Zabbix items show "Not supported" | Wrong item type (HTTP vs SNMP) or wrong OID | Verify the item is SNMP type. Check OID with snmpwalk. |
| Report Portal shows "DB connection error" | DB_HOST points to wrong IP | Ensure APPS-01's `DB_HOST: 172.26.18.102` |
| Grafana datasource "Zabbix-ATM" fails | Zabbix web not reachable | Check Zabbix runs on APPS-01 (local) |
| ISO 8583 Gateway "Address already in use" | Port 9876 already occupied | `ss -tlnp | grep 9876` to find the process, then stop it |
| OpenSearch won't start | `vm.max_map_count` not set | `sudo sysctl -w vm.max_map_count=262144` |
| Filebeat won't start | `filebeat.yml` not owned by root | `sudo chown root:root filebeat.yml` |
| OpenSearch Dashboards shows "No data" | No indices created yet | Wait for Filebeat to ship logs. Check `curl localhost:9200/_cat/indices` |
| GLPI 502 Bad Gateway | PHP worker timeout, or GLPI not fully installed | Complete GLPI installation wizard. Restart GLPI container. |
| Grafana PDF export fails | Renderer not running | Check `docker ps | grep grafana-renderer`. Verify `GF_RENDERING_SERVER_URL` |
| Anomaly Detector writes no anomalies | Too few transactions to trigger rules | In simulation mode, wait for ~50+ transactions. In production, check thresholds. |
| Network Correlator shows no data | Zabbix API not reachable | Check `ZABBIX_URL` env var. Verify firewall. |

---

# 17. Summary of Files

| Purpose | File |
|---|---|
| Original PoC compose | `docker-compose.yml` |
| Production APPS-01 (Zabbix + dashboards + reports) | `deploy/production/docker-compose-apps.yml` |
| Production DATA-01 (PostgreSQL + OpenSearch) | `deploy/production/docker-compose-data.yml` |
| Production GWY-01 (ISO 8583 Gateway) | `deploy/production/docker-compose-gateway.yml` |
| UAT-01 (All-in-one) | `deploy/uat/docker-compose-uat-vm1.yml` |
| UAT-02 (OpenSearch) | `deploy/uat/docker-compose-uat-vm2.yml` |
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
| UAT migration guide | `docs/UAT_Migration_Guide.md` |

---

*End of guide. Last updated: August 2026.*