# BRD Compliance Matrix
### Dashen Bank — Real-Time ATM Monitoring System

| Field | Value |
|-------|-------|
| Source | `docs/BRD for ATM Monitoring system (1).md` (§§5–10 + minimum-requirements table rows 1–19) |
| Version | 1.0 |
| Date | 2026-09-05 |
| Branch | `uat-prep` |
| Status legend | ✅ built + proven · 🟡 built, needs cutover data / verification / decision · ❌ gap |

> Scope note: every SNMP-gated item depends on the two known blockers (firewall `:161`, SNMP credentials). "Proven" means verified on the UAT deploy unless stated otherwise.

---

## 1. Summary counts

| Area | ✅ | 🟡 | ❌ |
|------|----|----|----|
| §5 Functional (5.1–5.6) | 2 | 4 | 0 (+2 sub-gaps) |
| §6 Reporting (6.1–6.5) | 2 | 3 | 0 |
| §7 Non-functional (7.1–7.4) | 0 | 2 | 2 |
| §8 Integrations | 2 | 3 | 4 |
| §9 User roles | 1 | 2 | 0 |
| Table rows 1–19 (M) | 0 | 14 | 5 |

---

## 2. §5 Functional requirements

### 5.1 ATM Status Monitoring — 🟡

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Real-time online/offline status | 🟡 | `state-manager` + `status_map.json` + pessimistic defaults; UNREACHABLE honesty proven live; real data needs SNMP cutover |
| Location, Branch, District, Terminal ID, status display | 🟡 | Schema + Report Portal detail + Grafana detail dashboard built; populated for 2 test ATMs, live values pending |
| Alerts: cash-min, offline, unstable link, device failure | 🟡 | Zabbix→GLPI chain proven (drill Problem #1 round-trip); ATM-specific triggers tuned at cutover (UAT_Test_Plan TC-ALERT-01) |

### 5.2 Transaction Monitoring — ✅

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Withdrawal, balance inquiry, mini-statement, cardless, failed | ✅ | ISO 8583 gateway (simulation) streaming 3,600+ rows for both test ATMs |
| Time, Terminal ID, type, status, error codes | ✅ | Columns present; Grafana transaction dashboard shows live rows |
| Reversed / timed-out outcomes | 🟡 | Verify status values against real switch codes at cutover |

### 5.3 Cash Level Monitoring — 🟡 + 1 ❌

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Real-time cassette levels | 🟡 | Built, SNMP-gated; per-ATM capacity overrides in `config/cassette_capacity.json` |
| Cash-low / out-of-cash alerts | 🟡 | State-driven (OUT_OF_CASH); proven chain, live tuning at cutover |
| Cash-depletion forecast | ❌ | No forecasting exists. Build post-cutover: per-ATM burn rate → days-of-cash. Owner: TBD |

### 5.4 ATM Error Monitoring — 🟡

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Dispenser / card reader / printer / network / software errors + auto tickets | 🟡 | 38 NCR + 30 GRG triggers live in Zabbix (tags, nodata silence-catcher, uuid-stable regen via `build_zabbix_triggers.py`); 8+4 thresholds flagged verify-at-walk. Four severity actions route to GLPI with recovery ops — full lifecycle drill passed |

### 5.5 Alert and Notification System — 🟡

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Email notifications | 🟡 | Zabbix email media exists; needs bank SMTP relay |
| SMS alerts | ❌ | Only an empty `AT_API_KEY` placeholder. Owner: TBD (build sender post-cutover) |
| Dashboard notifications | ✅ | Grafana + portal live |
| Triggers: offline, cash-out, hardware, suspicious pattern | 🟡 | Anomaly detector live; fault triggers at cutover |

### 5.6 Real-Time Dashboard — ✅

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Totals, online/offline counts, low cash, totals, failed | ✅ | 7 dashboards imported in `ATM Monitoring` folder; frames verified |
| Filters: district, branch, location, status | ✅ | Dashboard variables present |

---

## 3. §6 Reporting — 🟡 (verify each at cutover)

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| 6.1 Availability (uptime %, downtime, reasons; daily/weekly/monthly) | ✅ | Rewritten off `atm_state_history` (was transaction-activity proxy — wrong metric); verified live: 0.0/168.0/0.00%/UNREACHABLE on real history. PDF/Excel/CSV paths checked |
| 6.2 Transaction summary (totals, success rate, failed, value) | 🟡 | Queries exist in dashboards; same verification needed |
| 6.3 Cash report (level, dispensed, replenishment history) | ✅ | Live on UAT: Cash Loads page (form + history + audit, AUTO/MANUAL source), verified submit→DB→list; test row cleaned |
| 6.4 Error/incident (errors, resolution time, frequency) | 🟡 | GLPI holds the data; portal-side resolution-time view to verify |
| 6.5 Performance (most/least used, peak hours) | 🟡 | Countable from `atm_transactions`; verify views |
| PDF / Excel / CSV, scheduled + on-demand | ✅ | Portal export + scheduler ticking (observed) |

---

## 4. §7 Non-functional requirements

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| 7.1 System availability 99.9% | ❌ | Single-host deploy, no HA/DR design. Backups proven (`backup_db.sh` drill). Production HA decision required before go-live. Owner: IT + architect |
| 7.2 Data updates within 5 seconds | ❌ | SNMP polls at 30s by design; EJ path is seconds. Propose tiered SLA (30s state / seconds EJ) — 5s fleet SNMP is unrealistic. Owner: negotiate with bank |
| 7.3 RBAC | 🟡 | Portal roles (admin/viewer) + native Zabbix/GLPI/Grafana RBAC; end-to-end matrix unmapped |
| 7.3 Audit logging | ✅ | `audit_log` table, 770+ rows observed |
| 7.3 Data encryption | ❌ | No TLS on UIs/DB, no at-rest encryption. Must add (reverse-proxy TLS, PG SSL, volume encryption) before production. Owner: IT + architect |
| 7.4 Expansion of ATM network | ✅ | Bulk import scripts (1,202-ATM path), DB-driven inventory |
| 7.4 Integration with additional tools | 🟡 | SNMP / ISO 8583 / REST in place; NDC/DDC absent (see §8) |

---

## 5. §8 Integration requirements

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| ATM Switch | 🟡 | Gateway simulation live; real TCP mode built, bank feed pending |
| Core Banking System (read-only) | ❌ | No connector. Confirm whether required for UAT or production-only. Owner: bank |
| Card Management System | ❌ | Same as above |
| SMS / Email notification systems | 🟡 | Email pending relay; SMS missing (see 5.5) |
| ISO 8583 | ✅ | Gateway parses + stores; simulated feed live |
| SNMP | ✅ | Polling, v2c/v3, trap-ready design; credentials pending |
| NDC / DDC | ❌ | Not implemented. Confirm scope (needed only if switch feed is NDC/DDC rather than ISO 8583) |
| REST / SOAP APIs | 🟡 | GLPI + Zabbix APIs consumed; no public API of our own documented |

---

## 6. §9 User roles

| Requirement | Status | Evidence / next step |
|-------------|--------|----------------------|
| Admin: users, alerts, system config | 🟡 | Portal user management exists; alert config is Zabbix-side (manual); verify each admin flow |
| Operations: monitor, report, respond | 🟡 | Portal + Grafana + GLPI all live; formal walkthrough at UAT |
| Management: dashboards, performance reports | ✅ | Verified visible via tunnel |

---

## 7. Minimum-requirements table (rows 1–19, all M)

| # | Requirement | Status | Evidence / next step |
|---|-------------|--------|----------------------|
| 1 | Health & status (In/Out-of-Service, Partial, Supervisor, agent-disconnected, app failures, polling intervals, history) | 🟡 | States incl. IN_SUPERVISOR in dashboards; UNREACHABLE proven; polling configurable; history in `atm_current_state`. App-failure/reboot detection via EJ pending live data |
| 2 | Cash dispenser (cassette levels, thresholds, reject bin, jam, partial dispense) | 🟡 | Levels + thresholds built; reject bin / jam / partial-dispense depend on OID mapping + EJ at cutover |
| 3 | Card handling (reader health, capture, shutter) | 🟡 | Health in status map; capture/shutter via EJ + fault codes at cutover |
| 4 | Printers (paper, journal, faults) | 🟡 | SNMP-gated; fault classes mapped |
| 5 | Security & sensors (doors, temp/humidity, vibration/intrusion) | 🟡 | Door states mapped; sensor OIDs confirmed at walk time |
| 6 | Transactional monitoring (ISO 8583, outcomes, failure categories, anomaly, masked-PAN search, correlation) | 🟡 | Sim feed live; masked search + anomaly proven; reversed/timed-out + category coverage verified at cutover |
| 7 | Network monitoring (link/latency/loss/jitter, MPLS/VSAT/GSM/Fiber, alerts, correlation) | 🟡 | network-correlator live on self-host; link-type agnostic (IP-level); ATM link data at cutover |
| 8 | EJ management (collect, multi-vendor, search, correlate, dispute, export/retention) | 🟡 | Transport + NCR/GRG grok + portal masked search proven with fixtures (5/5 parsed, 3/3 raw retained); EJ index pattern + OSD import done. Retention: 90d ISM policy live + PG daily sweep (periods TBD with Risk/Compliance). Raw-vendor multiline normalization: confirm at cutover whether ATMs send pre-normalized lines |
| 9 | Camera & surveillance | ❌ | Unbuilt: no camera polling, snapshots, tampering alerts, or balance-check capture. Needs scope/Phase-2 decision + OID confirmation. Owner: bank + team |
| 10 | Power & environmental (mains/UPS/battery, correlation) | ❌ | Unbuilt. Same decision path as row 9 |
| 11 | Alert management (all triggers; email/SMS/push; severity/escalation; ack/suppress) | 🟡 | Four severity actions (Disaster/High/Average/Warning) route ATMs-group events to GLPI with recovery ops — full PROBLEM→solve lifecycle drill passed; escalation via GLPI SLAs. Email needs bank relay; SMS/push missing |
| 12 | Incident/ticket/SLA (auto+manual, lifecycle, assign, SLA, RCA, attachments) | 🟡 | Auto-create + lifecycle + assign + SLAs proven live; **RCA**: `ATM Fault RCA` solution template in GLPI (root-cause/corrective/preventive/verified-by) + close-with-RCA process. Native enforcement still impossible — process control |
| 13 | Mobile app (Android mandatory) | ❌ | Nothing exists. Scope decision required (build / waive / Phase 2); portal is mobile-browser-usable as mitigation. Owner: bank |
| 14 | Dashboards (operational, KPI, geo-map, drill-down, widgets/filters) | 🟡 | 7 dashboards live; geo-map pins verified for both test ATMs (4 Kilo coords seeded); EJ datasource green since :9200 opened |
| 15 | Txn reporting & analytics (uptime, downtime, SLA, vendor, success/failure, cash-out, export, scheduled) | 🟡 | All 7 reports executed against live sim data and cross-checked (txn↔cash↔vendor agree to the birr); PDFs render; Complete report gained §6 Availability |
| 16 | Integrations (switch, CBS, vendor tools, NMS, ISO/SNMP/NDC/REST) | 🟡 | See §8 |
| 17 | Security & compliance (RBAC, audit, encryption, PCI, regulatory, retention) | 🟡 | Audit ✅ + masking ✅ (last-4 proven); encryption ❌; PCI scope + retention mapping pending with Risk/Compliance |
| 18 | Non-functional (99.9%, HA, DR, scalable, web) | 🟡 | Scalable ✅ + web ✅ + backups ✅; HA/DR ❌ (see 7.1) |
| 19 | Vendor responsibilities (architecture, rollout, testing, training, support) | 🟡 | Docs + pilot checklist + UAT in progress; training sessions + support SLA unstarted (normal for this stage) |

---

## 8. Decisions required (bank / stakeholders)

| # | Decision | Why | Proposed owner |
|---|----------|-----|----------------|
| D1 | Mobile app: build, waive, or Phase 2? | M-row 13, nothing exists | Bank management |
| D2 | Cameras + Power/UPS: in scope or Phase 2? | M-rows 9–10, unbuilt | Bank management + Channel Support |
| D3 | 5-second update SLA: accept tiered (30s state / seconds EJ)? | Current design polls SNMP at 30s | Bank + architect |
| D4 | Production HA + encryption design | M-rows 17–18, UAT is single-host | IT + architect |
| D5 | CBS / Card-system / NDC scope for UAT vs production | §8 + row 16 gaps | Bank + Channel Support |
| D6 | SMS provider + cash forecast + replenishment tracking | Small builds, schedule post-cutover | Team |

## 9. Traceability

- UAT execution: `docs/UAT_Test_Plan.md` (§7 test cases, §8 requirements traceability)
- Cutover procedure: `docs/UAT_Phase0_Runbook.md`
- Deployment: `docs/UAT_Migration_Guide.md`
- RFP compliance artifact: this document (also satisfies BRD §19 proposal-compliance needs)
