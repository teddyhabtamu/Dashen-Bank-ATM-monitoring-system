# Dashen Bank — ATM Monitoring System
# UAT Migration Guide v2 (2026-08-20)

## From one laptop to two servers — a step-by-step cookbook

**Companion:** `docs/Production_Migration_Guide_v2.md` (Production Migration Guide v3)
**Scope:** UAT environment — RHEL 9, **2 VMs**, **2 real test ATMs** collected over **real SNMP** (vendor OIDs)
**Purpose:** Install, configure, and customize the system on the UAT servers exactly as it will later run in production.

---

# 0. What changed and why

| | Today (laptop) | UAT (2 servers) | Production (3 servers) |
|---|---|---|---|
| Where it runs | 1 laptop, 1 `docker-compose.yml` | UAT-01 + UAT-02 | APPS-01 + DATA-01 + GWY-01 |
| How services talk | container names (Docker DNS) | same-VM = container name; **cross-VM = IP** | same idea |
| How ATMs are polled | HTTP agent (simulators) | **SNMP agent** (real test ATMs, vendor OIDs) | SNMP agent (real ATMs) |
| Why | PoC | prove the multi-server architecture | run the bank |

The important UAT work is three-fold:
1. **Split the one compose file into two** (already done — the files are at the repo root: `docker-compose.yml` (VM1 / apps+data), `docker-compose.gateway.yml` (VM2 / search+gateway), and `.env.uat` for variables).
2. **Collect from real ATMs over SNMP** — UAT polls the bank's 2 real test ATMs via SNMP (UDP 161, vendor community/OIDs captured in `config/snmp/`). This is exactly the production mechanism; the only difference is scale. No simulators run in UAT.
3. **Analysis services read from Zabbix, not simulators** — `state-manager` derives `atm_current_state` from each ATM's Zabbix item last values (`atm.status`, `atm.cash.*`, `atm.card.reader`, `atm.net.link`); `network-correlator` pulls real `atm.net.latency`/`atm.net.packetloss`/`atm.net.link` metrics from Zabbix (synthetic metrics only if `NET_SIMULATE=1`). `anomaly-detector` is already DB-driven (reads `atm_transactions`). All three require the real ATMs to be present as Zabbix hosts named by their `atm_id`.

> Two things are handled by other departments (verify only): VM provisioning, VLANs, security groups, and firewall rules. You only **install, configure, and customize** — then check the connections work.

---

# 1. Before you start (prerequisites)

- [ ] **Two UAT servers provisioned** (confirmed with IT):

  | Server | Name | VLAN / Network | IP |
  |---|---|---|---|
  | UAT-01 (app) | dbtcatmmonappuat | 172.22.10.0/24 (VLAN/gw — confirm with IT) | 172.22.10.45 |
  | UAT-02 (data) | dbtcatmmondbuat | 172.22.10.0/24 (VLAN/gw — confirm with IT) | 172.22.10.60 |

  Sizes: UAT-01 = 4 vCPU / 8 GB / 500 GB; UAT-02 = 4 vCPU / 8 GB / 200 GB.
- [ ] **SSH access** via the bastion (confirmed working):

  ```bash
  ssh tewodros@196.189.126.63 -p 1006   # UAT-01 app  (dbtcatmmonappuat, 172.22.10.45)
  ssh tewodros@196.189.126.63 -p 1007   # UAT-02 data (dbtcatmmondbuat, 172.22.10.60)
  ```

  > Wherever steps below show `ssh <your-user>@<internal-ip>`, use the
  > bastion mapping above for that VM.
- [ ] **RHEL 9** with subscription / repo access on both
- [ ] **GitHub SSH key or token** on both (to clone the repo)
- [ ] Firewall rules requested from Cloud team (use this table in the change request —
  return traffic on established connections is assumed):

  | # | Source | Destination | Port / Proto | Purpose | Required |
  |---|--------|-------------|--------------|---------|----------|
  | 1 | UAT-01 app (172.22.10.45) | UAT-02 data (172.22.10.60) | TCP 9200 | OpenSearch API — Filebeat EJ shipping, Report Portal EJ counts, health checks | Yes |
  | 2 | UAT-02 data (172.22.10.60) | UAT-01 app (172.22.10.45) | TCP 5432 | PostgreSQL — ISO 8583 gateway writes `atm_transactions` | Yes |
  | 3 | UAT-01 ↔ UAT-02 | both ways | TCP 22 | SSH between the VMs (admin, `scp` file copies during deploy) | Recommended |
  | 4 | UAT-01 (172.22.10.45) | Test ATMs 172.22.248.4, 172.23.248.6 | UDP 161 | SNMP polling of the 2 real test ATMs | Yes |
  | 5 | Test ATMs 172.22.248.4, 172.23.248.6 | UAT-01 (172.22.10.45) | UDP + TCP 514 | EJ syslog forwarding to Filebeat | Yes |
  | 6 | ISO 8583 switch | UAT-02 (172.22.10.60) | TCP 9876 | Real transaction feed into the gateway | Only if real ISO 8583 is in scope |
  | 7 | Admin laptop | UAT-01 (172.22.10.45) | TCP 8080, 3000, 8082, 8888 | Zabbix, Grafana, GLPI, Report Portal UIs | Yes |
  | 8 | Admin laptop | UAT-02 (172.22.10.60) | TCP 5601, 9200 | OpenSearch Dashboards + API checks | Yes |
  | 9 | Repo server 172.25.37.4 | Both UAT VMs | TCP 443, 80 | OS/package updates | Yes |

---

# 2. Step A — Install the tools on each server (Docker + git)

Run these on **both** UAT-01 and UAT-02.

```bash
ssh <your-user>@172.22.10.45        # on UAT-01
# --- and separately ---
ssh <your-user>@172.22.10.60          # on UAT-02
```

On each:

```bash
# 1. RHEL 9 does not ship Docker — remove podman (it conflicts) and add Docker's repo
sudo dnf remove -y podman buildah
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
exit                                   # log back in so the group applies

# 2. git + net-snmp (the latter is needed to verify ATM SNMP reachability later)
sudo dnf install -y git net-snmp-utils

# 3. OpenSearch needs a higher mmap limit — set it on BOTH (UAT-02 especially)
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
sudo swapoff -a
```

Verify on both:

```bash
docker --version && docker compose version && git --version
```

---

# 3. Step B — Get the code and set passwords

Do this on **both** servers (clone into the same path).

```bash
cd /opt
sudo mkdir -p atm-monitoring
sudo chown $USER:$USER atm-monitoring
git clone <YOUR_GITHUB_URL> atm-monitoring
cd /opt/atm-monitoring

# Copy the UAT env template and set real passwords (chmod 600 — it is secret)
cp .env.uat .env
chmod 600 .env
nano .env                               # fill every <...> placeholder with a strong password
```

### B.1 — Prepare the compose file for THIS server

The repo root ships **two** compose files, one per VM:

| File | Runs on |
|---|---|
| `docker-compose.yml` | UAT-01 (VM1 — Zabbix, GLPI, Grafana, Report Portal, anomaly-detector, network-correlator, state-manager, Filebeat, PostgreSQL) |
| `docker-compose.gateway.yml` | UAT-02 (VM2 — OpenSearch, OpenSearch Dashboards, ISO 8583 gateway) |

Each server only brings up its own stack. After cloning, **delete the other VM's file and rename yours to the default name** so both servers end up with a plain `docker-compose.yml` and use the identical command `docker compose up -d` (no `-f`, no env var).

On **UAT-01** (VM1):
```bash
rm -f docker-compose.gateway.yml          # drop the VM2 file; keep docker-compose.yml (VM1 stack)
```

On **UAT-02** (VM2):
```bash
rm -f docker-compose.yml                  # drop the VM1 file
mv docker-compose.gateway.yml docker-compose.yml   # rename to the default name
```

> **Note — if you `git pull` later:** the deleted file comes back; just re-run the
> matching `rm`/`mv` above. Prefer not to delete files? On UAT-02 instead run
> `export COMPOSE_FILE=docker-compose.gateway.yml` (add it to `~/.bashrc`) — then
> `docker compose up -d` also works without renaming. Both ways are fine.
> **Pull trap on UAT-02:** the rename above dirties the worktree, so a plain
> `git pull --ff-only` aborts once the remote touches either compose file.
> Before pulling there, run `git checkout -- docker-compose.yml
> docker-compose.gateway.yml`, pull, then redo the `rm`/`mv`. Always check
> `git status` + `git log --oneline -1` after pulling — a silent abort leaves
> stale code running with no error.

Generate 4 distinct passwords:

```bash
openssl rand -base64 16     # run 4 times
```

> `DB_PASS` in `.env` **must equal** `POSTGRES_PASSWORD` — the UAT-01 PostgreSQL and the UAT-02 gateway both use it.

---

# 4. Step C — Deploy UAT-02 (OpenSearch + OpenSearch Dashboards + ISO 8583 Gateway)

Do this **first** — UAT-01's Report Portal and Filebeat depend on OpenSearch.

```bash
ssh <your-user>@172.22.10.60
cd /opt/atm-monitoring

# Start OpenSearch + Dashboards (the gateway needs UAT-01's DB, so start it later in Step E)
docker compose up -d opensearch opensearch-dashboards
sleep 60
curl -s http://localhost:9200          # expect JSON with cluster_name + version
curl -s http://localhost:9200/_cat/indices?v

# C.1 — Load the EJ ingest pipeline + index template (REQUIRED, before Filebeat ships logs)
# Without this, EJ log lines are NOT parsed into structured fields and the Report Portal
# "EJ Search" feature returns unparsed/flat data. See scripts/setup_new_machine.sh Step 6b.
curl -s -X PUT "http://localhost:9200/_ingest/pipeline/atm_ej_parser" \
  -H 'Content-Type: application/json' -d @config/opensearch/atm_ej_parser.json \
  | grep -q '"acknowledged":true' && echo "ingest pipeline: OK" || echo "ingest pipeline: FAILED"
curl -s -X PUT "http://localhost:9200/_index_template/atm-ej-live" \
  -H 'Content-Type: application/json' -d @config/opensearch/ej_live_index_template.json \
  | grep -q '"acknowledged":true' && echo "index template: OK" || echo "index template: FAILED"

# OpenSearch Dashboards Dashen branding is applied by the compose mount (config/osd) —
# verify at http://172.22.10.60:5601 (logo + "Dashen Bank" in the header).
```

---

# 5. Step D — Deploy UAT-01 (PostgreSQL + Zabbix + Grafana + GLPI + analysis services)

```bash
ssh <your-user>@172.22.10.45
cd /opt/atm-monitoring

# Create the local dirs the compose mounts
mkdir -p ej-logs config/grafana/dashboards
sudo mkdir -p /data/real-ej-logs && sudo chown $USER:$USER /data/real-ej-logs

# 1. PostgreSQL first
docker compose -f docker-compose.yml up -d postgres
for i in {1..30}; do
  docker exec zabbix-db pg_isready -U zabbix -d zabbix >/dev/null 2>&1 && break
  echo "waiting for PostgreSQL ($i/30)"; sleep 3
done

# 2. Create the UAT tables + seed the 2 test ATMs
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/uat_tables.sql
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/seed_uat_atms.sql
docker exec zabbix-db psql -U zabbix -d zabbix -c "\dt"     # confirm the custom tables exist
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT atm_id, vendor, ip_address FROM atm_locations;"
# Export the CSV the Zabbix sync script reads (gitignored, regenerated here)
docker exec zabbix-db psql -U zabbix -d zabbix -c "COPY atm_locations TO STDOUT WITH CSV HEADER" > config/postgres/atm_locations.csv

# 3. Let the UAT-02 gateway reach this DB over the network
docker exec zabbix-db bash -c "echo \"listen_addresses = '*'\" >> /var/lib/postgresql/data/postgresql.conf"
docker exec zabbix-db bash -c "echo 'host all all 172.22.10.0/24 md5' >> /var/lib/postgresql/data/pg_hba.conf"
docker compose -f docker-compose.yml restart postgres

# 4. Build the images that come from this repo, then start everything
# (UAT has no simulator engines — only the analysis/portal services build locally)
docker compose -f docker-compose.yml build \
  state-manager anomaly-detector network-correlator report-portal
docker compose -f docker-compose.yml up -d
sleep 60

# 5. Confirm all containers are Up
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v Exited
```

# 5b. Match the Zabbix host to the agent (self-monitoring)
The compose sets the agent hostname to `Zabbix-Server-UAT01`, but Zabbix ships
a host named `Zabbix server` — until renamed, active checks log `host not
found` and passive checks are rejected. Via Zabbix API (or UI):
- Rename host `Zabbix server` → technical name `Zabbix-Server-UAT01`.
- Set its agent interface IP to `172.22.10.45` (the server container reaches
  the host-networked agent via the LAN IP, not `127.0.0.1`).
- The compose already allows the bridge subnet (`ZBX_SERVER_HOST` includes
  `172.18.0.0/16` — confirm with `docker network inspect atm-monitoring_default`).
Verify: Monitoring → Latest data for `Zabbix-Server-UAT01` shows fresh values
and `agent.ping` = 1.

> **Report Portal external links** are driven by `.env.uat`: `GRAFANA_URL`,
> `ZABBIX_UI_URL`, and `GLPI_UI_URL` (already wired into the compose). The top bar
> shows **Live Dashboards → Grafana**, **Monitoring Center → Zabbix**, and
> **Incident Management → GLPI**. After deploy, open Report Portal and confirm each
> link resolves to the correct UAT URL.

**Critical UAT check — prove SNMP works** (this is the production-accurate path):

```bash
snmpwalk -v2c -c dashen_sim -On 127.0.0.1:1161 .1.3.6.1.4.1.99999.8
snmpwalk -v2c -c dashen_sim -On 127.0.0.1:1162 .1.3.6.1.4.1.99999.9
# Expect a row per OID, e.g. .1.3.6.1.4.1.99999.8.1.0 = INTEGER: 1
```

If empty: `docker logs atm-sim-engine --tail 50` — look for `[SNMP] ... responder on UDP :1161`.

---

# 6. Step E — Start the ISO 8583 gateway (on UAT-02, needs UAT-01's DB)

```bash
ssh <your-user>@172.22.10.60
cd /opt/atm-monitoring
docker compose up -d iso8583-gateway
sleep 15
docker logs iso8583-gateway --tail 5        # "MODE: SIMULATION", generating transactions
```

Verify transactions landed on UAT-01:

```bash
ssh <your-user>@172.22.10.45
docker exec zabbix-db psql -U zabbix -d zabbix -c \
  "SELECT source, COUNT(*) FROM atm_transactions GROUP BY source;"
# Expect source = 'ISO8583_SIM', count growing
```

---

# 7. Step F — Verify the network (the part other teams set up)

These must succeed (if not, open a ticket with the Network/Cloud team):

```bash
# From UAT-01: reach UAT-02's OpenSearch
curl -s http://172.22.10.60:9200/_cat/indices?v

# From UAT-02: reach UAT-01's PostgreSQL
pg_isready -h 172.22.10.45 -U zabbix -d zabbix      # (or: psql -h 172.22.10.45 ...)

# From your laptop: the web UIs on UAT-01
#   http://172.22.10.45:8080  Zabbix
#   http://172.22.10.45:3000  Grafana
#   http://172.22.10.45:8082  GLPI
#   http://172.22.10.45:8888  Report Portal

# From UAT-01: reach the two real test ATMs (SNMP UDP 161) — 4 Kilo Building
#   NCR ATM           172.22.248.4
#   GRG ATM Terminal  172.23.248.6
snmpwalk -v2c -c <ATM_COMMUNITY> 172.22.248.4 .1.3.6.1.2.1.1
snmpwalk -v2c -c <ATM_COMMUNITY> 172.23.248.6 .1.3.6.1.2.1.1
# If these time out, the Network/Cloud team must open UDP 161 from UAT-01
# (172.22.10.45) to 172.22.248.4 and 172.23.248.6.
```

---

# 8. Step G — Configure Zabbix (import + create ATM hosts)

Browse to **http://172.22.10.45:8080** (`Admin` / `zabbix`).

1. **Configuration → Templates → Import**
   - `config/zabbix/template_ncr_snmp.xml` (template `Dashen Bank ATM Hardware`)
   - `config/zabbix/template_grg_snmp.xml` (template `Dashen Bank ATM Hardware - GRG`)
2. **Configuration → Hosts → Import** → `config/zabbix/zbx_export_hosts.xml` (the VM's own Zabbix-agent host)
3. **Administration → Media types → Import** → `config/zabbix/zbx_export_mediatypes.xml`

> **UAT pilot = the 2 real test ATMs (§8.1), not simulators.** Create the two real-ATM SNMP hosts
> described in §8.1 (port 161, vendor OIDs from `config/snmp/oid_mapping.csv`). The simulator-host steps
> below are a **dev/PoC fallback only** — keep them solely for synthetic dashboard demos; do **not** use
> them for the UAT pilot.

> *Dev fallback (optional):* to stand up simulated SNMP hosts, create them as SNMP (not HTTP):
>
> 1. **Configuration → Hosts → Create host**
> 2. **Host name:** `ATM-001 | NCR | Sim`
> 3. **Host groups:** `Dashen Bank ATMs` (create if missing)
> 4. **Interfaces → Add → SNMP:** IP `127.0.0.1`, **Port `1161`**, SNMPv2, community `dashen_sim`
> 5. **Templates:** link `Dashen Bank ATM Hardware` (NCR) or `… - GRG`
> 6. **Macros:** `{$ATM_PORT}` = `1161`, `{$ATM_VENDOR}` = `NCR`
> 7. **Add**, then repeat for a few ATMs (ports `1162`, `1163`, …)

> After 1–2 min, **Monitoring → Latest data** for a host should show all ~30 items populated.

### 8.1 Register the 2 real test ATMs (4 Kilo Building)

Two real test ATMs are monitored from UAT-01 to validate SNMP collection end-to-end:

| ATM (`atm_id`) | Vendor | IP | Subnet |
|---|---|---|---|
| ATM-TEST-NCR | NCR | 172.22.248.4 | 172.22.248.0/24 |
| ATM-TEST-GRG | GRG | 172.23.248.6 | 172.23.248.0/24 |

**Automated (preferred):** a fresh UAT DB is seeded with these two ATMs (`config/postgres/seed_uat_atms.sql`). Before syncing to Zabbix, set the real SNMP community (from ATM/Channel Support):
```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix -c \
  "UPDATE atm_locations SET snmp_community='<REAL_COMMUNITY>' WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');"
docker exec zabbix-db psql -U zabbix -d zabbix -c "COPY atm_locations TO STDOUT WITH CSV HEADER" > config/postgres/atm_locations.csv
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
```
The sync creates Zabbix hosts **named by `atm_id`** (this is the name `state-manager` and `network-correlator` query), with the correct IP, SNMP v2, and community.

**Manual alternative:** Configuration → Hosts → Create host → **Host name** `ATM-TEST-NCR` (must equal the `atm_id`), SNMP interface IP `172.22.248.4` port `161` v2 community `<REAL_COMMUNITY>`, link template `Dashen Bank ATM Hardware` (NCR); repeat for `ATM-TEST-GRG` (IP `172.23.248.6`, GRG template).

> The shipped SNMP templates use the **simulation** OID tree (`.1.3.6.1.4.1.99999`). Real NCR/GRG ATMs expose different vendor OIDs, so items read "Not supported" until re-mapped to the real vendor MIB (production guide Phase 4). Confirm reachability with `snmpwalk` (Step F) first.


---

### 8.2 Transactions (ISO 8583 gateway)

The `iso8583-gateway` (VM2) feeds `atm_transactions`, which drives the transaction dashboards, the anomaly detector, and the network-correlation engine.

* **Default (out-of-the-box for UAT):** `ISO8583_MODE=simulation` generates transaction events for every ATM in `atm_locations` (i.e. the 2 test ATMs). This exercises the full transaction → anomaly → correlation pipeline without needing the bank switch. These rows are tagged `source=ISO8583_SIM`.
* **Real transactions (when the bank provides the switch feed):** set in `.env.uat`:
  ```bash
  ISO8583_MODE=tcp
  SWITCH_HOST=0.0.0.0
  SWITCH_PORT=9876
  ```
  then re-create the container (`docker compose up -d iso8583-gateway`). The gateway listens on `SWITCH_PORT`; the bank must route the 2 test ATMs' ISO 8583 traffic to **UAT-02 (`172.22.10.60`) port 9876**, and the VM2 firewall must allow it from the switch subnet. Real rows are tagged `source=ISO8583_REAL`.

> Until the real switch feed is connected, transaction/anomaly dashboards will be empty or show only simulated events — that is expected, not a UAT failure.

---

# 9. Step H — Configure & customize the rest

**Grafana** (http://172.22.10.45:3000): the `datasources-uat.yml` already provisions the 3 datasources (Zabbix-ATM, ATM-Transactions, EJ-OpenSearch) — open **Connections → Data sources** and confirm each says "Success". All 6 dashboards should show live data from the real test ATMs.

**GLPI** (http://172.22.10.45:8082): complete the install wizard → **Setup → General → API** enable REST API and create an API client (app token) → **Setup → Users → `glpi`** copy the **user token** (different from the app token!) → in Zabbix **Administration → Media types → GLPi**: set `glpi_url` = `http://glpi/apirest.php` (container-internal — the Zabbix *server container* calls it, not your laptop), `glpi_token` = user token, add `glpi_app_token` = app token. **This GLPI mandates App-Token on every call**, so the stock webhook script must be patched to send `App-Token` on initSession + all requests (done on UAT-01 via API; verified with a create/read/delete round-trip). Then create four actions (**ATM Disaster/High/Average/Warning to GLPI**), each: trigger events only, condition host-group = ATMs AND matching severity, send to Admin via the GLPi media, plus the same as recovery operation (auto-solves the Problem with a followup). Verified live on UAT (drill PROBLEM → ticket → resolve → solved, then cleaned). Trigger a fault on a real test ATM (via its management interface) and confirm a ticket is created.

**Report Portal** (http://172.22.10.45:8888): generate a PDF/Excel/CSV report. EJ counts come from UAT-02's OpenSearch.

**EJ logs → OpenSearch:** `ej-logs/` on UAT-01 is shipped by Filebeat to UAT-02. Check:
```bash
ssh <your-user>@172.22.10.60
curl -s http://localhost:9200/_cat/indices?v     # atm-ej-live-YYYY.MM.DD present
```
Open OpenSearch Dashboards (http://172.22.10.60:5601), create index pattern `atm-ej-live-*` (time field `@timestamp`).

**Filebeat → OpenSearch (cross-VM):** the repo `filebeat.yml` ships EJ from UAT-01 to UAT-02's OpenSearch. For UAT it points at the VM2 IP:
```yaml
output.elasticsearch:
  hosts: ["172.22.10.60:9200"]
  index: "atm-ej-live-%{+yyyy.MM.dd}"
  pipeline: "atm_ej_parser"
```
Real test ATMs forward their EJ to Filebeat's syslog listener (`0.0.0.0:514`, UDP/TCP) on UAT-01; the `atm_ej_parser` pipeline tags each record with `ej_parsed` (true when the structured grok matched, false otherwise) and always retains the original line in `ej_raw`. **Phase 0 tuning:** capture one real ATM EJ line (`tcpdump -i any udp port 514 -A` on UAT-01) and extend the grok patterns in `config/opensearch/atm_ej_parser.json` for the NCR/GRG format; until then, search `ej_raw` / filter `ej_parsed:false` to see unparsed records.

---

# 10. Step I — Validate UAT (sign-off checklist)

- [ ] All containers `Up` on both servers
- [ ] `snmpwalk … 127.0.0.1:1161 ….99999.8` returns data
- [ ] Zabbix shows every item for each ATM host (no "Not supported")
- [ ] A real-ATM fault creates a Zabbix problem **and** a GLPI ticket
- [ ] Grafana: 6 dashboards live; 3 datasources test OK
- [ ] Report Portal generates all report types
- [ ] `ISO8583_SIM` rows growing in `atm_transactions`
- [ ] `atm-ej-live-*` indices populated; OpenSearch Dashboards searchable
- [ ] **EJ structured schema present** — `_ingest/pipeline/atm_ej_parser` and `_index_template/atm-ej-live` exist; EJ Search shows nested `transaction.*` / `atm.id` / `fault.*` fields (not a single flat string)
- [ ] **OpenSearch Dashboards branding** visible (Dashen logo/name), applied from `config/osd`
- [ ] **Report Portal links correct** — Live Dashboards/Monitoring Center/Incident Management point to the UAT Grafana/Zabbix/GLPI URLs from `.env.uat`
- [ ] **Card masking consistent** — every `card_masked` value is `************XXXX` (16 chars, last-4 only); no `4000************XXXX` legacy form
- [ ] Restart a VM → everything returns (`restart: unless-stopped`)

---

# 11. UAT exit criteria (before production)

Do **not** deploy production until: SNMP path proven, templates final, cross-VM links proven, gateway parser validated (if sample messages exist), **EJ ingest pipeline + index template loaded and verified**, **OpenSearch Dashboards branding + Report Portal external links verified**, GLPI ticketing proven, reporting proven, backup/restore rehearsed, and every UAT change committed to git. Full detail: `docs/Production_Migration_Guide_v2.md` §5 + §15.

---

# 12. Troubleshooting (UAT-specific)

| Symptom | Cause | Fix |
|---|---|---|
| `snmpwalk` to ATM fails | SNMP not enabled / wrong community / firewall | `snmpwalk -v2c -c <community> <atm-ip> .1.3.6.1.2.1.1`; verify community, VLAN/firewall UAT-01↔ATM |
| Zabbix item "Not supported" | HTTP/agent interface, or wrong port/community | SNMP interface, `{$ATM_PORT}`, community `dashen_sim`, correct vendor template |
| Report Portal can't reach OpenSearch | wrong `OS_HOST` or firewall | `curl http://172.22.10.60:9200` from UAT-01; check `OS_HOST: 172.22.10.60:9200` |
| Gateway crash-loops | `DB_HOST` wrong / PG not remote-ready | `docker logs iso8583-gateway`; verify `listen_addresses='*'` + `172.22.10.0/24` in `pg_hba.conf` |
| Filebeat ships nothing | `ej-logs/` perms or `filebeat.yml` ownership | `sudo chown -R $USER:$USER ej-logs/`; `sudo chown root:root filebeat.yml` |
| OpenSearch won't start | `vm.max_map_count` missing | `sudo sysctl -w vm.max_map_count=262144` (Step A.3) |
| GLPI 502 | wizard not finished / PHP timeout | finish wizard at :8082; restart GLPI container |

For the rest, the stack is identical to production — see `docs/Production_Migration_Guide_v2.md` §16.

---

# 13. File index

| Purpose | File |
|---|---|
| UAT-01 compose | `docker-compose.yml` |
| UAT-02 compose | `docker-compose.gateway.yml` |
| UAT env | `.env.uat` |
| UAT datasources | `config/grafana/datasources-uat.yml` |
| UAT Postgres tuning | `config/postgres-uat/postgresql-custom.conf` |
| SNMP templates | `config/zabbix/template_ncr_snmp.xml`, `template_grg_snmp.xml` |
| Simulator SNMP responder (dev fallback only) | `simulators/snmp_agent.py` |
| Filebeat config | `filebeat.yml` (output edited per environment) |
| Production reference | `docs/Production_Migration_Guide_v2.md` |

---

*End of guide. Last updated: 2026-08-20.*
