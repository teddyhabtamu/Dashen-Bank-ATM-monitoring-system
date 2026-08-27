# Dashen Bank ATM Monitoring System — UAT Deployment

> **Branch:** `uat-prep` · **Repository is now PRIVATE** (clone requires a GitHub SSH key or token).
> This branch deploys the UAT environment across **2 VMs** using real SNMP polling against simulated ATMs.
> For the full step-by-step cookbook, see [`docs/UAT_Migration_Guide.md`](docs/UAT_Migration_Guide.md);
> for the 45-day readiness plan see [`docs/uat-pilot-checklist.md`](docs/uat-pilot-checklist.md).

Monitors **1,209 ATMs** (797 GRG, 412 NCR) across **14 districts** in Ethiopia.
UAT proves the multi-server architecture end-to-end; ATMs are simulated but polled over **real SNMP**
(community `dashen_sim`, OID root `1.3.6.1.4.1.99999`) — the same mechanism production uses against real ATMs.

---

## UAT topology (2 VMs)

| Server | Name | IP | Runs |
|---|---|---|---|
| **UAT-01** (VM1) | DBHQUATATMMONAPP | 172.26.208.176 | PostgreSQL, Zabbix (server/web/agent), GLPI, Grafana, Report Portal, simulators (EJ/txn/state/anomaly/network-correlator), Filebeat |
| **UAT-02** (VM2) | DBHQUATATMMONDB | 172.26.21.50 | OpenSearch, OpenSearch Dashboards, ISO 8583 gateway |

Cross-VM wiring (set in `.env.uat`): VM1 → VM2 `9200` (OpenSearch); VM2 → VM1 `5432` (PostgreSQL).

---

## Quick Start (per VM)

```bash
# 1. Clone (private repo — needs SSH key / token)
git clone <your-github-url> atm-monitoring && cd atm-monitoring

# 2. Create the real env from the UAT template, then set strong passwords
cp .env.uat .env
chmod 600 .env
nano .env        # fill every <...> placeholder; DB_PASS must equal POSTGRES_PASSWORD

# 3. Prepare the compose file for THIS server (each VM runs only its own stack)
#    On UAT-01 (VM1):
rm -f docker-compose.gateway.yml
#    On UAT-02 (VM2):
rm -f docker-compose.yml
mv docker-compose.gateway.yml docker-compose.yml

# 4. Bring up (both servers then use the identical command)
docker compose up -d
```

> Why the `rm`/`mv` in step 3? The repo ships two root files — `docker-compose.yml` (VM1)
> and `docker-compose.gateway.yml` (VM2). After pruning the other VM's file and renaming yours
> to the default name, **both servers run plain `docker compose up -d`**. If you `git pull` later,
> the removed file returns — just re-run the matching `rm`/`mv`. (Alternative: on VM2 set
> `export COMPOSE_FILE=docker-compose.gateway.yml` in `~/.bashrc` and skip the rename.)

### Post-bring-up (run on UAT-01)
```bash
# Load DB schema, Zabbix templates/hosts, GLPI structure, OpenSearch EJ pipeline
bash scripts/setup_new_machine.sh

# Verify the whole chain (SNMP → Zabbix → GLPI → Grafana → Report Portal)
bash scripts/uat_preflight.sh
```

---

## Access

| Service | URL | Login |
|---|---|---|
| Zabbix | http://172.26.208.176:8080 | `ZBX_USER` / `ZBX_PASS` (change factory default) |
| Grafana | http://172.26.208.176:3000 | admin / `GRAFANA_ADMIN_PASSWORD` |
| GLPI | http://172.26.208.176:8082 | glpi / `GLPI_API_PASSWORD` |
| Report Portal | http://172.26.208.176:8888 | admin / `ADMIN_PASS` |
| OpenSearch Dashboards | http://172.26.21.50:5601 | admin / admin (security plugin disabled — restrict network) |
| OpenSearch (API) | http://172.26.21.50:9200 | — |

---

## Key scripts

| Script | Purpose |
|---|---|
| `scripts/setup_new_machine.sh` | Full provisioning: DB restore, Zabbix sync, GLPI setup, OSD pipeline + index pattern |
| `scripts/uat_preflight.sh` | Health check of the end-to-end chain before UAT sign-off |
| `scripts/sync_atms_to_zabbix.py --apply --import-templates` | Re-sync Zabbix hosts/templates |
| `scripts/import_atms_from_excel.py` | Seed the 1,209 ATMs from `docs/ATM INFORMATION.xlsx` |
| `scripts/load_osd_dashboards.sh` | Load the EJ dashboards into OpenSearch Dashboards |
| `tests/test_ej_parser.py` | Validates the OpenSearch EJ ingest pipeline definition |

---

## Ticket lifecycle (Zabbix → GLPI)

Zabbix triggers (severity ≥ High) auto-create GLPI tickets, routed by trigger name:

| Trigger Pattern → | Category | Assigned To | SLA |
|---|---|---|---|
| Cash Empty / Cash Out | Cash Out | Cash Operations Team | Critical (2hr) |
| Cash Low | Cash Low | Cash Operations Team | Medium (8hr) |
| Cash Jam / Card Reader / Printer / High Temp | Hardware Error | ATM Field Engineers | High (4hr) |
| Camera Fault / Camera Storage | Camera Failure | ATM Field Engineers | Medium (8hr) |
| Network Link Down / Packet Loss / Latency | Network Issue | IT Network Team | High (4hr) |
| Main Power / UPS Critical | Power / UPS Alert | ATM Field Engineers | High (4hr) |
| UPS Battery Low / UPS on Battery | Power / UPS Alert | ATM Field Engineers | Medium (8hr) |
| Cabinet Door / Safe Door / Intrusion / Vibration / Card Capture | Security Alert | ATM Ops Center | Critical (2hr) |
| Out of Service / Offline | ATM Offline | ATM Field Engineers | Critical (2hr) |
| Supervisor / Maintenance / Partial Service | ATM Offline | ATM Field Engineers | Medium (8hr) |
| Transaction Failure / Anomaly | Transaction Anomaly | ATM Ops Center | High (4hr) |
| Software Error | Software Error | ATM Field Engineers | High (4hr) |
| *(no match)* | Transaction Anomaly | ATM Ops Center | Medium (8hr) |

On recovery the ticket moves to **Pending** with an automated note — it is **not** auto-closed; an engineer documents RCA before closing.

### GLPI category tree
```
ATM Hardware Fault
  ├── NCR ATM Hardware         → Field Engineers, High SLA
  ├── GRG ATM Hardware         → Field Engineers, High SLA
  ├── Hardware Error           → Field Engineers, High SLA
  ├── Camera Failure           → Field Engineers, Medium SLA
  ├── Power / UPS Alert        → Field Engineers, High SLA
  └── ATM Offline              → Field Engineers, Critical SLA
ATM Network Issue  → Network Issue            → IT Network Team, High SLA
ATM Cash Issue     → Cash Low / Cash Out      → Cash Operations, Medium/Critical SLA
ATM Software Error → Software Error           → Field Engineers, High SLA
Transaction Anomaly                              → Ops Center, High SLA
Security Alert                                   → Ops Center, Critical SLA
```

---

## Credentials

| Role | Username | Password |
|---|---|---|
| Report Portal Admin | `admin` | `ADMIN_PASS` (from `.env`) |
| Zabbix Admin | `ZBX_USER` (default `Admin`) | **change the factory default after setup** |
| Grafana Admin | `admin` | `GRAFANA_ADMIN_PASSWORD` |
| GLPI Admin | `glpi` | `GLPI_API_PASSWORD` |
| OpenSearch | `admin` | `admin` (security plugin disabled — restrict network) |
| PostgreSQL | `zabbix` | `POSTGRES_PASSWORD` |

> Never commit real passwords. All credentials are set per-machine via `.env` (gitignored);
> the tracked `*.uat` / `*.example` files contain only placeholders.

## More documentation

- [`docs/UAT_Migration_Guide.md`](docs/UAT_Migration_Guide.md) — authoritative UAT runbook
- [`docs/uat-pilot-checklist.md`](docs/uat-pilot-checklist.md) — 45-day readiness checklist
- [`docs/Production_Migration_Guide_v2.md`](docs/Production_Migration_Guide_v2.md) — production cutover (references `deploy/production/*`, which live on the production branch, not here)
- `docs/architecture.md`, `docs/troubleshooting.md`, `config/snmp/` (real-ATM SNMP onboarding)
