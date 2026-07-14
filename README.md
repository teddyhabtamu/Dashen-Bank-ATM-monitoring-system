# Dashen Bank ATM Monitoring System

## Prerequisites
- Ubuntu 22.04 LTS (or WSL2 on Windows)
- Git
- Internet connection for first setup
- Docker + Docker Compose

---

## Step 1 — Clone the repo

```bash
git clone YOUR_GITHUB_URL
cd Dashen-Bank-ATM-monitoring-system
```

> **Note:** The folder name becomes the Docker Compose project prefix
> (e.g. `dashen-bank-atm-monitoring-system_default` network name).
> If you rename the folder later, container names stay the same but
> the network name changes — this affects gateway IPs used in Step 6.

---

## Step 2 — Run setup script

```bash
bash scripts/setup_new_machine.sh
```

This takes 5–10 minutes. It:
- Checks Docker is installed
- Verifies all required project files exist
- Creates required directories (`ej-logs`, `reports`, `config/`)
- Builds all Docker images
- Starts all services (`docker compose up -d`)
- Restores the PostgreSQL database from backup

Verify everything is running:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v "Exited"
```

---

## Step 3 — Import Zabbix configuration

1. Open `http://localhost:8080` (`Admin` / `zabbix`)
2. **Data collection → Templates → Import**
   → select `config/zabbix/zbx_export_templates.xml` (NCR)
   → then `config/zabbix/template_grg.xml` (GRG)
3. **Data collection → Hosts → Import**
   → select `config/zabbix/zbx_export_hosts.xml`
4. **Alerts → Media types → Import**
   → select `config/zabbix/zbx_export_mediatypes.xml`
   (includes the **GLPI Ticket** webhook)
5. **Create the trigger action** (auto-creates GLPI tickets):
   ```bash
   python3 scripts/setup_zabbix_actions.py
   ```
   > Zabbix 6.4 cannot export/import **actions** as XML, so this step is
   > a small idempotent API script instead of a file import. It links
   > ATM triggers (severity ≥ High) to the GLPI Ticket media type. Safe
   > to re-run — it skips if the action already exists.

> **Ticket lifecycle (RCA):** a fired trigger auto-creates a GLPI
> ticket; when the alert clears, the ticket is moved to **Pending** with
> a recovery note — it is **not** auto-closed. An engineer must document
> a Root Cause Analysis (RCA) and record a solution before closing, per
> the BRD's incident-management requirement.

---

## Step 4 — Import Grafana dashboards

Grafana auto-loads datasources from `config/grafana/datasources.yml`
and dashboards from `config/grafana/dashboards/`.

If dashboards did not load automatically:
1. Open `http://localhost:3002` (`admin` / `dashen2024`)
2. **Dashboards → Import → Upload JSON file**
   → import each file from `config/grafana/dashboards/`

---

## Step 5 — Configure GLPI webhook

1. Open `http://localhost:8082` (`glpi` / `DashenGLPI2024`)
2. **Setup → General → API** → Enable REST API
3. **Add API client** → copy the App Token
4. In Zabbix → **Administration → Media types → GLPI Ticket**
   → update the `app_token` parameter with the new token

---

## Step 6 — Install and configure the Zabbix Agent (on the host/WSL)

ATM-001 uses a real Zabbix Agent installed directly on the host
(WSL Ubuntu), not a container — it represents the "local machine"
ATM. ATM-002 through ATM-005 use HTTP agent items against the
simulator containers and do **not** need this.

### 6.1 — Install the Zabbix repository and agent package

```bash
# Add the Zabbix 6.4 repo for Ubuntu 22.04
wget https://repo.zabbix.com/zabbix/6.4/ubuntu/pool/main/z/zabbix-release/zabbix-release_6.4-1+ubuntu22.04_all.deb
sudo dpkg -i zabbix-release_6.4-1+ubuntu22.04_all.deb
sudo apt update

# Install Zabbix Agent 2
sudo apt install -y zabbix-agent2
```

### 6.2 — Verify installation

```bash
zabbix_agent2 --version
sudo systemctl status zabbix-agent2
```

At this point the agent is installed but not yet configured to talk
to your Dockerized Zabbix server — continue to Step 6.3.

---

## Step 7 — Fix Zabbix Agent connectivity (WSL/Docker networking)

This step is needed whenever:
- You set up on a new machine
- Your laptop reboots and WSL assigns a new internal IP
- The project folder is renamed (changes the Docker network name)

### 7.1 — Check current network details

```bash
# Your WSL/host IP (used by Zabbix server to reach the agent)
ip addr show eth0 | grep 'inet '

# Docker network gateway IP (used in ServerActive)
docker network inspect <project-name>_default | grep -i gateway
# e.g. docker network inspect dashen-bank-atm-monitoring-system_default | grep -i gateway
```

### 7.2 — Update the Zabbix agent config

```bash
sudo nano /etc/zabbix/zabbix_agent2.conf
```

Set these two lines:

```
Server=127.0.0.1,172.16.0.0/12
ServerActive=<GATEWAY_IP_FROM_7.1>:10051
Hostname=ATM-001-Addis-Branch
```

> `172.16.0.0/12` covers the entire Docker private range
> (172.16.0.0–172.31.255.255), so it works regardless of which
> subnet Docker/WSL assigns — this only needs to be set once.

Restart the agent:
```bash
sudo systemctl restart zabbix-agent2
sudo systemctl status zabbix-agent2 | grep Active
```

Reload Zabbix server's config cache:
```bash
docker exec -it zabbix-server zabbix_server -R config_cache_reload
```

### 7.3 — Test connectivity from inside the Zabbix server container

```bash
docker exec zabbix-server zabbix_get -s <WSL_ETH0_IP> -p 10050 -k agent.ping
```

Replace `<WSL_ETH0_IP>` with the IP from step 7.1 (e.g. `172.28.26.171`).
A successful response returns `1`.

### 7.4 — Update the ATM-001 host interface in Zabbix

1. **Configuration → Hosts → ATM-001 | NCR | Addis Ababa Main Branch → Interfaces**
2. Set the Agent interface IP to `<WSL_ETH0_IP>` (from step 7.1)
3. Click **Update**
4. Wait ~1 minute, then confirm the host shows green **ZBX** in
   **Configuration → Hosts**

> ATM-002 through ATM-005 use HTTP agent items pointing directly at
> the simulator containers and are not affected by this — only the
> Zabbix Agent–based checks on ATM-001 need this fix.

---

## Step 8 — Verify OpenSearch Stack and Electronic Journal (EJ) log search

The EJ generator containers (`atm-ej-001` through `atm-ej-005`) write
transaction-style log files that Filebeat ships to OpenSearch for
search in OpenSearch Dashboards. These are **separate** from `atm-sim-00X`
(hardware metrics) and `txn-feed-00X` (PostgreSQL transactions).

### 8.1 — Fix `ej-logs/` directory ownership

After cloning on a new machine this directory is often created as
`root:root` by Docker, which blocks the EJ generator containers
(running as non-root) from writing:

```bash
sudo chown -R $USER:$USER ej-logs/
chmod 755 ej-logs/
```

### 8.2 — Fix Filebeat config file permissions

Filebeat refuses to start if `filebeat.yml` is not owned by root:

```bash
sudo chown root:root filebeat.yml
sudo chmod 644 filebeat.yml
docker compose restart filebeat
```

### 8.3 — Verify EJ generator services are in docker-compose.yml

The `atm-ej-001` through `atm-ej-005` services are now committed
in `docker-compose.yml`. Verify they are present:

```bash
grep -c "atm-ej-" docker-compose.yml
# Expected: 5
```

If the count is `0`, your clone predates this fix. Run:
```bash
git pull
```

### 8.4 — Build and start EJ generators

```bash
docker compose build atm-ej-001
docker compose up -d atm-ej-001 atm-ej-002 atm-ej-003 atm-ej-004 atm-ej-005

# First run generates 500 backfill entries per ATM (~1-2 min)
sleep 60

ls -la ej-logs/
wc -l ej-logs/*.log
```

### 8.5 — Confirm all OpenSearch/EJ containers running

```bash
docker ps --format "table {{.Names}}\t{{.Status}}" \
  | grep -E "atm-ej|atm-sim|filebeat|opensearch"
```

You should see 5x `atm-ej-00X`, 5x `atm-sim-00X`, `filebeat`,
`opensearch`, and `opensearch-dashboards` all `Up`.

### 8.6 — Confirm data reaches OpenSearch

```bash
sleep 30
curl -s "http://localhost:9200/_cat/indices?v"
```

You should see `atm-electronic-journal` index with
non-zero `docs.count`.

### 8.7 — Create the OpenSearch Dashboards Index Pattern

1. Open `http://localhost:5601`
2. **Stack Management → Index Patterns → Create index pattern**
3. Index pattern: `atm-electronic-journal`
4. Timestamp field: `@timestamp`
5. Save

Then **Discover** → select the data view → set time range to
**Last 7 days** (the default "Last 15 minutes" often shows nothing).

Search examples for dispute investigation:
- `ATM-003` — all activity at Merkato Branch
- `DECLINED` — all declined transactions across ATMs
- `card_masked: "************1234"` — all activity for one card

---

## Step 9 — Backup and restore database

### Backup (run before switching machines)
```bash
bash scripts/backup_db.sh
```
Saves `atm_locations` and `atm_transactions` to `config/postgres/`.

### Restore (run on new machine, after Step 2)
```bash
bash scripts/restore_db.sh
```
Drops and recreates `atm_locations` and `atm_transactions` from backup,
then prints row counts to verify.

---

## Service URLs

| Service | URL | Login |
|---|---|---|
| Zabbix | http://localhost:8080 | Admin/zabbix |
| Grafana | http://localhost:3002 | admin/dashen2024 |
| OpenSearch Dashboards | http://localhost:5601 | admin / admin |
| GLPI | http://localhost:8082 | glpi/DashenGLPI2024 |
| Report Portal | http://localhost:8888 | see below |
| pgAdmin | http://localhost:5050 | admin@dashenbank.com/dashen2024 |

### Report Portal Users

| Role | Username | Password | Permissions |
|------|----------|----------|-------------|
| **Admin** | `admin` | set in `.env` (`ADMIN_PASS`) | Full access — ATMs, reports, schedules, audit log, EJ search |
| **Operator** | `operator` | `operator123` | Manage ATMs, EJ search, reports (no schedules or audit) |
| **Viewer** | `viewer` | `viewer123` | Read-only — dashboard, ATM list, EJ search, reports |

To add more users, log in as **admin** and visit **Admin → User Management** in the sidebar. You can create, delete, and reset passwords for operator/viewer/admin accounts through the web UI.

### Managing ATMs from the Portal

Admins and operators manage the fleet under **Admin → ATMs** (`/admin/atm`):

- **Add / edit ATMs** — the **Name** field is optional (defaults to the
  branch name when blank). **Vendor** is a dropdown (NCR / GRG plus any
  vendors already in the database) with a "+ Add new vendor…" option.
- **Auto port allocation** — each new or imported ATM is automatically
  assigned a free `sim_port` (range `1161-1260`), so the simulator picks
  it up without a manual restart.
- **CSV import** — bulk-import ATMs; see `sample_atm_import.csv` for the
  format. Rows with an unknown vendor still import but are flagged with a
  warning (they are simulated with the NCR schema).
- **State vs. status** — the list and detail pages show each ATM's live
  **current state** (or a *Retired* tag when inactive), and the detail
  view is Dashen-branded for export. The card view is mobile-friendly
  with a **View** action available to all roles.

---

## Windows-only: Expose ports for phone/other devices

In **Admin PowerShell** (re-run after every reboot, or use the
scheduled task described in chat history):

```powershell
$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
netsh interface portproxy reset
netsh interface portproxy add v4tov4 listenport=8080 listenaddress=0.0.0.0 connectport=8080 connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=8082 listenaddress=0.0.0.0 connectport=8082 connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=3002 listenaddress=0.0.0.0 connectport=3002 connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=5601 listenaddress=0.0.0.0 connectport=5601 connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=9200 listenaddress=0.0.0.0 connectport=9200 connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=8888 listenaddress=0.0.0.0 connectport=8888 connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=5050 listenaddress=0.0.0.0 connectport=5050 connectaddress=$wslIp

New-NetFirewallRule -DisplayName "WSL ATM Services" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080,3002,5601,8082,8888,9200,5050 -Profile Any
```

---

## Known Manual Steps (cannot be automated)

1. **GLPI App Token** — unique per installation; regenerate and update
   the Zabbix `GLPI Ticket` media type parameter (Step 5).
2. **GLPI installer wizard** — must be clicked through once on a
   fresh install (`http://localhost:8082`).
3. **Zabbix Agent networking (Step 7)** — IP ranges depend on the
   host machine's network; the `/12` CIDR minimizes how often this
   needs revisiting, but the interface IP for ATM-001 must still be
   set per machine.
4. **EJ generator permissions (Step 8)** — `ej-logs/` ownership
   and `filebeat.yml` ownership are host-filesystem properties not
   captured by `git`, and must be fixed on each new machine (steps
   8.1–8.2). The `atm-ej-00X` services are now committed to
   `docker-compose.yml`.


   ## Star History

<a href="https://www.star-history.com/?repos=teddyhabtamu%2FDashen-Bank-ATM-monitoring-system&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=teddyhabtamu/Dashen-Bank-ATM-monitoring-system&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=teddyhabtamu/Dashen-Bank-ATM-monitoring-system&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=teddyhabtamu%2Fdashen-bank-atm-monitoring-system&type=date&legend=top-left" />
 </picture>
</a>
