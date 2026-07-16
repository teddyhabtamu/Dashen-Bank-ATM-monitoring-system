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

## 4. Open decision

The production collection path (NetXMS reuse vs full Zabbix replacement) is **not yet finalized**. The recommendation is to treat NetXMS as the telemetry source of truth and layer our presentation/analytics/ticketing on top, retaining Zabbix only for surrounding IT infrastructure monitoring. The simulators are dev/demo artifacts, not the production collection mechanism.

---

## 5. Build & deployment feasibility (2-month horizon)

See section "Feasibility" notes in project tracking. Summary: the **portal/reports/analytics/ticketing layer is achievable in 2 months**; the **production ATM data-collection cutover for all 2,000–2,500 real ATMs is the critical-path risk** and depends on the collection-strategy decision above.
