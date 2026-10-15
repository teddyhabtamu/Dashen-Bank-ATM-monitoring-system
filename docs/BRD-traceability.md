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
| 2.3 | Track cash levels, prevent cash-out | PARTIAL | Forecast MET; **physical cash levels are unavailable for NCR** (see 5.3) |
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
| 5.3 | Cassette levels in real time; low/out-of-cash alerts; depletion forecast from withdrawal patterns | PARTIAL | Forecast MET and it refuses to invent a date. **Cash levels GAP:** the WMI collector returns cassette *health*, not note counts, and NCR SNMP OIDs are unmapped, so the vault is empty for real NCR ATMs. Forecast falls back to a labelled book estimate (loads − withdrawals) until a real cash source exists |
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
| 7.2 | Monitoring data updates within **5 seconds** of an event | GAP — see the latency budget below |
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

1. **5.5 / 8.4 alert channels** — email and SMS are templates. "Shall send"
   is unproven until one test alert lands through each.
2. **8.1 switch integration** — blocked externally; the synthetic feed must not
   become permanent without a decision.
3. **7.3 credential escrow** — SNMP community is still typed by hand.
4. **2.6 fraud rules** — anomaly detection exists but "suspicious transaction
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

## BRD 7.2 latency budget — measured vs required

BRD 7.2 says monitoring data should update within **5 seconds**. Measured on
UAT (3 ATMs, templates in `config/zabbix/`):

| Data | How it arrives today | Measured | 5s met? |
|---|---|---|---|
| WMI parts/devices | Zabbix polls the agent (port 10050) | **10–30s** (5 items @10s, 26 @30s) | No |
| ATM offline / link | poll (agent / ping) | **10–60s** | No |
| EJ transactions | hourly `pull_ej_logs.sh`, then filebeat → OpenSearch | **up to 60 min** | No — ~700x over |
| Portal page render | Zabbix API + Postgres per request | ~0.3s at 3 ATMs, **uncached** | n/a |

**5 seconds is not achievable by polling, for any data type.** It requires the
event source to push. So the requirement has to be split by data type, with a
budget each can actually meet:

| Data | Target | Why that number |
|---|---|---|
| Transaction events | **≤5s** | Only via switch push (BRD 8.1). This is the one that genuinely needs 5s, because a cash-out or a fraud pattern is time-critical. |
| Device / hardware state | **≤30s** (10s for ping, link, status) | Polling physics. 1,300 hosts x ~90 items at 5s ≈ 23k collections/sec — beyond a single Zabbix server's sane operating point; values queue and arrive late, which is worse than a documented 30s. |
| ATM offline | **≤30s** | Active probing interval *is* the detection latency. 5s pings across 1,300 hosts is affordable but rarely worth the WAN and server load. |
| Dashboard render | **≤2s** | Portal-side: serve from a short-TTL cache instead of calling Zabbix per request. |

### What 5s forces architecturally (and what it does not)

To hit 5s for transactions: switch → HTTPS webhook / AMQP → **queue** →
idempotent, ordered ingest → Postgres + OpenSearch → portal. The request path
must never do the write synchronously; a burst at payday must not block on it.
This is BRD 8.1 and it also unblocks 2.6 (fraud rules).

It does **not** require: proxies (deferred, and `docs/proxy-topology.md` gives
the >3s trigger), or faster hardware for the portal (the portal stores
transactions and state, never telemetry history — Zabbix owns that).

### Scaling risks at 1,300 ATMs that are not latency

1. **Portal fan-out.** Every detail-page render calls the Zabbix API. At fleet
   scale that is self-inflicted load and the first thing to break. Needs a
   5–10s TTL cache. The fleet report is already backgrounded.
2. **EJ volume.** ~390k transaction rows/day (~142M/year). Postgres is for
   recent data and state; OpenSearch already serves search; year-scale reports
   must not query Postgres.
3. **Telemetry volume.** ~337M item values/day. These belong in Zabbix's own
   history store only — never copied into the portal DB.
4. **Alert storms.** A flapping host can flood every channel. Maintenance
   windows, suppression and hysteresis must be configured before go-live, not
   after the first storm.
5. **Verification freshness.** `verify.py` uses a 15-minute window, which is
   aligned with 10–30s polling, not with a 5s target. If the bank signs up to
   5s for transactions, the freshness window should tighten once push exists.

### Recommended next steps for 7.2

1. Put this budget in front of the bank and get the per-data-type targets
   agreed — claiming "5 seconds" for polled hardware is not deliverable.
2. **Cheap, measurable win now:** EJ pull cadence hourly → every minute. The
   Zabbix agent is already installed on the boxes, so this is a crontab change
   with no ATM-side work, and it moves transaction visibility from ~60 min to
   ~1–2 min.
3. **Cheap, measurable win now:** 5–10s TTL cache for Zabbix reads in the
   portal, so pages render from cache instead of hammering the API.
4. ~~Cash input for NCR~~ **DONE (estimate only).** The WMI collector returns
   cassette **health**, not note counts, so physical cash is unavailable for NCR.
   The forecast now falls back to *book cash* (loads − withdrawals) labelled as
   an estimate, and flags a ledger that is short. A **measured** source still
   needs the vendor's OIDs or a CIT-verified snapshot; until then 5.3 is
   PARTIAL, not MET.
5. Design the push ingest (8.1) when the switch project is ready.
