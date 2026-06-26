# System Architecture

> A one-page mental model of the Dashen Bank ATM Monitoring System.
> Read this before touching anything if you're new to the project —
> it explains what each container does, where its data comes from,
> and where its data goes.

---

## The Big Picture

This system monitors ATMs using 6 open source tools, each handling a
different part of the BRD's requirements. Right now it monitors 5
**simulated** ATMs (Python programs that behave like real ATM hardware
and transaction systems). When real ATMs are connected, only the data
sources change — the tools, dashboards, alerts, and reports stay the
same. See `docs/atm-onboarding.md` for that process.

```
┌─────────────────────────────────────────────────────────────┐
│  DATA GENERATION (currently simulated, will be real ATMs)    │
│                                                                │
│  atm-sim-00X      → hardware status (HTTP API)                │
│  atm-ej-00X       → Electronic Journal log files               │
│  txn-feed-00X     → transactions written to PostgreSQL          │
│  iso8583-gateway  → ISO 8583 transaction parsing                │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  COLLECTION & STORAGE                                         │
│                                                                │
│  Zabbix server    → polls hardware status every 30s            │
│  Filebeat         → ships EJ logs to OpenSearch                 │
│  PostgreSQL       → stores transactions + ATM locations          │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  ANALYSIS & ALERTING                                           │
│                                                                │
│  Zabbix triggers     → fire on thresholds (cash low, door open) │
│  anomaly-detector    → flags suspicious transaction patterns     │
│  network-correlator  → links network issues to txn failures      │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  PRESENTATION (what people actually look at)                   │
│                                                                │
│  Grafana          → dashboards, geo-map, drill-down              │
│  OS Dashboards    → EJ log search for dispute investigation       │
│  GLPI             → auto-created tickets, SLA, mobile              │
│  Report Portal    → PDF/Excel/CSV reports on demand                │
└─────────────────────────────────────────────────────────────┘
```

---

## Container-by-Container Reference

### Hardware Monitoring

| Container | What it does | Reads from | Writes to | Polled/used by |
|---|---|---|---|---|
| `atm-sim-001` … `atm-sim-005` | Simulates ATM hardware (cassettes, doors, printers, cameras, sensors) via an HTTP API on ports 1161–1165 | Internal Python state, randomized on a timer | Nothing persistent — in-memory only | Zabbix (HTTP agent items, every 30s) |
| `zabbix-server` | Polls all monitoring items, evaluates triggers, fires alerts | HTTP from `atm-sim-00X`, Zabbix Agent on host (ATM-001) | `zabbix-db` (PostgreSQL) | Grafana (via Zabbix data source), GLPI (via webhook) |
| `zabbix-web` | Web UI for Zabbix configuration | `zabbix-db` | — | Browser (port 8080) |
| Zabbix Agent (on host, not containerized) | Represents ATM-001 as a "local machine"; reports real laptop uptime/memory | Host OS | Sent to `zabbix-server` | Zabbix (active/passive checks) |

### Transactions

| Container | What it does | Reads from | Writes to | Used by |
|---|---|---|---|---|
| `txn-feed-001` … `txn-feed-005` | Simulates structured ATM transactions (withdrawals, balance checks, declines) | Internal randomization | `atm_transactions` table in PostgreSQL | Grafana, Report Portal |
| `iso8583-gateway` | Parses ISO 8583 messages (simulated now; will connect to the real ATM switch in production) | Internal simulation, or real TCP connection from switch (`MODE=tcp`) | `atm_transactions` table, tagged `source=ISO8583_SIM` or `ISO8583_REAL` | Grafana, Report Portal |
| `postgres` (container: `zabbix-db`) | Stores both Zabbix's own data AND the custom `atm_transactions` / `atm_locations` tables | All of the above | Disk volume `pgdata` | Grafana, Report Portal, pgAdmin |

### Electronic Journal (EJ) Logs

| Container | What it does | Reads from | Writes to | Used by |
|---|---|---|---|---|
| `atm-ej-001` … `atm-ej-005` | Simulates EJ log lines (NCR-style format, masked card numbers) | Internal randomization | `.log` files in `ej-logs/` (host-mounted) | Filebeat |
| `filebeat` | Watches `ej-logs/*.log`, ships new lines to OpenSearch | `ej-logs/` directory | OpenSearch index `atm-electronic-journal` | OpenSearch Dashboards |
| `opensearch` | Stores and indexes EJ log data for full-text search | Filebeat | Disk volume `opensearch-data` | OpenSearch Dashboards |
| `opensearch-dashboards` | Web UI for searching EJ logs | OpenSearch | — | Browser (port 5601), operations staff investigating disputes |

### Intelligence Layer

| Container | What it does | Reads from | Writes to | Used by |
|---|---|---|---|---|
| `anomaly-detector` | Scans `atm_transactions` for suspicious patterns (rapid repeat withdrawals, decline rate spikes) | PostgreSQL | Zabbix item value (`Unacknowledged Anomalies`) | Zabbix, Grafana |
| `network-correlator` | Correlates network metric spikes with transaction failure spikes | PostgreSQL + Zabbix history | Zabbix item value | Zabbix, Grafana |

### Presentation Layer

| Container | What it does | Reads from | Accessed via |
|---|---|---|---|
| `grafana` | Dashboards: ATM Operations Centre, geo-map, per-ATM drill-down | Zabbix data source, PostgreSQL data source, OpenSearch data source | Browser (port 3001) |
| `grafana-renderer` | Renders Grafana panels to PDF for export | Grafana | Internal only |
| `glpi` + `glpi-db` | Ticketing, SLA tracking, mobile app access | Zabbix webhook (auto-creates tickets), manual entry | Browser (port 8082), GLPI mobile app |
| `report-portal` | Web form to generate PDF/Excel/CSV reports on demand | PostgreSQL (`atm_transactions`, `atm_locations`) | Browser (port 8888) |
| `pgadmin` | Direct database browser/query tool | PostgreSQL | Browser (port 5050) |

---

## Where Does Each Number Come From? (Quick Answers)

| You see this in Zabbix/Grafana | It actually comes from |
|---|---|
| ATM Operational Status, hardware sensors, cassette levels | `atm-sim-00X` HTTP API — fake but follows the same SNMP-OID-like pattern real ATMs would use |
| ATM uptime, Available memory | **Your real laptop** (Zabbix Agent) — shown once per ATM host name, same value each time. Disabled per `docs/local-progress-plan.md` Priority 1 if not already done. |
| Unacknowledged Anomalies | `anomaly-detector` querying PostgreSQL for flagged transaction patterns |
| Transactions in Grafana/Report Portal | `txn-feed-00X` (simulated) + `iso8583-gateway` (simulated, tagged `ISO8583_SIM`) |
| EJ search results in OpenSearch Dashboards | `atm-ej-00X` → `ej-logs/*.log` → Filebeat → OpenSearch |
| GLPI tickets | Zabbix trigger fires → webhook → GLPI API creates ticket automatically |

---

## Key Design Principle

**Every simulator is a placeholder for a real data source.** The
database schema, dashboards, alert rules, ticketing integration, and
report portal do not change when real ATMs are connected — only where
the data originates changes:

| Data | Simulated source | Real source (production) |
|---|---|---|
| Hardware status | `atm-sim-00X` HTTP API | Real ATM SNMP agent |
| EJ logs | `atm-ej-00X` generated files | Real ATM EJ files (via agent, share, or SFTP) |
| Transactions | `txn-feed-00X` + simulated ISO 8583 | Real ATM switch via `iso8583-gateway` in `MODE=tcp` |
| ATM locations | Manually inserted SQL rows | Populated via admin form or bulk CSV import |
| Zabbix hosts | 5 hosts created manually | Auto-discovery + auto-registration |

Full detail on each of these transitions is in the **Production
Migration Guide** and `docs/atm-onboarding.md`.

---

## Key File Locations

| Path | What's there |
|---|---|
| `docker-compose.yml` | Defines every container, port, and environment variable |
| `simulators/` | Python source for `atm-sim`, `atm-ej`, `txn-feed` |
| `camel/iso8583_gateway.py` | ISO 8583 parser/simulator |
| `report-portal/app.py` | Report Portal Flask application |
| `filebeat.yml` | Filebeat input config (what log paths it watches) |
| `ej-logs/` | EJ log files (host-mounted into Filebeat + `atm-ej-00X`) |
| `config/zabbix/` | Exported Zabbix template/hosts/actions XML |
| `config/grafana/dashboards/` | Exported Grafana dashboard JSON |
| `config/postgres/` | Database backups |
| `scripts/` | Setup, backup, and restore scripts |
| `/etc/zabbix/zabbix_agent2.conf` | Real Zabbix Agent config (on host, not in repo) |

---

## See Also

- `docs/troubleshooting.md` — fixes for common failures
- `docs/atm-onboarding.md` — checklist for connecting a real ATM
- `README.md` — full setup instructions for a new machine