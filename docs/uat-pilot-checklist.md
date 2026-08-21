# UAT Pilot Checklist — 45-Day Production Readiness Plan

**Status:** Initial version (2026-07-25)
**Based on:** Senior engineer review of sandbox state, centralized architecture decision
**Prerequisite:** `docs/Production_Migration_Guide_v2.md` (full reference) + `docs/proxy-topology.md` (Phase 2)

---

## Phase 0 — Validate One Real ATM (Week 1)

> **Goal:** Confirm the entire collection chain works against real hardware before deploying anything.
> **Risk if skipped:** You build the platform but SNMP doesn't work against real ATMs.

### Step 0.1 — Get ATM access
- [ ] Contact ATM/Channel Support team for **one test ATM IP**
- [ ] Get the **SNMP community string** (v2c or v3?)
- [ ] Get the **vendor MIB or OID list** (NCR enterprise OID + GRG enterprise OID)
- [ ] Confirm the ATM has SNMP enabled (some vendors ship with it disabled)

### Step 0.2 — Test SNMP from sandbox
- [ ] Run from the sandbox host:
  ```bash
  snmpwalk -v2c -c <community> <atm-ip> .1.3.6.1.2.1.1
  ```
- [ ] Walk the vendor enterprise OID:
  ```bash
  snmpwalk -v2c -c <community> <atm-ip> .1.3.6.1.4.1
  ```
- [ ] Save the full output to `config/snmp/real_atm_snmpwalk_<vendor>.txt`
- [ ] Identify which OIDs map to our template items:
  - Cash cassette levels
  - ATM operational status
  - Card reader status
  - Printer status
  - Door sensors
  - Temperature
  - Network status
  - Transaction counters

### Step 0.3 — Build OID mapping table
- [ ] Create `config/snmp/oid_mapping.csv` with columns:
  ```
  sim_oid, real_oid, item_name, vendor, data_type
  1.1.0,    .1.3.6.1.4.1.37513.1.1.0, ATM Operational Status, NCR, INTEGER
  ```
- [ ] Validate every template item against the real OID

### Step 0.4 — Update Zabbix templates
- [ ] Clone existing templates to `Dashen Bank ATM Hardware - NCR (v2)` and `- GRG (v2)`
- [ ] Replace HTTP agent items with SNMP agent items using real OIDs
- [ ] Set SNMP community to the real value
- [ ] Test on the single ATM: host should go GREEN with real data

---

## Phase 1 — Deploy UAT VMs (Week 2)

> **Goal:** Stand up the full stack on the procured UAT hardware.

### Step 1.1 — Pre-deployment
- [ ] Confirm UAT servers are provisioned (per the revised spec — see `docs/UAT_Migration_Guide.md`):
  - UAT-01 `DBHQUATATMMONAPP` 172.26.208.176 — 4 vCPU, 8 GB RAM, 500 GB
  - UAT-02 `DBHQUATATMMONDB` 172.26.21.50 — 4 vCPU, 8 GB RAM, 200 GB
- [ ] Confirm SSH access to both VMs
- [ ] Confirm RHEL 9 subscription or repo access
- [ ] Firewall/VLAN rules handled by Cloud & Core / Network teams — verify reachability only:
  - UAT-01 (VLAN 4029) → UAT-02 (VLAN 4021) TCP 9200 (OpenSearch)
  - UAT-02 (VLAN 4021) → UAT-01 (VLAN 4029) TCP 5432 (PostgreSQL)
  - UAT-01:8080, 3000, 8082, 8888 ← your laptop access

### Step 1.2 — Deploy UAT-02 (OpenSearch + Dashboards, `DBHQUATATMMONDB`)
- [ ] SSH into UAT-02
- [ ] Install Docker (see Production_Migration_Guide §5.1.2)
- [ ] Clone the repo
- [ ] Configure sysctl: `vm.max_map_count=262144`
- [ ] Create `ej-logs/` directory
- [ ] Start OpenStack + OpenSearch Dashboards:
  ```bash
  docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d
  ```
- [ ] Verify: `curl http://localhost:9200` returns JSON

### Step 1.3 — Deploy UAT-01 (everything else, `DBHQUATATMMONAPP`)
- [ ] SSH into UAT-01
- [ ] Install Docker, clone repo
- [ ] Set up `.env` with passwords
- [ ] Start PostgreSQL first:
  ```bash
  docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d postgres
  ```
- [ ] Restore database:
  ```bash
  docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql
  ```
- [ ] Sync Zabbix templates and hosts:
  ```bash
  python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
  ```
- [ ] Start remaining services:
  ```bash
  docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d
  ```

### Step 1.4 — Verify UAT
- [ ] Zabbix web UI accessible at `http://<uat-vm1-ip>:8080`
- [ ] Grafana at `http://<uat-vm1-ip>:3000`
- [ ] Report Portal at `http://<uat-vm1-ip>:8888`
- [ ] GLPI at `http://<uat-vm1-ip>:8082`
- [ ] Simulators running and data flowing to Grafana

---

## Phase 2 — Pilot 20–50 Real ATMs (Week 3)

> **Goal:** Expand from 1 ATM to a representative sample across districts and vendors.
> **Risk if skipped:** You scale to 1,200 but discover a systemic issue (wrong OIDs for one vendor, district unreachable, etc.)

### Step 2.1 — Get pilot ATM list
- [ ] Request 20–50 ATMs from the ATM team covering:
  - Both vendors: NCR + GRG
  - Multiple districts: at least NAD, SAD, WAD, 1 regional
  - Mix of urban and rural branches
  - Mix of known-good and occasionally problematic ATMs
- [ ] Get IPs, communities, and vendor for each
- [ ] Verify the IPs in the inventory CSV match reality

### Step 2.2 — Create pilot hosts in Zabbix
- [ ] Import the pilot ATMs into Zabbix (manually or via `sync_atms_to_zabbix.py` with `--pilot` flag)
- [ ] Assign the v2 templates (with real OIDs from Phase 0)
- [ ] Set correct SNMP community per ATM

### Step 2.3 — Monitor pilot for 1 week
- [ ] Check GREEN/RED ratio daily
- [ ] Identify ATMs with SNMP timeouts
- [ ] Check for OID mismatches (items that never get data)
- [ ] Track polling duration per ATM (Zabbix internal metrics)
- [ ] Tune:
  - Polling intervals (critical 30s, normal hourly)
  - SNMP timeout values
  - Retry counts

### Step 2.4 — Fix issues
- [ ] OID mismatches → update template
- [ ] Timeout ATMs → check network path, firewall, ATM config
- [ ] Community mismatches → coordinate with ATM team
- [ ] Document all changes in `config/snmp/`

---

## Phase 3 — Deploy Production VMs (Week 3–4)

> **Goal:** Build the production environment (**3 VMs** per the revised spec) based on lessons learned from UAT pilot.

### Step 3.1 — Confirm production server readiness
- [ ] APPS-01 `DBHQPRODATMMONAPP` 172.26.18.74 — 8 vCPU, 16 GB RAM, 400 GB — provisioned?
- [ ] DATA-01 `DBHQPRODATMMONDB` 172.26.18.102 — 12 vCPU, 48 GB RAM, 3.5 TB — provisioned?
- [ ] GWY-01 `DBHQPRODATMMONGW` 172.26.18.76 — 4 vCPU, 8 GB RAM, 100 GB — provisioned?
- [ ] All VMs have RHEL 9 + Docker + SSH access?
- [ ] Firewall/VLAN rules handled by Cloud & Core / Network teams — verify inter-VLAN reachability (APPS/GWY on VLAN 4055 → DATA-01 on VLAN 4056)

### Step 3.2 — Deploy in order
- [ ] DATA-01 → PostgreSQL (with tuning from `config/postgres-production/postgresql-custom.conf`) + OpenSearch + Dashboards + Filebeat
- [ ] APPS-01 → Zabbix Server + Web (pointing to DATA-01's DB) + Grafana + GLPI + Report Portal + Renderer + Anomaly Detector + Correlator + State Manager
- [ ] GWY-01 → ISO 8583 Gateway

### Step 3.3 — Import configuration
- [ ] Import Zabbix templates (v2 with real OIDs)
- [ ] Import hosts (pilot group first, then full fleet)
- [ ] Import media types (GLPI webhook)
- [ ] Configure GLPI API token
- [ ] Configure Grafana datasources (point to APPS-01, DATA-01 — see `config/grafana/datasources-production.yml`)

---

## Phase 4 — Scale to Full Fleet (Week 4–5)

> **Goal:** All 1,200+ ATMs monitored from production.

### Step 4.1 — Import all hosts
- [ ] Run `scripts/sync_atms_to_zabbix.py --apply`
- [ ] Verify host count matches Abinet's inventory (1,202)
- [ ] Sort by district and verify proxy assignment field is set to none (centralized)

### Step 4.2 — Tune at scale
- [ ] Monitor server CPU, RAM, disk I/O
- [ ] Monitor PostgreSQL connections and query performance
- [ ] Adjust polling intervals per item type:
  - Critical (status, cash, door, temp): 30–60s
  - Normal (counters, levels): 5–15 min
  - Static (serial, firmware, vendor): 1x/day
- [ ] Adjust SNMP timeout and retry based on observed latency

### Step 4.3 — Inventory cleanup
- [ ] Compare Abinet's Excel data against actual discovered ATMs
- [ ] Flag mismatches: wrong IP, retired ATM, duplicate entry
- [ ] Coordinate with ATM team to reconcile

---

## Phase 5 — Dashboard, Alerts & Handover (Week 5–6)

> **Goal:** Operations team can use the system without developer support.

### Step 5.1 — Operational dashboard
- [ ] Deploy the ATM Operations Centre dashboard (already built in sandbox)
- [ ] Verify the following KPIs show correct data:
  - Total ATMs / Online / Offline / Warning
  - Cash low count
  - Receipt paper low count
  - Communication failures
  - District status breakdown
  - Vendor distribution (NCR vs GRG)
  - Top 10 problematic ATMs
  - Alerts in last 24 hours
  - ATM availability percentage

### Step 5.2 — Alert tuning
- [ ] Review all trigger thresholds against real ATM behaviour
- [ ] Adjust thresholds that generate false positives
- [ ] Test GLPI ticket creation from a real trigger
- [ ] Configure alert escalation paths

### Step 5.3 — Documentation & handover
- [ ] Run `scripts/setup_new_machine.sh` on a fresh VM to verify setup is reproducible
- [ ] Update README with production IPs and credentials (minus secrets)
- [ ] Create operations runbook:
  - How to check system health
  - How to add a new ATM
  - How to troubleshoot a RED host
  - How to restart services
  - Who to call for what
- [ ] Schedule handover session with operations team

---

## Phase 6 — Post-Go-Live Monitoring (Week 7+)

> **Goal:** Measure whether proxies are needed.

### Step 6.1 — Measure centralized performance
- [ ] Track average polling duration per ATM
- [ ] Track timeout rate (should be <5% on stable links)
- [ ] Track WAN bandwidth used by SNMP polling
- [ ] Track Zabbix server CPU and memory

### Step 6.2 — Decision gate: proxies needed?
| Measure | Threshold | Action |
|---------|-----------|--------|
| Poll duration | >3s average | Consider proxy for that district |
| Timeout rate | >10% | Investigate network, then consider proxy |
| Server CPU | >80% sustained | Tune intervals, then consider proxy |
| WAN bandwidth | >50 Mbps sustained | Consider proxy |

### Step 6.3 — Deploy proxies (if needed)
- [ ] Follow `docs/proxy-topology.md`
- [ ] Deploy one proxy for the worst-performing district
- [ ] Measure improvement before expanding

---

## Summary Timeline

| Week | Phase | Deliverable |
|------|-------|-------------|
| 1 | 0 | 1 real ATM validated, OIDs mapped |
| 2 | 1 | UAT VMs deployed with full stack |
| 3 | 2 | 20–50 real ATMs piloted, issues fixed |
| 3–4 | 3 | Production VMs deployed |
| 4–5 | 4 | Full fleet onboarded |
| 5–6 | 5 | Dashboards, alerts, handover |
| 7+ | 6 | Post-go-live monitoring, proxy decision |
