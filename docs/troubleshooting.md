# Troubleshooting Guide

> Quick answers for the most common failures in the Dashen Bank ATM
> Monitoring System. If you are new to this system, read
> `docs/architecture.md` first so the fixes below make sense.

---

## How to Use This Document

Find the symptom you're seeing in the headings below, run the
diagnostic commands, apply the fix. Most issues in this system come
from one of three root causes:

1. **WSL/Docker network IP changed** (after a reboot or folder rename)
2. **File/directory ownership** (root vs. your user, after cloning fresh)
3. **A container isn't running** (crashed, never started, or wrong image)

Keep that in mind — many "different looking" problems below have the
same underlying cause.

---

## "A container is in Restarting status"

**Check the logs first, always:**

```bash
docker compose logs <container-name> --tail=30
```

For example:
```bash
docker compose logs atm-sim-002 --tail=30
```

**Common causes:**

| Log message contains | Cause | Fix |
|---|---|---|
| `AttributeError` / Python traceback | Code bug in the simulator | Check recent changes to that .py file |
| `port is already allocated` | Another process/container using that port | `docker ps` to find the conflicting container, stop it |
| `Connection refused` to postgres/opensearch | Dependency container not ready yet | Wait 30s, `docker compose restart <name>` |
| `config file must be owned by...` | File permissions issue (common with filebeat.yml) | `sudo chown root:root filebeat.yml && sudo chmod 644 filebeat.yml` |
| `no such file or directory` | Missing volume mount or file not copied during build | Rebuild: `docker compose build <name> --no-cache` |

**General recovery — full restart of one service:**

```bash
docker compose stop <name>
docker compose rm -f <name>
docker compose up -d <name>
sleep 10
docker compose logs <name> --tail=20
```

**If multiple containers are restarting at once** after a reboot or
machine move, it's almost always the Docker network gateway IP
changing. See "Zabbix agent shows Not Available" below — the same
root cause affects multiple services.

---

## "Zabbix agent shows Not Available" (ATM-001)

This means the real Zabbix Agent (running on your WSL host, representing
ATM-001) and the Zabbix Server container can't reach each other.

**Full fix is documented in README.md Step 7.** Quick version:

```bash
# 1. Find your current WSL IP
ip addr show eth0 | grep 'inet '

# 2. Find the Docker network gateway
docker network inspect <project-name>_default | grep -i gateway

# 3. Confirm/update the agent config
sudo nano /etc/zabbix/zabbix_agent2.conf
# Server=127.0.0.1,172.16.0.0/12   <- covers all Docker subnets permanently
# ServerActive=<gateway-ip-from-step-2>:10051

# 4. Restart agent + reload Zabbix server cache
sudo systemctl restart zabbix-agent2
docker exec -it zabbix-server zabbix_server -R config_cache_reload

# 5. Test connectivity
docker exec zabbix-server zabbix_get -s <wsl-ip-from-step-1> -p 10050 -k agent.ping
```

If step 5 returns `1`, go to **Configuration → Hosts → ATM-001 →
Interfaces** and set the IP to your WSL IP from step 1, then click
**Update**.

> ATM-002 through ATM-005 use HTTP agent items against the simulator
> containers, not the Zabbix Agent — they are not affected by this
> issue.

---

## "OpenSearch Dashboards shows no data" / "Ready to try OpenSearch Dashboards? First, you need data"

This means OpenSearch has no indices, which means EJ logs aren't
reaching it. Work through these in order:

**1. Check OpenSearch actually has indices:**
```bash
curl -s "http://localhost:9200/_cat/indices?v"
```
If this only shows the header row with no data — OpenSearch is
empty. Continue below.

**2. Check the EJ generator containers are running:**
```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep atm-ej
```
You should see 5 containers (`atm-ej-001` through `atm-ej-005`) all
`Up`. If they don't exist at all:
```bash
grep -c "atm-ej-" docker-compose.yml
```
If this returns `0`, the services are missing from your compose file
entirely — see README.md Step 8.3 to add them back.

**3. Check `ej-logs/` directory ownership** (very common after cloning
on a new machine — Docker often creates it as `root:root`):
```bash
ls -la ej-logs/
sudo chown -R $USER:$USER ej-logs/
chmod 755 ej-logs/
docker compose restart atm-ej-001 atm-ej-002 atm-ej-003 atm-ej-004 atm-ej-005
```

**4. Check Filebeat config file ownership** (Filebeat refuses to start
otherwise):
```bash
docker compose logs filebeat --tail=10
```
If you see `config file must be owned by the user identifier (uid=0)`:
```bash
sudo chown root:root filebeat.yml
sudo chmod 644 filebeat.yml
docker compose restart filebeat
```

**5. Confirm log files are actually being written:**
```bash
ls -la ej-logs/
wc -l ej-logs/*.log
```

**6. Re-check OpenSearch after fixing the above** (wait ~30-60s):
```bash
curl -s "http://localhost:9200/_cat/indices?v"
```

**7. If indices exist but OpenSearch Dashboards Discover shows nothing:**
- Check the Index Pattern exists: **Stack Management → Index Patterns** —
  should show one pointing to `atm-electronic-journal` with timestamp field
  `@timestamp`
- Check the time range picker (top right of Discover) — default
  "Last 15 minutes" often shows nothing; set it to **Last 7 days**

---

## "Report portal returns 500 error"

**Always check the container logs first:**
```bash
docker logs report-portal --tail=30
```

**Common causes:**

| Symptom in logs | Cause | Fix |
|---|---|---|
| `psycopg2.OperationalError: could not connect` | PostgreSQL not reachable or down | `docker ps \| grep zabbix-db` — confirm it's Up; check `DB_HOST` env var matches the postgres container name |
| `relation "atm_transactions" does not exist` | Database tables missing (fresh install, restore not run) | `bash scripts/restore_db.sh` |
| `KeyError` or `IndexError` in a report route | A query returned no rows for the selected date range | Try a wider date range (e.g. 30 days instead of 1 day) |
| Container not running at all | Build failed or crashed on startup | `docker compose up -d report-portal` then check logs again |

**Quick full rebuild if nothing else works:**
```bash
docker compose stop report-portal
docker compose build report-portal --no-cache
docker compose up -d report-portal
sleep 10
docker logs report-portal --tail=20
```

**Test the database connection directly** to isolate whether the
problem is the Report Portal app or the database itself:
```bash
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT COUNT(*) FROM atm_transactions;"
```
If this works but the Report Portal still fails, the issue is in the
Flask app itself, not the database.

---

## "GLPI is not creating tickets"

Work through the chain in this order — the most common break point is
the App Token going stale after a fresh GLPI install.

**1. Confirm the alert actually fired in Zabbix:**
**Monitoring → Problems** — is the problem listed there at all? If
not, this isn't a GLPI issue — the trigger never fired. Check the
item is collecting data first.

**2. Check the Zabbix Action Log for the GLPI operation:**
**Reports → Action log** — find the relevant alert, check the GLPI
Ticket media type row. It will show ✅ Sent or ❌ Failed with the
exact error.

**3. Common errors and fixes:**

| Error in Action log | Cause | Fix |
|---|---|---|
| `ERROR_WRONG_APP_TOKEN` | App Token doesn't match current GLPI install | Regenerate in GLPI → Setup → General → API, update Zabbix media type parameter |
| `ERROR_LOGIN_PARAMETERS_MISSING` | `glpi_user`/`glpi_password` parameter wrong or missing | Re-check Parameters tab on the GLPI Ticket media type |
| Connection refused / timeout | GLPI container down or wrong URL | `docker ps \| grep glpi`; confirm `glpi_url` param uses the container name e.g. `http://glpi:80/apirest.php` |
| No error, but no ticket appears | Action condition doesn't match, or operation not enabled | **Configuration → Actions → Trigger actions** — check conditions and that the GLPI operation is present |

**4. Regenerating the GLPI App Token (most common fix):**

In GLPI: **Setup → General → API** tab → confirm "Enable REST API" is
on → find your API client (or create one) → copy the **App Token**.

In Zabbix: **Administration → Media types → GLPI Ticket** → update
the `app_token` parameter with the new value → **Update**.

**5. Test the GLPI API directly** to confirm GLPI's API itself is
working, independent of Zabbix:
```bash
curl -s -X GET "http://localhost:8082/apirest.php/initSession" \
  -H "Content-Type: application/json" \
  -H "Authorization: Basic $(echo -n 'glpi:DashenGLPI2024' | base64)" \
  -H "App-Token: YOUR_APP_TOKEN"
```
A successful response returns a `session_token`. If this fails, the
problem is in GLPI itself, not the Zabbix integration.

---

## "ATM-002 through ATM-005 show no data, only ATM-001 works" (or vice versa)

This is almost always a **Docker gateway IP mismatch** after a machine
move, reboot, or project folder rename.

```bash
# Find the current gateway
docker network inspect <project-name>_default | grep -i gateway

# Test reaching each simulator from inside Zabbix server
docker exec zabbix-server wget -qO- http://<gateway-ip>:1162/oid/1.1.0
docker exec zabbix-server wget -qO- http://<gateway-ip>:1163/oid/1.1.0
docker exec zabbix-server wget -qO- http://<gateway-ip>:1164/oid/1.1.0
docker exec zabbix-server wget -qO- http://<gateway-ip>:1165/oid/1.1.0
```

If these return `1`, but Zabbix items still show no data, the item
URLs in the **Dashen Bank ATM Hardware** template have the wrong IP
hardcoded. Go to **Configuration → Templates → Dashen Bank ATM
Hardware → Items**, open any item, and check the URL field — it
should use `{$ATM_PORT}` macro, with the gateway IP matching what you
just confirmed works.

Also check that all 5 simulator containers are actually running and
not stuck restarting:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep atm-sim
```

---

## "docker compose up fails with network not found"

This happens when the project folder was renamed (Docker names
networks after the folder name) and an old `docker-compose.yml`
reference or cached state points to the old name.

```bash
docker compose down
docker compose up -d
```

This recreates the network under the current folder's name. Update
any hardcoded references to the old network name (e.g. in backup
scripts or notes) to match.

---

## "Permission denied" errors generally (not covered above)

Almost always one of:
```bash
# ej-logs directory
sudo chown -R $USER:$USER ej-logs/

# filebeat config
sudo chown root:root filebeat.yml && sudo chmod 644 filebeat.yml

# scripts not executable
chmod +x scripts/*.sh
```

---

## When All Else Fails — Full Stack Restart

```bash
cd ~/project/ATM_monitoring_sys/zabbix-atm

docker compose down
docker compose up -d

sleep 60

docker ps --format "table {{.Names}}\t{{.Status}}" | grep -v "Exited"
```

If containers are still failing after this, check `docker compose
logs <name>` for each one that isn't `Up`, and work through the
relevant section above.

---

## Where to Look for More Detail

- **README.md** — full setup steps for a new machine, including the
  WSL/Docker networking fix in much more detail
- **docs/architecture.md** — what each container does and where its
  data comes from, useful for understanding *why* something broke
- **Action Log (Zabbix)** and **container logs (`docker logs`)** are
  always the fastest way to find the real error message — read the
  actual error before guessing at a fix