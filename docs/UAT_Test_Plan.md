# UAT Test Plan & Acceptance Criteria
### Dashen Bank — Real-Time ATM Monitoring System (UAT)

**Branch:** `uat-prep`  ·  **Status:** ready to execute once IT provisions the 2 UAT VMs + SNMP credentials

---

## 1. Purpose & Audience
This document defines *what "working" means* for the User Acceptance Test (UAT) of the ATM
Monitoring System against **two real test ATMs** (no simulators). It is written for two readers:

- **Bank UAT / QA team & stakeholders** — read §2–§3 and the Acceptance Criteria (§6) to understand scope and sign-off.
- **Engineering / DevOps** — use the Test Cases (§5) as the verification checklist; execution steps link to the detailed runbooks.

It does **not** repeat step-by-step procedures — those live in the linked runbooks.

---

## 2. UAT Overview (executive summary)
**Objective:** prove the platform monitors real ATMs end-to-end — live state, faults, cash levels,
EJ (electronic journal) ingestion, and alerting — using the bank's two designated test terminals.

**What is validated**
- Real SNMP polling of the two test ATMs (no simulator fallback in UAT).
- Correct ATM state detection (In-Service, Out-of-Service, Offline, Fault, Out-of-Cash).
- EJ log ingestion into OpenSearch and the Report Portal EJ Search.
- Live dashboards (Grafana) and the Report Portal detail views.
- Fault → Zabbix problem → GLPI ticket flow.

**Environment at a glance**

| Role | Host | IP |
|------|------|-----|
| App / monitoring server (UAT-01) | Postgres, Zabbix, Grafana, GLPI, Report Portal, state-manager, filebeat | `172.26.208.176` |
| Data server (UAT-02) | OpenSearch, OpenSearch Dashboards, ISO 8583 gateway | `172.26.21.50` |
| Test ATM — NCR | NCR | `172.22.248.4` |
| Test ATM — GRG | GRG | `172.23.248.6` |

**Phasing:** this plan covers **Phase 0** (validate the two real ATMs). The broader 45-day
production-readiness rollout is described in [`uat-pilot-checklist.md`](uat-pilot-checklist.md).

---

## 3. Scope & Prerequisites

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

## 4. Environment reference

| Component | Host / IP | Role in UAT | Verify via |
|-----------|-----------|-------------|------------|
| PostgreSQL + Zabbix | UAT-01 `172.26.208.176` | Inventory + metrics store | Zabbix UI `:8080` |
| Grafana | UAT-01 `:3000` | Live dashboards | Grafana UI |
| GLPI | UAT-01 `:8082` | Ticket system | GLPI UI |
| Report Portal | UAT-01 `:8888` | ATM detail / EJ search | Portal UI |
| OpenSearch + Dashboards | UAT-02 `172.26.21.50:9200 / :5601` | EJ storage + search | OSD UI |
| filebeat | UAT-01 | Forwards ATM EJ (`:514`) → OpenSearch | `ss -lun :514` |
| state-manager | UAT-01 | Computes `atm_current_state` from Zabbix | `atm_current_state` table |
| Test ATM NCR | `172.22.248.4` | SNMP source | `snmpwalk` |
| Test ATM GRG | `172.23.248.6` | SNMP source | `snmpwalk` |

---

## 5. UAT Test Cases

> **Steps** columns point to [`UAT_Phase0_Runbook.md`](UAT_Phase0_Runbook.md). Mark **Pass/Fail** per test.
> **Critical** tests must all pass for sign-off.

### 5.1 Infrastructure & network
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-INF-01 | Infra | All containers `Up` | Migration Guide §D.5 | `docker ps` shows services healthy (no `Restarting`) | ✅ |
| TC-INF-02 | Infra | UIs reachable | Migration Guide §F | Zabbix `:8080`, Grafana `:3000`, GLPI `:8082`, Portal `:8888`, OSD `:5601` respond | ✅ |
| TC-NET-01 | Net | SNMP reachability | Runbook §0, §3 | UAT-01 can `snmpwalk` both ATMs on UDP 161 | ✅ |
| TC-NET-02 | Net | EJ listener | Runbook §0 | `ss -lun` shows `:514` on UAT-01 (filebeat) | ✅ |
| TC-NET-03 | Net | ISO 8583 path *(if in scope)* | Runbook §9 | Switch → UAT-02 `:9876` reachable | ⬜ |

### 5.2 SNMP capture & template remap
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-SNMP-01 | SNMP | Walk captured (both) | Runbook §3 | `config/snmp/real_atm_snmpwalk_NCR.txt` / `_GRG.txt` saved with OID rows | ✅ |
| TC-MAP-01 | Mapping | `oid_mapping.csv` complete | Runbook §4 | Every monitored `sim_oid` has a real `real_oid` (no gaps for the items we poll) | ✅ |
| TC-TMPL-01 | Template | Generator guardrail passes | Runbook §5 | `generate_zabbix_templates.py` exits **0** (aborts with exit 1 if any OID unmapped) | ✅ |
| TC-TMPL-02 | Template | Templates remapped & committed | Runbook §5–§6 | XMLs rewritten off simulator root `1.3.6.1.4.1.99999.*`; committed to `uat-prep` | ✅ |

### 5.3 Zabbix registration & collection
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-ZBX-01 | Zabbix | Hosts registered | Runbook §6 | Hosts `ATM-TEST-NCR`, `ATM-TEST-GRG` exist in Zabbix with SNMP interface `:161` | ✅ |
| TC-ZBX-02 | Zabbix | Items populate | Runbook §7 | **Monitoring → Latest data** shows live values (not "Not supported") for both ATMs | ✅ |
| TC-STATE-01 | State | State computed correctly | Runbook §7 | `atm_current_state` reflects real condition per `config/status_map.json` (In-Service / Out-of-Service / Offline / Hardware-Fault / Out-of-Cash / Unknown / Unreachable) | ✅ |
| TC-STATE-02 | State | No false "In-Service" | Runbook §7 | An unreachable/polled-empty ATM shows `UNREACHABLE`/`UNKNOWN`, **never** falsely `IN_SERVICE` | ✅ |

### 5.4 EJ ingestion & Report Portal
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-EJ-01 | EJ | EJ lands in OpenSearch | Runbook §8 | Real EJ lines indexed in `atm-ej-live-*` / `atm-electronic-journal` | ✅ |
| TC-EJ-02 | EJ | Parser extracts fields | Runbook §8 | For NCR & GRG formats, grok yields structured fields; unparsed lines retained with `ej_parsed:false` | ⬜ |
| TC-PORTAL-01 | Portal | Live detail views | Runbook §7 | Report Portal ATM detail shows live cassettes / hardware / state from Zabbix (no blanks) | ✅ |
| TC-GRAF-01 | Grafana | Dashboards live | Runbook §10 | Grafana shows live state + transactions for both ATMs | ✅ |

### 5.5 Alerting & rollback
| ID | Area | Test | Steps | Expected Result | Crit |
|----|------|------|-------|-----------------|------|
| TC-ALERT-01 | Alert | Fault → ticket | Runbook §10 | Induced fault → Zabbix problem → GLPI ticket created | ✅ |
| TC-ISO-01 | ISO 8583 *(conditional)* | Transactions recorded | Runbook §9 | With real ISO traffic, `atm_transactions` grows (source `ISO8583`) | ⬜ |
| TC-ROLL-01 | Rollback | Remap reversible | Runbook §Rollback | Restore `*.simulator.bak` + re-import returns ATMs to last-good templates | ✅ |

---

## 6. Acceptance Criteria / Definition of Done
UAT is accepted when **all Critical (✅) tests pass** and:
- Any non-critical (⬜) gap is documented with an owner and a follow-up date.
- `oid_mapping.csv`, `status_map.json`, and the remapped templates are committed to `uat-prep`.
- No open `UNREACHABLE`/`UNKNOWN` state that cannot be explained by a real ATM condition.

---

## 7. Rollback & Support
- **Template rollback:** `cp config/zabbix/template_*.xml.simulator.bak config/zabbix/template_*.xml` then re-run import (Runbook §Rollback).
- **General troubleshooting:** [`troubleshooting.md`](troubleshooting.md).
- **Real-ATM how-to (SNMP/community/terminal ID):** [`REAL_ATM_INTEGRATION_GUIDE.md`](REAL_ATM_INTEGRATION_GUIDE.md).

---

## 8. Sign-off

| Role | Name | Date | Decision (Accept / Reject) |
|------|------|------|----------------------------|
| UAT Lead (Bank) | | | |
| Monitoring Engineer | | | |
| IT / Network | | | |
| Project Owner | | | |
