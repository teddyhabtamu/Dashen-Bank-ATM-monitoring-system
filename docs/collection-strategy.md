# ATM Monitoring — Data Collection Strategy & Build Feasibility

**Author:** IT Modernization Department, Dashen Bank
**Context:** Modernization of the current ATM monitoring system (NetXMS-based) using open-source tooling (Zabbix-centric proposal).
**Status:** Decision analysis / discussion record — not yet committed to a single path.

---

## 1. How Dashen's current system (NetXMS) actually works

NetXMS monitors Dashen's NCR + GRG ATM fleet using a **proprietary agent installed on each terminal**. Key facts from NetXMS's documented ATM capability:

- The agent runs **independently of the ATM host software** and reads the XFS hardware layer directly (dispenser, card reader, printer, cassettes, sensors).
- It **synchronizes the electronic journal (EJ) within ~30 seconds** of any change — so EJ is available even if the ATM later goes unreachable.
- It supports **screenshots / screencasts, remote command execution, file transfer, XFS commands (card eject), and reboot** over TLS-encrypted, authenticated TCP.
- It can run in **tunnel mode** (agent initiates the connection) — required for ATMs behind branch NAT/firewalls.
- It has **agent caching mode**: if the link drops, the agent keeps collecting locally and pushes the backlog on reconnect, so no data is lost during outages.
- Server side provides thresholding, event processing, alarms, reporting (JasperReports), and full-text EJ search across all ATMs.

NetXMS is **purpose-built for multi-vendor ATM monitoring**. Dashen's deployment is therefore: agent on each NCR/GRG terminal → NetXMS server (regional zones/proxies) → DB + web console.

---

## 2. Our proposed approach (Zabbix-centric) vs NetXMS

Our built stack is **Zabbix 6.4 + simulators that mimic SNMP-shaped OIDs over HTTP**, plus purpose-built components (EJ engine, transaction engine, state manager, fault-type mapping, report portal, Grafana, GLPI ticketing).

| Capability (BRD) | NetXMS (current, proven) | Our Zabbix approach (built) |
|---|---|---|
| ATM hardware / cash / status | Agent on terminal, native | HTTP-mimicked SNMP OIDs (simulators) |
| EJ sync within 30s | Built-in agent feature | Separate `atm-ej-engine` (built) |
| ATM screen screenshot | Native agent feature | Not possible over SNMP/HTTP |
| Link-loss data caching | Agent caching mode | None — data gaps during outage |
| ATM behind NAT/firewall | Tunnel mode (agent outbound) | Needs a reachable port per ATM |
| Remote actions (reboot, eject) | Native | Not in BRD scope / not built |
| 5-sec freshness @ 2,000–2,500 ATMs | Active agent push | Polling fan-out problem (flagged) |
| Vendor performance / mgmt reports | JasperReports | Custom report portal (built) |
| Human-readable fault mapping | Raw by default | `fault_type_map` (built) |
| Ticket automation / RCA | Basic | GLPI webhook + RCA-pending (built) |

**Key realization:** NetXMS delivers most BRD requirements out of the box because it was designed for this. Our Zabbix build is a from-scratch reimplementation of the same collection layer — EJ engine, txn engine, state manager, fault maps — that NetXMS already provides.

---

## 3. Options considered

- **A. NetXMS as primary collector + our portal/reports on top.** Reuse Dashen's proven agent channel; point portal/Grafana/GLPI at NetXMS data (API/DB). Our value-add (reports, vendor analytics, fault mapping, GLPI RCA) stays. Lowest risk, full BRD coverage.
- **B. Full Zabbix, replace NetXMS.** Deploy `zabbix-agent2` on ATMs reusing the same channel NetXMS uses. Viable (agents work at Dashen) but discards a working purpose-built system and rebuilds EJ/screenshot/caching Zabbix lacks natively. High effort, low added value.
- **C. Zabbix + simulators as the production model.** Only works if real ATMs speak our sim protocol — they will not. This is a demo/prototype, not a production migration path.

---

## 4. Decision (refined, 2026-07-16)

**Chosen path: keep the current SNMP-oriented design for the build phase; perform real production migration (SNMP against actual NCR/GRG ATMs) once the bank provides the server and real ATM access.** This is the pragmatic middle ground and is honest about the gap.

Critical correction to the current build: **today the simulators are collected over HTTP, not SNMP.** The Zabbix templates use `HTTP agent` items hitting `http://172.17.0.1:{$ATM_PORT}/oid/...`. That is *not* SNMP. For the "SNMP now, real SNMP later" plan to be clean, the simulators must emit **real SNMP** (e.g. `snmp4arts` / `snmp-simulator`) so the exact Zabbix item type (SNMP agent, real OIDs) used in production is exercised in development. Otherwise the production cutover requires rewriting every Zabbix item from HTTP → SNMP and re-mapping OIDs — wasted rework.

What stays identical across sim → real: item **names**, **triggers**, **value maps**, **Grafana dashboards**, **report portal**. Only the item **type** (HTTP → SNMP) and the **OID/transport** change.

### Pre-production gates (must be done before fleet cutover)
1. **Simulators speak real SNMP**, not HTTP — so Zabbix items are SNMP-native from day one.
2. **Collection model:** The sandbox uses per-ATM published ports (1161–1260) for simulation only. In production, all real ATMs use **UDP 161** — no port mapping needed. With ~1,200 ATMs and our server specs (16 vCPU + 64 GB PostgreSQL), **centralized polling is the initial deployment**. Zabbix proxies are deferred to Phase 2 — see `docs/proxy-topology.md`.
3. **Real NCR + GRG MIBs obtained and compiled** into Zabbix; sim OIDs must map to the real vendor OID trees (the sim's `1.1.0` style is *shaped like* NCR/GRG but is not the real MIB).
4. **OID mapping table** built (sim OID ↔ real vendor OID ↔ Zabbix item) and committed.

### Honest capability gaps vs NetXMS (must be stated to stakeholders)
- **ATM screen screenshot / screencast:** not available over SNMP. NetXMS agent does this. Mitigation: not in BRD scope; accept the gap or source via vendor console.
- **Link-loss data caching:** SNMP polling silently gaps during ATM outages. NetXMS agent caches and replays. Mitigation: accept brief gaps, or rely on EJ feed from the Switch for the transaction record.
- **Remote actions (reboot, card eject):** not in BRD scope; SNMP alone cannot. Accept.

### Fleet-size inconsistency (resolved 2026-07-25)
- Abinet's inventory Excel confirmed **1,202 ATMs** (798 GRG + 429 NCR).
- This reconciles the earlier discrepancy between "~1,300" and "2,300–2,700".
- Capacity planning and VM sizing use this confirmed count.

---

## 5. Build & deployment feasibility (2-month horizon)

- **Achievable in 2 months (high confidence):** the presentation/analytics layer — report portal, Grafana dashboards, GLPI ticketing + RCA automation, vendor-performance reports, human-readable fault mapping, EJ search. This is the differentiator and is largely done.
- **Achievable in 2 months as a pilot:** connect a **subset** of real ATMs (10–50) over SNMP and prove end-to-end on the production VMs.
- **NOT achievable in 2 months:** full fleet cutover of all real ATMs. Reasons: (a) agent/SNMP onboarding across remote branches is an operations project requiring vendor coordination and field work; (b) the scale/proxy/OID work above is a prerequisite; (c) parallel-run validation against NetXMS is needed before trusting the new system. Recommend phased waves, not big-bang.
- **Stakeholder expectation:** the system delivered in 2 months should be framed as "production-ready platform + pilot on real ATMs, better than current on reporting/analytics," not "all ATMs migrated."
