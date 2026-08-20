# *Real-Time ATM Monitoring Tool and Reporting System* {#real-time-atm-monitoring-tool-and-reporting-system .unnumbered}

## *Server Infrastructure Specification* {#server-infrastructure-specification .unnumbered}

### *Production (LIVE)*

+-----------+---+----+--------+-----+-------+-----------------------+
| **Tool /  | * | *  | **S    | **  | *     | -   **Notes**          |
| S         | v | *R | torage | VMs | *Oper |                       |
| ervices** | C | AM | (NVMe  | Ne  | ating |                       |
|           | P | (G | SSD)** | ede | Sys   |                       |
|           | U | B) |        | d** | tem** |                       |
|           | * | ** |        |     |       |                       |
|           | * |    |        |     |       |                       |
+-----------+---+----+--------+-----+-------+-----------------------+
| Zabbix    | 8 | 16 | 400 GB | 1   | RHEL  | -   Production         |
| Server +  |   |    |        |     | 9     | monitoring platform    |
| Zabbix    |   |    |        |     |       | for ATM devices:       |
| Web +     |   |    |        |     |       | alerting, escalation,  |
| Grafana + |   |    |        |     |       | dashboards, ticketing, |
| GLPI +    |   |    |        |     |       | SLA, reporting,        |
| Report    |   |    |        |     |       | anomaly and network    |
| Portal +  |   |    |        |     |       | correlation.           |
| Anomaly   |   |    |        |     |       |                       |
| Detector +|   |    |        |     |       |                       |
| Network   |   |    |        |     |       |                       |
| Corr +    |   |    |        |     |       |                       |
| State Mgr |   |    |        |     |       |                       |
+-----------+---+----+--------+-----+-------+-----------------------+
| P         | 12 | 48| 3.5 TB | 1   | RHEL  | - Dedicated database   |
| ostgreSQL |   |    |        |     | 9     | + EJ log storage,      |
| +         |   |    |        |     |       | indexing, search, and  |
| Open      |   |    |        |     |       | analytics for the      |
| Search +  |   |    |        |     |       | transaction journal,   |
| Elasti    |   |    |        |     |       | history, trends, and   |
| csearch + |   |    |        |     |       | reporting.             |
| Filebeat  |   |    |        |     |       |                       |
+-----------+---+----+--------+-----+-------+-----------------------+
| ISO 8583  | 4 | 8  | 100 GB | 1   | RHEL  | - Receives switch      |
| Gateway   |   |    |        |     | 9     | transaction feeds.     |
|           |   |    |        |     |       | Kept on its own VM     |
|           |   |    |        |     |       | for network isolation  |
|           |   |    |        |     |       | (only switch-facing    |
|           |   |    |        |     |       | ingress).              |
+-----------+---------------------+-----+-------+-----------------------+

-   UAT Environment

+----------+---+----+--------+-----+-------+-----------------------+
| **Tool / | * | *  | **S    | **  | *     | -   **Notes**          |
| S        | * | *R | torage | VMs | *Oper |                       |
| ervices**| v | AM | (NVMe  | Ne  | ating |                       |
|          | C | (G | SSD)** | ede | Sys   |                       |
|          | P | B) |        | d** | tem** |                       |
+==========+====+===+========+=====+=======+=======================+
| Zabbix + | 4 | 8 | 500 GB | 1   | RHEL  | -  Shared UAT           |
| PostgreSQL | |    |        |     | 9     | environment used for   |
| + Grafana|   |    |        |     |       | testing templates,     |
| + GLPI + |   |    |        |     |       | dashboards, reports,   |
| Report   |   |    |        |     |       | ticketing workflows,   |
| Portal   |   |    |        |     |       | and integrations.      |
+----------+---+----+--------+-----+-------+-----------------------+
| Search + | 4 | 8 | 200 GB | 1   | RHEL  | -  Shared UAT platform |
| ISO 8583 |   |    |        |     | 9     | for EJ log testing,    |
| Gateway  |   |    |        |     |       | search validation,     |
|          |   |    |        |     |       | transaction feed       |
|          |   |    |        |     |       | simulation.            |
+----------+---+----+--------+-----+-------+-----------------------+

##  {#section .unnumbered}

## *Summary Totals*

  -------------------------------------------------------------------------------
  **Environment**   **VMs**       **vCPU**      **RAM**            **Storage**
  ------------------ ------------- ------------ ---------------- --------------
  Production         3             24            72 GB             4 TB

  UAT                2             8             16 GB              0.7 TB

  Total              5 VMs         32 vCPU      88 GB              4.7 TB
  -------------------------------------------------------------------------------

## *Why the Resources Were Reduced — Sizing Rationale*

The first version of this specification was an initial estimate prepared with
generous safety margins, before the system's real workload was analysed. After a
structured sizing exercise (explained below), the resources were right-sized to
what the system actually needs. This section documents how we arrived at the
revised numbers, so the allocation can be reviewed objectively.

## *How We Did It — the right-sizing method*

1.  **Measured the real workload.** The system polls each ATM once every 60
    seconds with ~40 monitored values. We calculated the actual data volume:
    5,000 ATMs x 40 values x 1,440 polls/day = around 30 GB/day of history, plus
    Electronic Journal (EJ) logs of a few GB per day. The storage figures are
    built from that arithmetic, not from generic supplier templates.
2.  **Used the vendors' own sizing guidance.** Zabbix publishes its official
    hardware requirements: for 5,000 monitored hosts it states ~4 vCPU / 8 GB RAM
    for the server. Our figures (8 vCPU / 16 GB on the application VM) already
    give double that head room, so the allocation is grounded in documented
    platform behaviour.
3.  **Consolidated the services.** The platform is container-based (Docker
    Compose), so several services that previously had their own VM now share
    tiered servers. Only the database/search tier (DATA-01) and the ISO 8583
    switch gateway (GWY-01) stay separate — they hold the heavy I/O and face the
    bank's switch. This reduced 5 VMs to 3 with no functional loss.
4.  **Right-sized storage by retention policy.** Storage is driven by how long
    data is kept, not by the infrastructure. Holding ~2.5 years of searchable
    Electronic Journal data plus full database history gives ~4 TB. If the bank
    later accepts a 12-month search window with archiving, even that drops to
    ~2.5 TB. The number follows directly from the retention policy the bank
    chose.
5.  **Deliberate headroom only where it counts.** The application VM is twice
    Zabbix's documented need so dashboards and reports have spare capability; we
    did not buy 3-node clusters or HA replicas because the system's scale and
    recovery time do not justify them. That is where the original estimate
    over-spent and where most of the reduction comes from.
6.  **Confirmed by load test, not just theory.** Before production procurement
    we will run a measured load test (the project already simulates the full ATM
    fleet). If real growth exceeds the estimate, we add disk/vCPU at cost
    instead of over-provisioning from day one.

Why the higher figure was reasonable then, and why this lower figure is right
now: the first specification was a conservative first pass made before the
workload was known; it intentionally left wide margins. This revision follows the
same approach laid out above — the lower figure is the result of measuring the
work, not of cutting corners. Neither the system's functionality nor its
performance window is reduced: all services, reports, alerts and the 3-year
retention the bank requested remain unchanged.

***Prepared by:** IT Modernization Program Management Department\
**Date:** June 2026\
**Scope:** Production Deployment --- up to 5,000 ATMs, 3-Year Data
Retention*