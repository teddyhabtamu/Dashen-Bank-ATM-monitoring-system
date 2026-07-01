# *Real-Time ATM Monitoring Tool and Reporting System* {#real-time-atm-monitoring-tool-and-reporting-system .unnumbered}

## *Server Infrastructure Specification* {#server-infrastructure-specification .unnumbered}

### *Production (LIVE)*

+-----------+---+----+--------+-----+-------+-----------------------+
| **Tool /  | * | *  | **S    | **  | *     | -   **Notes**         |
| S         | * | *R | torage | VMs | *Oper |                       |
| ervices** | v | AM | (NVMe  | Ne  | ating |                       |
|           | C | (G | SSD)** | ede | Sys   |                       |
|           | P | B) |        | d** | tem** |                       |
|           | U | ** |        |     |       |                       |
|           | * |    |        |     |       |                       |
|           | * |    |        |     |       |                       |
+-----------+---+----+--------+-----+-------+-----------------------+
| Zabbix    | 1 | 32 | 200 GB | 1   | RHEL  | -   Production        |
| Server +  | 6 |    |        |     | 9     |     monitoring        |
| Zabbix    |   |    |        |     |       |     platform for all  |
| Web       |   |    |        |     |       |     ATM devices,      |
|           |   |    |        |     |       |     alerting,         |
|           |   |    |        |     |       |     escalation, and   |
|           |   |    |        |     |       |     integrations.     |
+-----------+---+----+--------+-----+-------+-----------------------+
| P         | 1 | 64 | 1 TB   | 1   | RHEL  | -   Dedicated         |
| ostgreSQL | 6 |    |        |     | 9     |     production        |
| (M        |   |    |        |     |       |     database for      |
| onitoring |   |    |        |     |       |     monitoring        |
| DB +      |   |    |        |     |       |     history,          |
| Tr        |   |    |        |     |       |     transactions,     |
| ansaction |   |    |        |     |       |     inventory,        |
| DB)       |   |    |        |     |       |     trends, and       |
|           |   |    |        |     |       |     reporting.        |
+-----------+---+----+--------+-----+-------+-----------------------+
| Elasti    | 1 | 48 | 2 TB   | 1   | RHEL  | -   Production        |
| csearch + | 2 |    |        |     | 9     |     Electronic        |
| Kibana +  |   |    |        |     |       |     Journal (EJ) log  |
| Filebeat  |   |    |        |     |       |     storage,          |
|           |   |    |        |     |       |     indexing, search, |
|           |   |    |        |     |       |     and analytics     |
|           |   |    |        |     |       |     platform.         |
+-----------+---+----+--------+-----+-------+-----------------------+
| Grafana + | 8 | 24 | 200 GB | 1   | RHEL  | -   Dashboards,       |
| GLPI +    |   |    |        |     | 9     |     reporting,        |
| Report    |   |    |        |     |       |     ticketing, SLA    |
| Portal +  |   |    |        |     |       |     management, and   |
| Grafana   |   |    |        |     |       |     management        |
| Renderer  |   |    |        |     |       |     reporting.        |
+-----------+---+----+--------+-----+-------+-----------------------+
| ISO 8583  | 8 | 16 | 100 GB | 1   | RHEL  | -   Receives switch   |
| Gateway + |   |    |        |     | 9     |     transaction       |
| Anomaly   |   |    |        |     |       |     feeds, anomaly    |
| D         |   |    |        |     |       |     detection, and    |
| etector + |   |    |        |     |       |                       |
| Network   |   |    |        |     |       |   transaction-network |
| C         |   |    |        |     |       |     correlation.      |
| orrelator |   |    |        |     |       |                       |
+-----------+---+----+--------+-----+-------+-----------------------+

-   UAT Environment

+----------+---+----+--------+-----+-------+-----------------------+
| **Tool / | * | *  | **S    | **  | *     | -   **Notes**         |
| Se       | * | *R | torage | VMs | *Oper |                       |
| rvices** | v | AM | (NVMe  | Ne  | ating |                       |
|          | C | (G | SSD)** | ede | Sys   |                       |
|          | P | B) |        | d** | tem** |                       |
|          | U | ** |        |     |       |                       |
|          | * |    |        |     |       |                       |
|          | * |    |        |     |       |                       |
+==========+===+====+========+=====+=======+=======================+
| Zabbix + | 8 | 32 | 500 GB | 1   | RHEL  | -   Shared UAT        |
| Post     |   |    |        |     | 9     |     environment used  |
| greSQL + |   |    |        |     |       |     for testing       |
| G        |   |    |        |     |       |     monitoring        |
| rafana + |   |    |        |     |       |     templates,        |
| GLPI +   |   |    |        |     |       |     dashboards,       |
| Report   |   |    |        |     |       |     reports,          |
| Portal   |   |    |        |     |       |     ticketing         |
|          |   |    |        |     |       |     workflows, and    |
|          |   |    |        |     |       |     integrations.     |
+----------+---+----+--------+-----+-------+-----------------------+
| Elastic  | 8 | 32 | 500 GB | 1   | RHEL  | -   Shared UAT        |
| search + |   |    |        |     | 9     |     platform for EJ   |
| Kibana + |   |    |        |     |       |     log testing,      |
| ISO 8583 |   |    |        |     |       |     search            |
| Gateway  |   |    |        |     |       |     validation,       |
|          |   |    |        |     |       |     transaction feed  |
|          |   |    |        |     |       |     simulation, and   |
|          |   |    |        |     |       |     anomaly testing.  |
+----------+---+----+--------+-----+-------+-----------------------+

##  {#section .unnumbered}

## *Summary Totals*

  -------------------------------------------------------------------------------
  **Environement**   **VMs**       **vCPU**      **RAM**            **Storage**
  ------------------ ------------- ------------- ------------------ -------------
  Production         5             60            184 GB             3.5 TB

  UAT                2             16            48 GB              1 TB

  Total              7 VMs         76 vCPU       232 GB             4.5 TB
  -------------------------------------------------------------------------------

***Prepared by:** IT Modernization Program Management Department\
**Date:** June 2026\
**Scope:** Production Deployment --- 2,300--2,700 ATMs, 3-Year Data
Retention*
