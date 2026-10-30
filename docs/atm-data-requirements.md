# ATM Data Requirements vs Reality

Purpose: single inventory of every data point the monitoring system needs
from each ATM, what flows today, what does not, and the concrete path to
close each gap. Evidence cut-off: 2026-10-09 (UAT-01 + NCR SelfServ 34
`172.23.248.4` via NAT `10.41.30.114`; agent + SNMP paths live).

Legend: ✅ flowing · ⚠️ partial/proxy · ❌ blocked/missing

## A. Identity & platform (all ✅)

| # | Data | Consumer | Source today | Status |
|---|------|----------|--------------|--------|
| A1 | ATM ID, name, branch, district, city, region, terminal ID | Portal, reports, tickets, maps | `atm_locations` (seed + CSV import + Registration UI) | ✅ |
| A2 | Vendor (NCR/GRG), model | Template routing, vendor reports, GLPI category routing | `atm_locations.vendor/model` + `{$ATM_VENDOR}` macro | ✅ |
| A3 | Management IP | Polling, tickets | `atm_locations.ip_address` | ✅ |
| A4 | GPS coordinates | Geomap, SVG fallback map | `atm_locations.latitude/longitude` (4 Kilo seeded) | ✅ (data entry ongoing) |
| A5 | OS metrics (CPU/mem/disk/uptime), network interfaces | Dashboards, capacity | SNMP host-resources + IF-MIB (proven in walk) | ✅ |
| A6 | Agent heartbeat / software inventory | "Is the ATM's monitoring alive?" | Zabbix agent 7.0.0 on box (`agent.ping` proven over NAT) | ✅ |

## B. Hardware health

| # | Data | Consumer | Source today | Status |
|---|------|----------|--------------|--------|
| B1 | Part states, all devices (33 parts: cassettes present, pick modules, reader, shutter, printer, presenter, transport…) | Monitoring Center, outage causes (Phase 2), triggers | WMI `NCR_DevicePart` via `atm-wmi-collector.ps1` → `atm.parts` → 41 dependent items (master flowing; dependents under repair — JSONPath→JS migration Oct 9) | ⚠️ pipe built, values pending |
| B2 | Same via SNMP (NCR enterprise `1.3.6.1.4.1.191`, `NCR-CORESST-MIB`) | Zabbix SNMP triggers (38 NCR + 30 GRG already defined) | Windows SNMP view: **enterprise tree empty** ("past the end of the MIB tree") | ❌ agent exposes nothing; triggers cannot fire on real boxes as written |
| B3 | Supervisor / service mode (`sstService` equivalent) | Health rollup override (IN_SERVICE vs maintenance) | Unknown over WMI or SNMP | ❌ needs discovery (APTRA status/API) |
| B4 | Power/UPS/battery, temperature, doors, intrusion | Environment + security monitoring (BRD rows 9–10) | `NCR_SensorData`/SIU classes listed, instances unverified | ❌ unbuilt |

## C. Cash (the critical gap)

| # | Data | Consumer | Source today | Status |
|---|------|----------|--------------|--------|
| C1 | **Notes per cassette (bill counts)** | Cash-out detection, depletion forecast (BRD 6.3), CIT planning, outage report | `NCR_DispensableBills` / `FinancialContent` / `FinancialDenomination` classes: **empty**. SNMP: unavailable. Dispense-math proxy (`cash_watcher.py`): estimates only | ❌ **single biggest data gap** |
| C2 | Cassette replenishment events (loads) | Cash reports, forecast baseline | Manual `Cash Loads` form (AUTO source reserved for future) | ⚠️ manual only |
| C3 | Cash dispensed totals | Reports, KPIs, reconciliation | ISO 8583 transactions (sim feed; real switch feed pending) | ⚠️ amounts flow; source is simulation until cutover |

## D. Transactions & journals

| # | Data | Consumer | Source today | Status |
|---|------|----------|--------------|--------|
| D1 | Real-time transactions (type/status/amount/error/pan-masked) | Dashboards, KPIs, anomaly detection, reports | ISO 8583 gateway **simulation** (60k+ rows); TCP mode built, bank feed pending | ⚠️ pipeline proven, production data pending |
| D2 | Electronic Journal entries | Dispute investigation (EJ Search), fault evidence (jams/retracts/power) | filebeat → OpenSearch pipe built; **37 docs total on UAT = pipe dry**; bank journal feed pending | ⚠️ code ready, data absent |
| D3 | Reversal/timeout outcome codes | Success-rate truth, anomaly tuning | Must verify against real switch codes at cutover | ⚠️ pending real data |

## E. Events, alerts, tickets

| # | Data | Consumer | Source today | Status |
|---|------|----------|--------------|--------|
| E1 | Device/link state-change events | Problems, tickets, outage rows | Zabbix triggers + webhook → GLPI (lifecycle drill passed; test ticket #14/#15 end-to-end) | ✅ chain proven |
| E2 | Trigger firing on REAL hardware faults | Same as E1, for WMI/SNMP device OIDs | SNMP OIDs empty on box; WMI codes uncalibrated (observation cycle just started) | ⚠️ framework ready, live firing unverified |
| E3 | Per-ATM link quality (latency/loss per link type) | Network blame-split (bank vs Ethio-telecom vs power) | `network-correlator` (self-host proven); per-ATM link telemetry at cutover | ⚠️ partial |
| E4 | Recovery/RCA evidence | Pending→Solved flow (BRD Req 12) | Webhook recovery block sets Pending + RCA demand; manual RCA process | ✅ by design |

## F. What the numbers say

- **Have (✅):** 6 identity/platform + E1/E4 chains + reporting engine ≈ **55% of requirement by count, ~70% by operational value** (everything the morning workflow needs except cash truth).
- **Partial (⚠️):** B1 (days away), C2/C3/D1/D2 (external feeds), E2/E3 (calibration/cutover).
- **Blocked (❌):** **C1 bill counts**, B2 SNMP enterprise, B3 supervisor mode, B4 environment/security, cameras (BRD row 9 — scoped out pending decision).

## G. Solution roadmap (prioritized)

1. **Verify WMI dependents flow** (in progress — JS preprocessing fix). Unlocks: part-level health, Phase-2 outage causes, calibrated triggers. Owner: team. No bank dependency.
2. **Bill counts (C1)** — the one gap no polling trick closes. Parallel tracks, in order of likelihood: (a) ask NCR/TMS owner for the cassette-count feed or file drop the bank already gets for CIT planning; (b) APTRA/NDC status files or printer-journal lines on the box parsed by an agent-side script; (c) dispense-math estimation hardened with CIT-load anchors (manual form already captures them). Owner: bank Channel Support + vendor. **This is the report's #1 ask.**
3. **Real transaction + journal feeds** (D1/D2) — bank switch + journal source activation at cutover. No build needed; needs scheduling.
4. **Trigger calibration + health rollup** — one observation cycle on live WMI values (started 2026-10-09), ideally covering a CIT visit. Then encode thresholds. Owner: team.
5. **Supervisor-mode + sensor discovery** (B3/B4) — one focused on-box session: APTRA status surface + SIU sensor instances. Small; schedule with the ATM custodian.
6. **SNMP enterprise (B2)** — deprioritize: WMI supersedes it on Windows boxes. Keep the 38 triggers defined but expect them to stay quiet on real hardware; revisit only if a non-Windows (GRG Linux?) estate appears.
7. **Cameras/power/UPS, mobile app, CBS/Card feeds** — scoped decisions already logged in the BRD matrix (v2.0 §8/§10); not data problems, governance ones.

## H. Evidence pointers (for the report appendix)

- SNMP walk proving empty enterprise tree: `snmpwalk_output.txt` (UAT host) + `docs/` runbook §3.1.
- WMI 33-part live JSON: `zabbix_get atm.parts` transcript 2026-10-01 (agent-side) and 2026-10-09 (over NAT).
- Template + items: `config/zabbix/template_atm_wmi.xml`, `scripts/import_wmi_template_api.py`, Zabbix Latest Data screenshots.
- Reporting engine: `docs/` outage-template mapping + delivered 7 AM mail.
- BRD position: `docs/BRD_Compliance_Matrix.md` v2.0.
