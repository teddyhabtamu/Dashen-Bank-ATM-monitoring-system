# Server Infrastructure Specification — Revised (Worst-Case Sizing)

**Real-Time ATM Monitoring Tool and Reporting System**
**Prepared by:** IT Modernization Program Management Department
**Scope:** Production sizing for **up to 5,000 ATMs** (worst case / growth case),
3-year data-retention policy
**Date:** August 2026
**Status:** Revised after management review of the original specification

---

## 1. TL;DR

The original spec was **over-built on compute**. Sizing honestly for the worst case
(fleet growth to 5,000 ATMs) and keeping the bank's **3-year retention policy**
within a realistic **~4 TB** storage budget:

| What | Original spec | This revision (5,000 ATMs) | Change |
|---|---|---|---|
| Production vCPU | 60 (+16 UAT) | **24** (+8 UAT) | −60% |
| Production RAM | 184 GB (+96 UAT) | **72 GB** (+16 UAT) | −61% |
| Production storage | 3.5 TB (+1 TB UAT) | **4 TB** (+0.7 TB UAT) | ≈ same |
| Production VMs | 5 | **3** | fewer, consolidated |

Counting honestly: **compute drops ~60% and RAM ~61% with the same layout doing
more work**. Officially, Zabbix documents 5,000 monitored hosts at ~4 vCPU / 8 GB
for the server; our numbers build in 2× headroom and the growth plan. Storage is
sized to ~4 TB total — the honest budget for ~3 years of EJ journals, with the
retention rules in §7 keeping it in check.

The single cheapest decision available is **how long EJ journals stay searchable**:

- **12 months searchable + archive older** → ~2.5 TB total disk (room to spare)
- **3 years searchable** (current policy) → ~4 TB total disk (the budget above)

---

## 2. Assumptions (read before quoting any number)

| Assumption | Value | Notes |
|---|---|---|
| Fleet / worst case | **5,000 ATMs** | growth is planned; numbers below are peak, not today |
| Poll interval | 60 s per ATM | 30 s would double database storage; not recommended ≥ 5,000 (§7) |
| Items per ATM | ~40 | status, cassettes, door/sensors, connectivity, counters |
| Transactions / ATM / day | ~300 | incl. declines, balance checks |
| EJ journal, searchable text per txn | ~2 KB | the dominant storage consumer |
| Monitoring history retention | 30 days (trends 1 year) | Zabbix standard practice |
| Transaction + EJ retention | **3 years** (bank policy) | retention is the cost knob (§7) |
| Operational posture | Single node per tier + nightly backup + restore drill | 3-node HA clusters not justified at 5,000 |

---

## 3. Production (LIVE) — 3 VMs, sized to 5,000 ATMs

| VM | Services | vCPU | RAM | Storage (NVMe) | Size logic (honest math) |
|---|---|---|---|---|---|
| **APPS-01** | Zabbix Server + Zabbix Web + Grafana + Grafana Renderer + GLPI (incl. MariaDB) + Report Portal + Anomaly Detector + Network Correlator + State Manager | 8 | 16 GB | 400 GB | Zabbix's own guide: 5,000 hosts ≈ 4 vCPU / 8 GB — 8/16 is 2× headroom. Every other service on this VM (dashboards, GLPI, reports, detectors) is light and already runs together on a laptop today. 400 GB = MariaDB + logs + exports. |
| **DATA-01** | PostgreSQL (monitoring history/trends + ATM transactions + fleet data) + OpenSearch/Elasticsearch + Filebeat (EJ search/index) | 12 | 48 GB | **3.5 TB** | 5,000 × 40 items × 1,440 polls ≈ 288 M rows/day ≈ **30 GB/day** → 30-day history ~1 TB + trends + 3-yr transactions (~250 GB) in PostgreSQL (32 GB RAM feeds its cache). EJ journals ~3 GB/day ≈ **1.1 TB/yr** indexed in OpenSearch (16 GB JVM heap). 3.5 TB = PG (~1 TB) + ES (~2.5 TB, ~2.5 yrs hot) + margin. |
| **GWY-01** | ISO 8583 Gateway | 4 | 8 GB | 100 GB | Kept on its **own VM for network isolation** — it is the only ingress from the bank's switch (DMZ/blast-radius separation, standard for banking integrations). 4 vCPU comfortably parses 5,000 ATMs × ~300 tx/day even at switch peaks. |
| **TOTAL** | | **24** | **72 GB** | **~4 TB** | **3 VMs** |

> **Why 3 VMs, not 5:** every extra VM is an OS to patch, a firewall rule-set, and a
> backup stream. Zabbix server + web on the same host as the app tier is a standard,
> documented deployment at this fleet size. The two separations that *matter* are
> kept: DATA-01 owns the high-I/O storage tier (DB + search), and GWY-01 sits alone
> facing the switch. If the bank later relaxes the gateway-isolation requirement,
> GWY-01's services fold into APPS-01 without any resize — the totals are unchanged.

---

## 4. Growth phases (buy once, not repeatedly)

| Phase | Fleet | vCPU (total) | RAM (total) | Storage (total) |
|---|---|---|---|---|
| **1. Pilot / UAT** | ≤ 500 | 8 | 16 GB | 500 GB |
| **2. Production go-live** | 2,000–3,000 | 16 | 48 GB | ~2 TB |
| **3. Full growth (worst)** | 5,000 | 24 | 72 GB | ~4 TB |

Because all tiers are single VMs on a hypervisor, going Phase 2 → 3 is "add vCPU
and extend the virtual disk", not redeploy. The Phase-3 VM sizes set the purchased
**physical host** footprint, so the same hardware covers all three phases.

---

## 5. What to avoid, even at worst case

1. **30-second polling at 5,000 ATMs** — almost doubles disk. Standard ATM practice
   is 60 s. Keep 60 s and status-critical sensors at 30 s only.
2. **3-node Elastic HA cluster** — single node + nightly snapshots is sufficient
   at this scale; clustering multiplies CPU/RAM/disk by ~3× for negligible gain.
3. **64 GB PostgreSQL** — that sized for *millions* of metrics. 48 GB on DATA-01
   covers PostgreSQL + OpenSearch combined; 16–24 works if housekeeping is clean.
4. **NVMe on every VM** — NVMe matters only on **DATA-01** (random IO from
   PostgreSQL + OpenSearch). Zabbix, apps, and the gateway are fine on mid-level
   SATA SSD.
5. **A VM per container** — this project runs ~18 containers; 3 tiered VMs
   (apps, data, gateway) is the right grouping. One VM per tool is waste; folding
   everything into one box loses the two isolations that matter (data tier + DMZ).

---

## 6. Storage math — the whole cost story (~4 TB budget)

| Tier | 5,000 ATMs | 1 yr | 3 yr | Landed on |
|---|---|---|---|---|
| History (60 s, 40 items, 30-day) | ~30 GB/day | — | — | DATA-01 (PG) ≈ 1 TB |
| Trends (hourly, 1 yr) | ~0.2 GB/day | ~80 GB | ~250 GB | DATA-01 (PG) |
| Transactions (3-yr) | ~0.25 GB/day | ~90 GB | ~250 GB | DATA-01 (PG) |
| **EJ journals (the driver)** | ~3 GB/day | **≈1.1 TB** | **≈3.3 TB** | DATA-01 (ES) ≈ 2.5 TB |
| Apps / logs / exports | — | — | ~0.4 TB | APPS-01 |
| Gateway / switch feeds | — | — | ~0.1 TB | GWY-01 |

Bottom line: **~4 TB total covers the bank's 3-year retention policy at 5,000 ATMs**,
with OpenSearch holding roughly 2.5 years of searchable EJ. If EJ searchable
retention is trimmed to 12 months, total disk drops to **~2.5 TB** and everything
fits with wide margins.

---

## 7. Retention rules that keep this from growing further

1. **Poll at 60 s, not 30 s.**
2. **Audit the ~40 items per ATM** — cut ones nobody uses; storage grows with
   collection, not with what is retained.
3. **Slow-moving values use slow polling** — cash cassette levels don't change
   hourly; poll them daily while connectivity stays at 60 s.
4. **Drop history to trends** — purge Zabbix history > 30 days; trends preserve the
   long picture in a quarter of the space.

---

## 8. UAT Environment

| VM | Services | vCPU | RAM | Storage |
|---|---|---|---|---|
| UAT-01 | Zabbix + PostgreSQL + OpenSearch + Grafana + GLPI + Report Portal | 4 | 8 GB | 500 GB |
| UAT-02 | ISO 8583 Gateway + search/validation tooling | 4 | 8 GB | 200 GB |
| **Total** | | **8** | **16 GB** | **700 GB** |

UAT needs to replicate the **shape** of the system, not its 5,000-node **scale**:
10–20 simulated ATMs are enough to validate templates, dashboards, and reports.

---

## 9. Backup & DR (sits outside the VM disks)

- Daily PostgreSQL dump + OpenSearch snapshot, streamed to an **offsite backup
  target**, 30-day rollback.
- ~4 TB backup capacity, separate from the 4 TB of production disk.
- RTO ≤ 24 h, restore tested quarterly.

---

## 10. Validate before you buy

Every number above is an estimate with margin. Before procurement, run a **measured
load test**: this project's simulators already generate the full fleet — run 5,000
simulated ATMs for 2–3 weeks at 60 s polling and measure real GB/day growth from
PostgreSQL and the ES index. Two weeks of simulation beats any size estimate —
adjust the disk columns to the measured reality, not to this document.

---

*Prepared by: IT Modernization Program Management Department — August 2026*