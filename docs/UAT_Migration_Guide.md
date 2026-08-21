# Dashen Bank — ATM Monitoring System
# UAT Migration Guide v2 (2026-08-20)

## From one laptop to two servers — a step-by-step cookbook

**Companion:** `docs/Production_Migration_Guide_v2.md` (Production Migration Guide v3)
**Scope:** UAT environment — RHEL 9, **2 VMs**, 10–20 simulated ATMs collected over **real SNMP**
**Purpose:** Install, configure, and customize the system on the UAT servers exactly as it will later run in production.

---

# 0. What changed and why

| | Today (laptop) | UAT (2 servers) | Production (3 servers) |
|---|---|---|---|
| Where it runs | 1 laptop, 1 `docker-compose.yml` | UAT-01 + UAT-02 | APPS-01 + DATA-01 + GWY-01 |
| How services talk | container names (Docker DNS) | same-VM = container name; **cross-VM = IP** | same idea |
| How ATMs are polled | HTTP agent (simulators) | **SNMP agent** (simulators serve real SNMP) | SNMP agent (real ATMs) |
| Why | PoC | prove the multi-server architecture | run the bank |

The important UAT work is two-fold:
1. **Split the one compose file into two** (already done — files are in the repo under `deploy/uat/`).
2. **Make collection SNMP-native** — the simulators already answer genuine SNMP GETs (community `dashen_sim`, OID root `.1.3.6.1.4.1.99999`). This is the same mechanism production uses for real ATMs; only the port (161) and the real vendor OID tree change later.

> Two things are handled by other departments (verify only): VM provisioning, VLANs, security groups, and firewall rules. You only **install, configure, and customize** — then check the connections work.

---

# 1. Before you start (prerequisites)

- [ ] **Two UAT servers provisioned** (confirmed with IT):

  | Server | Name | VLAN / Network | IP |
  |---|---|---|---|
  | UAT-01 | DBHQUATATMMONAPP | VLAN 4029 — 172.26.208.0/24 (gw 172.26.208.1) | 172.26.208.176 |
  | UAT-02 | DBHQUATATMMONDB | VLAN 4021 — 172.26.21.0/24 (gw 172.26.21.1) | 172.26.21.50 |

  Sizes: UAT-01 = 4 vCPU / 8 GB / 500 GB; UAT-02 = 4 vCPU / 8 GB / 200 GB.
- [ ] **SSH access** to both (security groups being added by Cloud & Core)
- [ ] **RHEL 9** with subscription / repo access on both
- [ ] **GitHub SSH key or token** on both (to clone the repo)
- [ ] (Handled by other teams, verify later) firewall/VLAN rules:
  - UAT-01 (VLAN 4029) → UAT-02 (VLAN 4021) **TCP 9200** (OpenSearch)
  - UAT-02 (VLAN 4021) → UAT-01 (VLAN 4029) **TCP 5432** (PostgreSQL)
  - UAT-01: **8080, 3000, 8082, 8888** reachable from your laptop
  - Repo server 172.25.37.4 → both servers **TCP 443, 80** (updates)

---

# 2. Step A — Install the tools on each server (Docker + git)

Run these on **both** UAT-01 and UAT-02.

```bash
ssh <your-user>@172.26.208.176        # on UAT-01
# --- and separately ---
ssh <your-user>@172.26.21.50          # on UAT-02
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

# 2. git + net-snmp (the latter is needed to verify simulator SNMP later)
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
cp deploy/uat/.env.uat .env
chmod 600 .env
nano .env                               # fill every <...> placeholder with a strong password
```

Generate 4 distinct passwords:

```bash
openssl rand -base64 16     # run 4 times
```

> `DB_PASS` in `.env` **must equal** `POSTGRES_PASSWORD` — the UAT-01 PostgreSQL and the UAT-02 gateway both use it.

---

# 4. Step C — Deploy UAT-02 (OpenSearch + OpenSearch Dashboards + ISO 8583 Gateway)

Do this **first** — UAT-01's Report Portal and Filebeat depend on OpenSearch.

```bash
ssh <your-user>@172.26.21.50
cd /opt/atm-monitoring

# Start OpenSearch + Dashboards (the gateway needs UAT-01's DB, so start it later in Step E)
docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d opensearch opensearch-dashboards
sleep 60
curl -s http://localhost:9200          # expect JSON with cluster_name + version
curl -s http://localhost:9200/_cat/indices?v
```

---

# 5. Step D — Deploy UAT-01 (PostgreSQL + Zabbix + Grafana + GLPI + simulators)

```bash
ssh <your-user>@172.26.208.176
cd /opt/atm-monitoring

# Create the local dirs the compose mounts
mkdir -p ej-logs config/grafana/dashboards
sudo mkdir -p /data/real-ej-logs && sudo chown $USER:$USER /data/real-ej-logs

# 1. PostgreSQL first
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d postgres
for i in {1..30}; do
  docker exec zabbix-db pg_isready -U zabbix -d zabbix >/dev/null 2>&1 && break
  echo "waiting for PostgreSQL ($i/30)"; sleep 3
done

# 2. Create the custom tables (atm_locations, atm_transactions, ...)
docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/atm_custom_tables.sql
docker exec zabbix-db psql -U zabbix -d zabbix -c "\dt"     # confirm the custom tables exist

# 3. Let the UAT-02 gateway reach this DB over the network
docker exec zabbix-db bash -c "echo \"listen_addresses = '*'\" >> /var/lib/postgresql/data/postgresql.conf"
docker exec zabbix-db bash -c "echo 'host all all 172.26.21.0/24 md5' >> /var/lib/postgresql/data/pg_hba.conf"
docker compose -f deploy/uat/docker-compose-uat-vm1.yml restart postgres

# 4. Build the images that come from this repo, then start everything
docker compose -f deploy/uat/docker-compose-uat-vm1.yml build --no-cache \
  atm-sim-engine atm-txn-engine atm-ej-engine state-manager report-portal
docker compose -f deploy/uat/docker-compose-uat-vm1.yml up -d
sleep 60

# 5. Confirm all containers are Up
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v Exited
```

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
ssh <your-user>@172.26.21.50
cd /opt/atm-monitoring
docker compose -f deploy/uat/docker-compose-uat-vm2.yml up -d iso8583-gateway
sleep 15
docker logs iso8583-gateway --tail 5        # "MODE: SIMULATION", generating transactions
```

Verify transactions landed on UAT-01:

```bash
ssh <your-user>@172.26.208.176
docker exec zabbix-db psql -U zabbix -d zabbix -c \
  "SELECT source, COUNT(*) FROM atm_transactions GROUP BY source;"
# Expect source = 'ISO8583_SIM', count growing
```

---

# 7. Step F — Verify the network (the part other teams set up)

These must succeed (if not, open a ticket with the Network/Cloud team):

```bash
# From UAT-01: reach UAT-02's OpenSearch
curl -s http://172.26.21.50:9200/_cat/indices?v

# From UAT-02: reach UAT-01's PostgreSQL
pg_isready -h 172.26.208.176 -U zabbix -d zabbix      # (or: psql -h 172.26.208.176 ...)

# From your laptop: the web UIs on UAT-01
#   http://172.26.208.176:8080  Zabbix
#   http://172.26.208.176:3000  Grafana
#   http://172.26.208.176:8082  GLPI
#   http://172.26.208.176:8888  Report Portal
```

---

# 8. Step G — Configure Zabbix (import + create ATM hosts)

Browse to **http://172.26.208.176:8080** (`Admin` / `zabbix`).

1. **Configuration → Templates → Import**
   - `config/zabbix/template_ncr_snmp.xml` (template `Dashen Bank ATM Hardware`)
   - `config/zabbix/template_grg_snmp.xml` (template `Dashen Bank ATM Hardware - GRG`)
2. **Configuration → Hosts → Import** → `config/zabbix/zbx_export_hosts.xml` (the VM's own Zabbix-agent host)
3. **Administration → Media types → Import** → `config/zabbix/zbx_export_mediatypes.xml`

Now create the simulator hosts (SNMP, not HTTP):

1. **Configuration → Hosts → Create host**
2. **Host name:** `ATM-001 | NCR | Sim`
3. **Host groups:** `Dashen Bank ATMs` (create if missing)
4. **Interfaces → Add → SNMP:** IP `127.0.0.1`, **Port `1161`**, SNMPv2, community `dashen_sim`
5. **Templates:** link `Dashen Bank ATM Hardware` (NCR) or `… - GRG`
6. **Macros:** `{$ATM_PORT}` = `1161`, `{$ATM_VENDOR}` = `NCR`
7. **Add**
8. Repeat for a few ATMs (next port `1162`, `1163`, …)

After 1–2 min: **Monitoring → Latest data** for `ATM-001` should show all ~30 items populated.

---

# 9. Step H — Configure & customize the rest

**Grafana** (http://172.26.208.176:3000): the `datasources-uat.yml` already provisions the 3 datasources (Zabbix-ATM, ATM-Transactions, EJ-OpenSearch) — open **Connections → Data sources** and confirm each says "Success". All 6 dashboards should show simulator data.

**GLPI** (http://172.26.208.176:8082): complete the install wizard → **Setup → General → API** enable REST API and create an API client → in Zabbix **Administration → Media types** set the GLPI Ticket media type `glpi_url` = `http://172.26.208.176:8082`. Trigger a fault on a simulator ATM and confirm a ticket is created.

**Report Portal** (http://172.26.208.176:8888): generate a PDF/Excel/CSV report. EJ counts come from UAT-02's OpenSearch.

**EJ logs → OpenSearch:** `ej-logs/` on UAT-01 is shipped by Filebeat to UAT-02. Check:
```bash
ssh <your-user>@172.26.21.50
curl -s http://localhost:9200/_cat/indices?v     # atm-ej-live-YYYY.MM.DD present
```
Open OpenSearch Dashboards (http://172.26.21.50:5601), create index pattern `atm-ej-live-*` (time field `@timestamp`).

**Filebeat output note:** the repo `filebeat.yml` is edited per environment. For UAT it must point at the UAT-02 IP:
```yaml
output.elasticsearch:
  hosts: ["172.26.21.50:9200"]
```
(For production it becomes `localhost:9200` and adds `/data/real-ej-logs/*.log`.)

---

# 10. Step I — Validate UAT (sign-off checklist)

- [ ] All containers `Up` on both servers
- [ ] `snmpwalk … 127.0.0.1:1161 ….99999.8` returns data
- [ ] Zabbix shows every item for each ATM host (no "Not supported")
- [ ] A simulator fault creates a Zabbix problem **and** a GLPI ticket
- [ ] Grafana: 6 dashboards live; 3 datasources test OK
- [ ] Report Portal generates all report types
- [ ] `ISO8583_SIM` rows growing in `atm_transactions`
- [ ] `atm-ej-live-*` indices populated; OpenSearch Dashboards searchable
- [ ] Restart a VM → everything returns (`restart: unless-stopped`)

---

# 11. UAT exit criteria (before production)

Do **not** deploy production until: SNMP path proven, templates final, cross-VM links proven, gateway parser validated (if sample messages exist), GLPI ticketing proven, reporting proven, backup/restore rehearsed, and every UAT change committed to git. Full detail: `docs/Production_Migration_Guide_v2.md` §5 + §15.

---

# 12. Troubleshooting (UAT-specific)

| Symptom | Cause | Fix |
|---|---|---|
| `snmpwalk` empty on 1161 | simulator SNMP responder not up / UDP not published | `docker logs atm-sim-engine` → `[SNMP]`; check `1161-1260/udp` in compose |
| Zabbix item "Not supported" | HTTP/agent interface, or wrong port/community | SNMP interface, `{$ATM_PORT}`, community `dashen_sim`, correct vendor template |
| Report Portal can't reach OpenSearch | wrong `OS_HOST` or firewall | `curl http://172.26.21.50:9200` from UAT-01; check `OS_HOST: 172.26.21.50:9200` |
| Gateway crash-loops | `DB_HOST` wrong / PG not remote-ready | `docker logs iso8583-gateway`; verify `listen_addresses='*'` + `172.26.21.0/24` in `pg_hba.conf` |
| Filebeat ships nothing | `ej-logs/` perms or `filebeat.yml` ownership | `sudo chown -R $USER:$USER ej-logs/`; `sudo chown root:root filebeat.yml` |
| OpenSearch won't start | `vm.max_map_count` missing | `sudo sysctl -w vm.max_map_count=262144` (Step A.3) |
| GLPI 502 | wizard not finished / PHP timeout | finish wizard at :8082; restart GLPI container |

For the rest, the stack is identical to production — see `docs/Production_Migration_Guide_v2.md` §16.

---

# 13. File index

| Purpose | File |
|---|---|
| UAT-01 compose | `deploy/uat/docker-compose-uat-vm1.yml` |
| UAT-02 compose | `deploy/uat/docker-compose-uat-vm2.yml` |
| UAT env | `deploy/uat/.env.uat` |
| UAT datasources | `config/grafana/datasources-uat.yml` |
| UAT Postgres tuning | `config/postgres-uat/postgresql-custom.conf` |
| SNMP templates | `config/zabbix/template_ncr_snmp.xml`, `template_grg_snmp.xml` |
| Simulator SNMP responder | `simulators/snmp_agent.py` |
| Filebeat config | `filebeat.yml` (output edited per environment) |
| Production reference | `docs/Production_Migration_Guide_v2.md` |

---

*End of guide. Last updated: 2026-08-20.*
