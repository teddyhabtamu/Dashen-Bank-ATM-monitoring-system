# UAT Handover — Cover Document

> Single signature page + index for the 2-test-ATM UAT. Detail lives in the
> referenced docs; nothing is duplicated here. Fill the TBDs on sign-off day.

| Field | Value |
|-------|-------|
| Build under test | TBD (commit hash + `docs/UAT_Release_Notes.md` entry) |
| UAT window | TBD – TBD |
| UAT-01 (app) | `172.22.10.45` — Zabbix, Postgres, Grafana, GLPI, Report Portal, Filebeat |
| UAT-02 (data) | `172.22.10.60` — OpenSearch, Dashboards, ISO 8583 gateway |
| Test ATMs | `ATM-TEST-NCR` (`172.22.248.4`, NCR) · `ATM-TEST-GRG` (`172.23.248.6`, GRG) |
| Credentials | Sealed handover (server `.env`, GLPI admin) — never in git or email |

## 1. Scope

- **In:** live SNMP state, cash levels, EJ ingestion + dispute search, dashboards, fault → GLPI tickets, transaction reports for the 2 test ATMs.
- **Out:** full-fleet rollout; D1–D7 decisions (mobile, cameras/power, 5s-SLA, HA/encryption, CBS scope, SMS key, AD cutover) — tracked in `docs/BRD_Compliance_Matrix.md` §8 with owners.

## 2. Test summary

Full record: `docs/UAT_Test_Plan.md` §7 (mark Pass/Fail per TC during execution).

| Suite | Result |
|-------|--------|
| §7.1 Infrastructure & network | TBD |
| §7.2 SNMP capture & template remap | TBD |
| §7.3 Zabbix registration & collection | TBD |
| §7.4 EJ ingestion & Report Portal | TBD |
| §7.5 Alerting & rollback | TBD |

Acceptance bar (`UAT_Test_Plan.md` §10): **all Critical (✅) tests pass**; no unexplained `UNREACHABLE`/`UNKNOWN`.

## 3. Defect summary

Log: (link/sheet TBD — see `UAT_Test_Plan.md` §9 for severity + process).

| Severity | Open | Closed / Waived |
|----------|------|-----------------|
| Critical | TBD | TBD |
| Major | TBD | TBD |
| Minor | TBD | TBD |

Rule: every Critical is closed or waived in writing by the UAT Lead.

## 4. Known issues & gaps

Extract on the day from `docs/BRD_Compliance_Matrix.md` (🟡/❌ rows). Each entry gets owner + follow-up date. Conditional tests (⬜: `TC-EJ-02`, `TC-ISO-01`, `TC-NET-03`) documented here, not re-reported.

| # | Issue / gap | Owner | Follow-up |
|---|-------------|-------|-----------|
| 1 | TBD | TBD | TBD |

## 5. Cutover artifacts (committed to `uat-prep`)

- [ ] `config/snmp/real_atm_snmpwalk_NCR.txt` + `..._GRG.txt`
- [ ] `config/snmp/oid_mapping.csv` (complete — generator exit 0)
- [ ] Remapped `config/zabbix/template_ncr_snmp.xml` + `template_grg_snmp.xml`
- [ ] Final `config/status_map.json`

## 6. Operations handover

Runbook covers: health check, add-ATM flow (`docs/atm-onboarding.md`), RED-host triage (`docs/troubleshooting.md`), service restarts, backup/restore proof, who-to-call. Walkthrough session: TBD.

## 7. Signatures

| Role | Name (fill now) | Date | Decision (Accept / Reject) |
|------|-----------------|------|----------------------------|
| UAT Lead (Bank) | TBD | TBD | TBD |
| Monitoring Engineer | TBD | TBD | TBD |
| IT / Network | TBD | TBD | TBD |
| Project Owner | TBD | TBD | TBD |

## 8. Document index

| Doc | Purpose |
|-----|---------|
| `docs/UAT_Test_Plan.md` | Plan, cases, traceability, acceptance |
| `docs/UAT_Phase0_Runbook.md` | Cutover execution steps |
| `docs/UAT_Migration_Guide.md` | VM deploy + firewall table |
| `docs/BRD_Compliance_Matrix.md` | Requirement status + D1–D7 |
| `docs/UAT_Release_Notes.md` | Build contents (create per build) |
| `docs/atm-onboarding.md` | Add-ATM checklist |
| `docs/troubleshooting.md` | Failure triage |
| `docs/LDAP_Integration.md` | Directory auth record + AD cutover |
| `docs/REAL_ATM_INTEGRATION_GUIDE.md` | Beginner field guide |
