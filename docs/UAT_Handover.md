# UAT Handover Document — ATM Monitoring System (2 Test ATMs)

> Self-contained handover for the Dashen Bank real-time ATM monitoring UAT.
> Everything the bank needs to run, evaluate, accept, and operate the UAT is
> in this file. Fill every TBD before sign-off.

## 1. Document control

| Field | Value |
|-------|-------|
| Version | 1.0 (draft — TBDs open) |
| Build under test | TBD (branch `uat-prep`, commit hash recorded here on cutover day) |
| UAT window | TBD – TBD |
| Authors | Monitoring Engineer (build) · UAT Lead, Bank (acceptance) |

## 2. Purpose

Prove the platform monitors real ATMs end-to-end — live state, faults, cash
levels, EJ ingestion, dashboards, and alerting — using the bank's two
designated test terminals, with no simulators running.

## 3. Scope

**In scope:** real SNMP polling of both test ATMs; state detection
(In-Service, Out-of-Service, Offline, Fault, Out-of-Cash, Unknown,
Unreachable); EJ ingestion and dispute search; live Grafana dashboards and
Report Portal detail views; fault → Zabbix problem → GLPI ticket flow;
transaction/cash/error/performance reports for the 2 ATMs.

**Out of scope:** full-fleet rollout; simulator-based testing (simulators are
disabled in UAT); real ISO 8583 transaction feed unless the bank routes
traffic to it; mobile app; cameras/power monitoring; production HA and
encryption (see §12 decisions).

## 4. Environment

| Role | Host | IP | Access |
|------|------|----|--------|
| App / monitoring server (UAT-01) | Postgres, Zabbix, Grafana, GLPI, Report Portal, state-manager, Filebeat | `172.22.10.45` | Zabbix `:8080`, Grafana `:3000`, GLPI `:8082`, Portal `:8888` |
| Data server (UAT-02) | OpenSearch, OpenSearch Dashboards, ISO 8583 gateway | `172.22.10.60` | Dashboards `:5601`, API `:9200`, gateway `:9876` |
| Test ATM — NCR | `ATM-TEST-NCR` / terminal `TID-UAT-NCR` | `172.23.248.4` | SNMP UDP 161 |
| Test ATM — GRG | `ATM-TEST-GRG` / terminal `TID-UAT-GRG` | `172.23.248.6` | SNMP UDP 161 |

Entry firewall state: UAT-01 → test ATMs UDP 161; test ATMs → UAT-01 UDP+TCP
514 (EJ syslog); UAT-01 ↔ UAT-02 TCP 9200 (Filebeat/OpenSearch) and TCP 5432
(gateway → Postgres); switch → UAT-02 TCP 9876 only if ISO 8583 is in scope.

**Credentials:** handed over sealed (server `.env` values for Zabbix, Grafana,
Portal, Postgres; GLPI admin; app API tokens). Never committed to git, never
sent by email or chat. OpenSearch Dashboards has no login in UAT (security
plugin disabled).

## 5. Roles & contacts

| Activity | UAT Lead (Bank) | Monitoring Eng | IT / Network | Project Owner |
|----------|:---:|:---:|:---:|:---:|
| Firewall/network + VM provisioning | C | C | Accountable | Informed |
| SNMP credentials (v2c/v3) | Accountable | C | C | Informed |
| Stack deploy + cutover (§8) | C | Accountable | Informed | Informed |
| Test execution (§6) | Accountable | Responsible | Informed | Informed |
| Defect fix | C | Accountable | C | Informed |
| Sign-off (§13) | Accountable | C | Informed | C |

Names: TBD (fill before testing starts).

## 6. Test cases (mark Pass/Fail per row during execution)

Critical (✅) rows must all pass for sign-off. Conditional (⬜) rows are
recorded but do not block.

### 6.1 Infrastructure & network

| ID | Test | How | Expected | Crit | Result |
|----|------|-----|----------|------|--------|
| TC-INF-01 | All containers Up | `docker ps` on both VMs | Healthy, none `Restarting` | ✅ | TBD |
| TC-INF-02 | UIs reachable | Open the 5 URLs in §4 | All respond | ✅ | TBD |
| TC-NET-01 | SNMP reachability | `snmpwalk -v2c -c <community> -t 5 -r 2 -On <atm-ip> .1.3.6.1.2.1.1` from UAT-01, both ATMs | Values returned | ✅ | TBD |
| TC-NET-02 | EJ listener | `ss -lun` on UAT-01 | `:514` held by Filebeat | ✅ | TBD |
| TC-NET-03 | ISO 8583 path (if in scope) | `nc -zv 172.22.10.60 9876` | Open | ⬜ | TBD |

### 6.2 SNMP capture & template remap

| ID | Test | How | Expected | Crit | Result |
|----|------|-----|----------|------|--------|
| TC-SNMP-01 | Walks captured (both vendors) | `./config/snmp/capture_walk.sh NCR <ip> <community>` and GRG equivalent | `real_atm_snmpwalk_NCR.txt` / `_GRG.txt` saved with OID rows | ✅ | TBD |
| TC-MAP-01 | OID mapping complete | Fill `config/snmp/oid_mapping.csv` (`sim_oid,real_oid,item_name,vendor,data_type`) | Every monitored simulator OID has a real counterpart | ✅ | TBD |
| TC-TMPL-01 | Generator guardrail passes | `python3 scripts/generate_zabbix_templates.py --dry-run`, then real run | Exit 0 (aborts exit 1 if any OID unmapped) | ✅ | TBD |
| TC-TMPL-02 | Templates remapped & committed | Rewrite + `git commit` on `uat-prep` | No item left on simulator root `1.3.6.1.4.1.99999.*` | ✅ | TBD |

### 6.3 Zabbix registration & collection

| ID | Test | How | Expected | Crit | Result |
|----|------|-----|----------|------|--------|
| TC-ZBX-01 | Hosts registered | `python3 scripts/sync_atms_to_zabbix.py --apply --import-templates` | `ATM-TEST-NCR`, `ATM-TEST-GRG` exist with SNMP `:161` | ✅ | TBD |
| TC-ZBX-02 | Items populate | Monitoring → Latest data, both hosts | Live values, not "Not supported" | ✅ | TBD |
| TC-STATE-01 | State computed correctly | `SELECT atm_id, state FROM atm_current_state;` | Real condition per status map | ✅ | TBD |
| TC-STATE-02 | No false In-Service | Same query with an ATM unreachable | `UNREACHABLE`/`UNKNOWN`, never false `IN_SERVICE` | ✅ | TBD |

### 6.4 EJ ingestion & Report Portal

| ID | Test | How | Expected | Crit | Result |
|----|------|-----|----------|------|--------|
| TC-EJ-01 | EJ lands in OpenSearch | Trigger test withdrawals; check index `atm-ej-live-*` | Real lines indexed | ✅ | TBD |
| TC-EJ-02 | Parser extracts fields | Same lines in Discover / portal EJ Search | Structured fields; unparsed kept as `ej_parsed:false` | ⬜ | TBD |
| TC-PORTAL-01 | Live detail views | Portal ATM detail for both ATMs | Cassettes/hardware/state live, no blanks | ✅ | TBD |
| TC-GRAF-01 | Dashboards live | Ops Centre + drill-down | Live state + transactions, both ATMs | ✅ | TBD |

### 6.5 Alerting & rollback

| ID | Test | How | Expected | Crit | Result |
|----|------|-----|----------|------|--------|
| TC-ALERT-01 | Fault → ticket | Induce a fault (e.g. low cash) | Zabbix problem → GLPI ticket created | ✅ | TBD |
| TC-ISO-01 | Transactions recorded (conditional) | With real ISO traffic | `atm_transactions` grows (`ISO8583_REAL`) | ⬜ | TBD |
| TC-ROLL-01 | Remap reversible | Restore `*.simulator.bak` + re-import | Hosts return to last-good templates | ✅ | TBD |

## 7. Requirements traceability

| Req | Business requirement | Tests |
|-----|----------------------|-------|
| R1 | Monitor real ATMs without simulators | TC-INF-01, TC-STATE-02 |
| R2 | Correct live ATM state detection | TC-STATE-01, TC-STATE-02 |
| R3 | Cash / cassette levels visible | TC-ZBX-02, TC-PORTAL-01 |
| R4 | EJ ingested and searchable | TC-EJ-01, TC-EJ-02 |
| R5 | Live operational dashboards | TC-GRAF-01 |
| R6 | Fault raises a ticket | TC-ALERT-01 |
| R7 | SNMP templates use real OIDs | TC-MAP-01, TC-TMPL-01, TC-TMPL-02 |
| R8 | Safe rollback of template changes | TC-ROLL-01 |
| R9 | ISO 8583 transactions (optional) | TC-ISO-01 |
| R10 | Network / firewall correctly opened | TC-NET-01, TC-NET-02, TC-NET-03 |

## 8. Cutover record (fill during execution)

1. Inventory seeded — `SELECT atm_id, vendor, ip_address, status FROM atm_locations;` shows both test ATMs. Date/TBD.
2. Real community/version set — `UPDATE atm_locations SET snmp_community='<…>', snmp_version=2 WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');` (v3: version + env secrets instead). Date/TBD.
3. Walks captured (§6.2 TC-SNMP-01). Date/TBD.
4. Mapping completed and generator exit 0; templates committed to `uat-prep`. Date/TBD.
5. Hosts imported; `state-manager` image rebuilt before sync. Date/TBD.
6. Collection verified (§6.3–6.4). Date/TBD.
7. EJ parser tuned from one real line per vendor. Date/TBD.

## 9. Defect management

Log every failure: Defect ID, linked Test ID, description, severity, owner,
status (Open / In-progress / Fixed / Waived), target date. Critical defects go
immediately to Monitoring Engineer + Project Owner and are reviewed daily.
Severities: Critical (blocks acceptance), Major (broken with workaround),
Minor (cosmetic), Conditional (⬜-linked, never blocks).

## 10. Operations handover

- **Health:** `docker ps` (all Up) · Zabbix/Grafana/GLPI/Portal/Dashboards URLs respond · `ss -lun` shows `:514` · OpenSearch `:9200` reachable from UAT-01.
- **Add an ATM:** portal `/admin/atm` form (IP, SNMP community/version, EJ format included; blanks keep existing on edit) or CSV import → `sync_atms_to_zabbix.py --apply` → verify Latest data → confirm EJ + transactions → check dashboards + one filtered report.
- **RED host triage:** Latest data "Not supported" → firewall UDP 161, then community string, then template OID; `UNREACHABLE` state with empty polls is honest output, not a bug.
- **Restarts:** `docker compose up -d <service>`; portal picks up `.env` on recreate; Grafana admin password resets via `grafana-cli`, not `.env`.
- **Backups:** DB dump before any direct SQL (`pg_dump`); template `*.simulator.bak` before every remap.
- **Contacts / walkthrough session:** TBD.

## 11. Assumptions & risks

Assumptions: both test ATMs available and reachable; SNMP credentials
provided; simulators stay off; ISO 8583 optional. Risks: unknown SNMP version
(both v2c/v3 supported); incomplete OID mapping (generator blocks cutover
until complete); vendor status codes differ (tunable in `status_map.json` —
re-render triggers, re-import); EJ format surprises (raw retained, grok tuned);
firewall/VM delays (requested early).

## 12. Out-of-scope decisions (owners + dates, not build items)

D1 Mobile app: build / waive / Phase 2 · D2 Cameras + Power/UPS: in scope or
Phase 2 · D3 5-second SLA: accept tiered (30s state / seconds EJ) · D4
Production HA + encryption · D5 CBS / Card-system / NDC scope · D6 SMS
provider + cash forecast · D7 Directory cutover + TLS + SSO path. Owner/date
per item: TBD.

## 13. Acceptance & sign-off

Accepted when **all Critical (✅) tests pass**, non-critical gaps carry owner +
date, cutover artifacts (§8 items 3–4) are committed, no unexplained
`UNREACHABLE` remains, and all Critical defects are closed or waived.

| Role | Name | Date | Decision (Accept / Reject) |
|------|------|------|----------------------------|
| UAT Lead (Bank) | TBD | TBD | TBD |
| Monitoring Engineer | TBD | TBD | TBD |
| IT / Network | TBD | TBD | TBD |
| Project Owner | TBD | TBD | TBD |
