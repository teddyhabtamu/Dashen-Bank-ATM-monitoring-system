# UAT Test Plan & Acceptance Criteria
### Dashen Bank — Real-Time ATM Monitoring System (UAT)

---

## 0. Document Control

| Field | Value |
|-------|-------|
| Document title | UAT Test Plan & Acceptance Criteria — Dashen Bank ATM Monitoring System |
| Version | 1.0 |
| Date | 2026-08-29 |
| Author | Monitoring Engineering |
| Status | Draft — pending UAT execution |
| Source branch | `uat-prep` |
| Approver | UAT Lead (Bank) |

> Change history: v1.0 — initial UAT test plan for Phase 0 (two real test ATMs).

---

## 1. Purpose & Audience
This document defines *what "working" means* for the User Acceptance Test (UAT) of the ATM
Monitoring System against **two real test ATMs** (no simulators). It is written for two readers:

- **Bank UAT / QA team & stakeholders** — read §3 (Overview), §4 (Scope), §10 (Acceptance), and §13 (Sign-off).
- **Engineering / DevOps** — use the Test Cases (§7) as the verification checklist; execution steps link to the detailed runbooks.

It does **not** repeat step-by-step procedures — those live in the linked runbooks.

---

## 2. Glossary

| Term | Meaning |
|------|---------|
| ATM | Automated Teller Machine |
| NCR / GRG | ATM hardware vendors in scope (NCR Corp., GRG Banking Equipment) |
| EJ | Electronic Journal — the ATM's transaction/event log stream |
| SNMP | Simple Network Management Protocol — used to poll ATM metrics (UDP 161) |
| Zabbix | Monitoring platform that stores ATM metrics as items |
| GLPI | IT service / ticket management system (fault tickets) |
| OSD | OpenSearch Dashboards — EJ search & visualization UI |
| UAT-01 / UAT-02 | The two UAT servers: app/monitoring and data respectively |
| state-manager | Service that computes per-ATM state from Zabbix item values |
| ISO 8583 | Financial transaction messaging standard (optional in UAT) |

---

## 3. UAT Overview (executive summary)
**Objective:** prove the platform monitors real ATMs end-to-end — live state, faults, cash levels,
EJ ingestion, and alerting — using the bank's two designated test terminals.

**What is validated**
- Real SNMP polling of the two test ATMs (no simulator fallback in UAT).
- Correct ATM state detection (In-Service, Out-of-Service, Offline, Fault, Out-of-Cash).
- EJ log ingestion into OpenSearch and the Report Portal EJ Search.
- Live dashboards (Grafana) and the Report Portal detail views.
- Fault → Zabbix problem → GLPI ticket flow.

**Environment at a glance**

| Role | Host | IP |
|------|------|-----|
| App / monitoring server (UAT-01, `dbtcatmmonappuat`) | Postgres, Zabbix, Grafana, GLPI, Report Portal, state-manager, filebeat | `172.22.10.45` |
| Data server (UAT-02, `dbtcatmmondbuat`) | OpenSearch, OpenSearch Dashboards, ISO 8583 gateway | `172.22.10.60` |
| Test ATM — NCR | NCR | `172.23.248.4` |
| Test ATM — GRG | GRG | `172.23.248.6` |

**Phasing:** this plan covers **Phase 0** (validate the two real ATMs). The broader 45-day
production-readiness rollout is described in [`uat-pilot-checklist.md`](uat-pilot-checklist.md).

---

## 4. Scope & Prerequisites

### In scope
Real SNMP polling, state detection, EJ ingestion, Report Portal live views, Grafana dashboards,
and alert→ticket flow for the two test ATMs.

### Out of scope
Full-fleet rollout, simulator-based testing (simulators are disabled in UAT via `ENABLE_SIMULATORS=false`),
and ISO 8583 transaction monitoring **unless** the bank routes real traffic (see TC-ISO-01, conditional).

### Prerequisites / entry criteria
1. **Infrastructure** deployed per [`UAT_Migration_Guide.md`](UAT_Migration_Guide.md) (both VMs up, all containers healthy).
2. **Network / firewall openings** requested from IT — see [`UAT_Phase0_Runbook.md §0`](UAT_Phase0_Runbook.md):
   - UAT-01 → test ATMs on **UDP 161** (SNMP)
   - Test ATMs → UAT-01 **UDP/TCP 514** (EJ / filebeat)
   - ISO 8583 switch → UAT-02 **TCP 9876** (only if ISO 8583 is in scope)
3. **SNMP credentials** obtained from the ATM / Channel Support team: version (v2c vs v3) and
   community (v2c) or user/auth/priv (v3). Recorded in `atm_locations` (Runbook §2).
4. **Test ATMs seeded** into `atm_locations` (Runbook §1).

---

## 5. Environment reference

| Component | Host / IP | Role in UAT | Verify via |
|-----------|-----------|-------------|------------|
| PostgreSQL + Zabbix | UAT-01 `172.22.10.45` | Inventory + metrics store | Zabbix UI `:8080` |
| Grafana | UAT-01 `:3000` | Live dashboards | Grafana UI |
| GLPI | UAT-01 `:8082` | Ticket system | GLPI UI |
| Report Portal | UAT-01 `:8888` | ATM detail / EJ search | Portal UI |
| OpenSearch + Dashboards | UAT-02 `172.22.10.60:9200 / :5601` | EJ storage + search | OSD UI |
| filebeat | UAT-01 | Forwards ATM EJ (`:514`) → OpenSearch | `ss -lun :514` |
| state-manager | UAT-01 | Computes `atm_current_state` from Zabbix | `atm_current_state` table |
| Test ATM NCR | `172.23.248.4` | SNMP source | `snmpwalk` |
| Test ATM GRG | `172.23.248.6` | SNMP source | `snmpwalk` |

---

## 6. Roles & Responsibilities (RACI)

| Activity | UAT Lead (Bank) | Monitoring Eng | IT / Network | Project Owner |
|----------|:---:|:---:|:---:|:---:|
| Provision UAT VMs + firewall/network | C | C | **A** | I |
| Provide SNMP credentials (v2c/v3) | **A** | C | C | I |
| Deploy stack ([`UAT_Migration_Guide.md`](UAT_Migration_Guide.md)) | C | **A** | C | I |
| Execute cutover (Runbook §2–§6) | C | **A** | I | I |
| Execute test cases (§7) | **A** | R | I | I |
| Fix defects | C | **A** | C | I |
| Log & triage defects | **A** | R | I | I |
| Approve sign-off (§13) | **A** | C | I | C |

*A = Accountable, R = Responsible, C = Consulted, I = Informed.*

---

## 7. UAT Test Cases

> **Steps** columns point to [`UAT_Phase0_Runbook.md`](UAT_Phase0_Runbook.md). Mark **Pass/Fail** per test.
> **Critical** tests (✅) must all pass for sign-off.

### 7.1 Infrastructure & network
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-INF-01 | Infra | All containers `Up` | Migration Guide §D.5 | `docker ps` shows services healthy (no `Restarting`) | ✅ |
| TC-INF-02 | Infra | UIs reachable | Migration Guide §F | Zabbix `:8080`, Grafana `:3000`, GLPI `:8082`, Portal `:8888`, OSD `:5601` respond | ✅ |
| TC-NET-01 | Net | SNMP reachability | Runbook §0, §3 | UAT-01 can `snmpwalk` both ATMs on UDP 161 | ✅ |
| TC-NET-02 | Net | EJ listener | Runbook §0 | `ss -lun` shows `:514` on UAT-01 (filebeat) | ✅ |
| TC-NET-03 | Net | ISO 8583 path *(if in scope)* | Runbook §9 | Switch → UAT-02 `:9876` reachable | ⬜ |

### 7.2 SNMP capture & template remap
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-SNMP-01 | SNMP | Walk captured (both) | Runbook §3 | `config/snmp/real_atm_snmpwalk_NCR.txt` / `_GRG.txt` saved with OID rows | ✅ |
| TC-MAP-01 | Mapping | `oid_mapping.csv` complete | Runbook §4 | Every monitored `sim_oid` has a real `real_oid` (no gaps for the items we poll) | ✅ |
| TC-TMPL-01 | Template | Generator guardrail passes | Runbook §5 | `generate_zabbix_templates.py` exits **0** (aborts with exit 1 if any OID unmapped) | ✅ |
| TC-TMPL-02 | Template | Templates remapped & committed | Runbook §5–§6 | XMLs rewritten off simulator root `1.3.6.1.4.1.99999.*`; committed to `uat-prep` | ✅ |

### 7.3 Zabbix registration & collection
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-ZBX-01 | Zabbix | Hosts registered | Runbook §6 | Hosts `ATM-TEST-NCR`, `ATM-TEST-GRG` exist in Zabbix with SNMP interface `:161` | ✅ |
| TC-ZBX-02 | Zabbix | Items populate | Runbook §7 | **Monitoring → Latest data** shows live values (not "Not supported") for both ATMs | ✅ |
| TC-STATE-01 | State | State computed correctly | Runbook §7 | `atm_current_state` reflects real condition per `config/status_map.json` (In-Service / Out-of-Service / Offline / Hardware-Fault / Out-of-Cash / Unknown / Unreachable) | ✅ |
| TC-STATE-02 | State | No false "In-Service" | Runbook §7 | An unreachable/polled-empty ATM shows `UNREACHABLE`/`UNKNOWN`, **never** falsely `IN_SERVICE` | ✅ |

### 7.4 EJ ingestion & Report Portal
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-EJ-01 | EJ | EJ lands in OpenSearch | Runbook §8 | Real EJ lines indexed in `atm-ej-live-*` / `atm-electronic-journal` | ✅ |
| TC-EJ-02 | EJ | Parser extracts fields | Runbook §8 | For NCR & GRG formats, grok yields structured fields; unparsed lines retained with `ej_parsed:false` | ⬜ |
| TC-PORTAL-01 | Portal | Live detail views | Runbook §7 | Report Portal ATM detail shows live cassettes / hardware / state from Zabbix (no blanks) | ✅ |
| TC-GRAF-01 | Grafana | Dashboards live | Runbook §10 | Grafana shows live state + transactions for both ATMs | ✅ |

### 7.5 Alerting & rollback
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-ALERT-01 | Alert | Fault → ticket | Runbook §10 | Induced fault → Zabbix problem → GLPI ticket created | ✅ |
| TC-ISO-01 | ISO 8583 *(conditional)* | Transactions recorded | Runbook §9 | With real ISO traffic, `atm_transactions` grows (source `ISO8583`) | ⬜ |
| TC-ROLL-01 | Rollback | Remap reversible | Runbook §Rollback | Restore `*.simulator.bak` + re-import returns ATMs to last-good templates | ✅ |

---

## 8. Requirements → Test Traceability

| Req ID | Business requirement | Test(s) |
|--------|----------------------|---------|
| R1 | Monitor real ATMs without simulators | TC-INF-01, TC-STATE-02 |
| R2 | Correct live ATM state detection | TC-STATE-01, TC-STATE-02 |
| R3 | Cash / cassette levels visible | TC-ZBX-02, TC-PORTAL-01 |
| R4 | EJ ingested and searchable | TC-EJ-01, TC-EJ-02 |
| R5 | Live operational dashboards | TC-GRAF-01 |
| R6 | Fault raises a ticket | TC-ALERT-01 |
| R7 | SNMP templates use real OIDs | TC-MAP-01, TC-TMPL-01, TC-TMPL-02 |
| R8 | Safe rollback of template changes | TC-ROLL-01 |
| R9 | ISO 8583 transactions *(optional)* | TC-ISO-01 |
| R10 | Network / firewall correctly opened | TC-NET-01, TC-NET-02, TC-NET-03 |

---

## 9. Defect / Issue Logging & Escalation

**Severity definitions**
- **Critical** — blocks acceptance (e.g., TC-STATE-02 false In-Service, TC-ZBX-02 no data). Must be fixed or formally waived.
- **Major** — core feature broken but has a workaround.
- **Minor** — cosmetic / non-blocking.
- **Optional** — relates to a ⬜ (conditional) test; logged but does not block sign-off.

**Logging:** every failure is recorded with: Defect ID, linked Test ID, description, severity,
owner, status (Open / In-progress / Fixed / Waived), and target resolution date.

**Escalation:** Critical defects are raised immediately to the Monitoring Engineer and Project
Owner; status reviewed in the daily UAT stand-up. Acceptance (§10) requires all Critical
defects **closed or waived in writing by the UAT Lead**.

---

## 10. Acceptance Criteria / Definition of Done
UAT is accepted when **all Critical (✅) tests pass** and:
- Any non-critical (⬜) gap is documented with an owner and a follow-up date.
- `oid_mapping.csv`, `status_map.json`, and the remapped templates are committed to `uat-prep`.
- No open `UNREACHABLE`/`UNKNOWN` state that cannot be explained by a real ATM condition.
- All Critical defects closed or waived (§9).

---

## 11. Assumptions & Risks

**Assumptions**
- Two test ATMs are available and network-reachable from UAT-01.
- SNMP credentials (version + community / v3) will be provided by the bank.
- Simulators remain disabled in UAT (`ENABLE_SIMULATORS=false`).
- UAT topology mirrors the production topology (UAT-01 app, UAT-02 data).
- ISO 8583 is optional unless the bank routes real traffic.

**Risks & mitigations**
| Risk | Impact | Mitigation |
|------|--------|------------|
| SNMP version unknown (v2c vs v3) | Cutover commands differ | v3 + v2c both supported; Runbook §2/§3 cover both |
| Real OID mapping incomplete | Items stay "Not supported" | Generator guardrail (TC-TMPL-01) blocks cutover until complete |
| Vendor status/fault enum differs from assumed codes | Wrong state shown | Codes externalized in `config/status_map.json`, tunable during UAT |
| EJ grok needs per-vendor tuning | EJ unparsed | Raw retained (`ej_parsed:false`); grok tuned in Runbook §8 |
| Firewall / VM provisioning delay | UAT blocked | IT-ask raised early (Runbook §0) — current blocker |

---

## 12. Rollback & Support
- **Template rollback:** `cp config/zabbix/template_*.xml.simulator.bak config/zabbix/template_*.xml` then re-run import (Runbook §Rollback).
- **General troubleshooting:** [`troubleshooting.md`](troubleshooting.md).
- **Real-ATM how-to (SNMP/community/terminal ID):** [`REAL_ATM_INTEGRATION_GUIDE.md`](REAL_ATM_INTEGRATION_GUIDE.md).

---

## 13. Sign-off

| Role | Name | Date | Decision (Accept / Reject) |
|------|------|------|----------------------------|
| UAT Lead (Bank) | | | |
| Monitoring Engineer | | | |
| IT / Network | | | |
| Project Owner | | | |
