# BRD Traceability — Dashen Bank ATM Monitoring

Source: `docs/BRD for ATM Monitoring system (1).md`

Why this file exists: the build was driven by a gap analysis of what stood
between "an ATM is in the database" and "the BRD's claims are actually true".
That served several clauses well, but it was not a traceability pass, and it let
a required feature (cash depletion forecast) sit behind a page that described
it as "planned — BRD 6.3", citing the wrong clause. This table makes the gap
visible instead of implied.

Legend: **MET** — implemented and demonstrated · **PARTIAL** — some of the
clause is met · **GAP** — not built · **BLOCKED** — waiting on an external
dependency.

## 2. Business objectives

| # | Objective | Status | Evidence |
|---|---|---|---|
| 2.1 | High availability, detect issues instantly | MET | Zabbix triggers + GLPI tickets; `verify.py` gate |
| 2.2 | Real-time transaction monitoring | PARTIAL | Live from EJ journals; **synthetic feed until switch integration** |
| 2.3 | Track cash levels, prevent cash-out | PARTIAL | Levels MET; **depletion forecast GAP (5.3)** |
| 2.4 | Operational + management reports | MET | `ATM Performance`, `Transaction Summary`, cash reports |
| 2.5 | Improve incident response time | PARTIAL | Incident module + `state_duration_minutes`; resolution-time report partial (6.4) |
| 2.6 | Fraud monitoring and security oversight | PARTIAL | Anomaly module; "suspicious transaction pattern" alert not tuned to bank rules |
| 2.7 | Centralized dashboard for all ATMs | MET | Portal + Grafana dashboards |

## 3. Scope

| Clause | Status | Note |
|---|---|---|
| 3.1 In scope (all six items) | MET | Transaction feed is synthetic pending 8.1 |
| 3.2 Out of scope | MET | No maintenance, no transaction processing, no core banking build |

## 5. Functional requirements

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 5.1 | Real-time online/offline, location/branch/district/terminal ID, alerts for low cash, offline, unstable link, device failure | MET | ATM detail page; hardware chips; Zabbix triggers. Onboarding Phases 1–4 make the status trustworthy rather than assumed |
| 5.2 | Real-time transaction monitoring | PARTIAL | EJ Search + categories; synthetic feed until switch integration |
| 5.3 | Cassette levels in real time; low/out-of-cash alerts; **depletion forecast from withdrawal patterns** | PARTIAL | **Forecast GAP** |
| 5.4 | ATM error monitoring | MET | WMI parts/devices, 35+14 per NCR host, triggers → GLPI |
| 5.5 | Alerts via email, SMS, dashboard; triggers for offline / cash out / hardware error / suspicious pattern | PARTIAL | Dashboard alerts MET; Email + Africa's Talking SMS media types exist as **templates with placeholders** — not proven end to end |
| 5.6 | Real-time dashboard | MET | Grafana + portal |

## 6. Reporting

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 6.1 | ATM availability report | MET | Report type present |
| 6.2 | Transaction summary | MET | Report type present |
| 6.3 | Cash level: current, cash dispensed, replenishment history | MET | `/admin/cash` unified record |
| 6.4 | Errors, incident resolution time, error frequency | PARTIAL | Incidents + GLPI; resolution-time figure partial |
| 6.5 | Most/least used ATMs, peak transaction hours | MET | `performance` report type |

## 7. Non-functional

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 7.1 | Availability | PARTIAL | Portal restart-tested; no HA/DR story documented |
| 7.2 | Performance | PARTIAL | Fine at 3 ATMs; **fleet-size page loads need the background job (done) + measurement at 1,202** |
| 7.3 | Security | PARTIAL | Roles, CSRF, password hashing; credential escrow **GAP** |
| 7.4 | Scalability: expansion of ATM network | MET | Onboarding Phases 1–4: data-driven EJ list, single provisioning writer, verification gate, fleet report |

## 8. Integrations

| # | Requirement | Status | Note |
|---|---|---|---|
| 8.1 | ATM Switch system | BLOCKED | External dependency; synthetic feed in place behind a labelled banner |
| 8.2 | Core Banking System | GAP | Not started; not required until switch integration |
| 8.3 | Card Management System | GAP | Not started |
| 8.4 | SMS and email notification | PARTIAL | Templates exist, credentials unset — see 5.5 |

## 9. User roles

| Role | Status | Note |
|---|---|---|
| System Administrator | MET | Full admin |
| ATM Operations Officer | MET | operator role (register, provision) |
| Management | MET | viewer role |

## 10. Risk and compliance

| Clause | Status | Note |
|---|---|---|
| 10 | Risk / compliance | PARTIAL | Card data masked in EJ; credential escrow and retention rules GAP |

## Open gaps, ordered by BRD obligation

1. **5.3 cash depletion forecast** — the only clause where every input exists
   (cassette levels, transaction history, replenishment log) and no output is
   produced. The schema comment on `cash_replenishments` already anticipates it.
2. **5.5 / 8.4 alert channels** — email and SMS are templates. "Shall send"
   is unproven until one test alert lands through each.
3. **8.1 switch integration** — blocked externally; the synthetic feed must not
   become permanent without a decision.
4. **7.3 credential escrow** — SNMP community is still typed by hand.
5. **2.6 fraud rules** — anomaly detection exists but "suspicious transaction
   pattern" is not yet tuned to the bank's rules.

## House rule for new work

Every field that exists to help someone **decide** must be impossible to get
wrong by default:

- unknown means "no data", never "fault" (hardware chips)
- a gate must key off the system's own internal truth, not a weaker proxy
  (`atm.parts` for NCR, not "any item ticked")
- healthy means "no action", never "investigate" (fleet report)
- a failing step shows its error instead of a spinner
- timestamps are shown in EAT, the timezone the portal reports in
