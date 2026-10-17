# Portal database split — switch and rollback

The portal's tables used to live in the `zabbix` database, alongside Zabbix's
own configuration and history. They now have a database of their own. This is
the procedure for moving the running services across, and for putting them back.

Read the whole thing before starting. The order matters: several steps exist
because the wrong order loses data silently rather than loudly.

---

## What moves, and what must not

Six services write portal tables. All six must move together.

| Service | Host | Writes |
|---|---|---|
| `report-portal` | UAT-01 | `audit_log`, `app_users`, `atm_anomalies`, `atm_locations`, … |
| `network-correlator` | UAT-01 | `atm_network_metrics`, `atm_network_correlation`, `atm_network_events` |
| `anomaly-detector` | UAT-01 | `atm_anomalies` |
| `state-manager` | UAT-01 | `atm_current_state`, `atm_state_history` |
| `cash-watcher` | UAT-01 | `atm_cassette_levels`, `cash_replenishments` |
| `iso8583-gateway` | **UAT-02** | `atm_transactions` |

`iso8583-gateway` is in `docker-compose.gateway.yml`, runs on a different host,
and reaches the database over the network. It is also the simulator that feeds
demo transactions. Missing it means transactions keep landing in the old
database while the portal reads the new one — no error anywhere, and a demo
with no transactions in it.

There is also a **reader**, which is not a service and has no `DB_NAME`:

| Reader | Where | Reads |
|---|---|---|
| Grafana datasource `ATM-Transactions` | UAT-01, `config/grafana/datasources-uat.yml` | `atm_transactions`, `atm_current_state`, `atm_network_metrics`, `atm_network_correlation`, `atm_locations`, `atm_anomalies` |

It was missed in the first pass, because the inventory was built by walking
services and this is a provisioning file. While it pointed at the old database
every dashboard rendered frozen data with no error: transactions stuck at
181,265 while the portal read 181,378, and `last_seen` stuck at 16:48:27.
A stranded *writer* costs one page. A stranded *dashboard* costs the instrument
you would use to notice that data has stopped moving.

Grafana reads provisioning **at startup**, so a bind-mounted change needs a
restart. `docker compose up -d grafana` does not restart it when only a mounted
file changed — it reports `Running` and changes nothing. Use
`docker compose restart grafana`, then confirm with
`docker inspect grafana --format '{{.State.StartedAt}}'`.

**Zabbix itself never moves.** `postgres`, `zabbix-server`, `zabbix-web`,
`zabbix-agent` and `nginx` stay on `zabbix`. Pointing Zabbix at the portal
database would leave it with no hosts, no items and no history.

Leaving one *writer* behind is the quieter failure of the two, and much harder
to notice: nothing errors, every container stays up, and one page simply stops
updating.

---

## Why writers are stopped before the final sync

The copy is a snapshot. `network-correlator` adds to `atm_network_metrics`
every 120 seconds, `state-manager` writes `atm_current_state`, and the gateway
writes `atm_transactions` every 10 seconds.

If any of them is running while the final copy is taken, the rows they write
afterwards exist only in the old database. The portal switches to the new one
and those rows are simply gone — with no error, and with the old database still
sitting there looking complete.

So: stop the writers, take the final copy, then start them against the new
database. The cost is a few minutes of portal downtime, which is the right
trade against silent data loss.

---

## Procedure

### 0. Confirm the starting point

```bash
cd /data/atm-monitoring && git pull origin uat-prep
bash scripts/split_portal_db.sh --verify      # must exit 0
docker exec -e DB_NAME=portal -i report-portal python3 - < scripts/probe_portal_db.py
```

Both must pass. Do not continue otherwise.

### 1. Stop the writers (UAT-01)

```bash
docker compose stop cash-watcher anomaly-detector network-correlator state-manager
```

`report-portal` stays up for now so the site is reachable while you work.

### 2. Stop the gateway (UAT-02)

```bash
docker stop iso8583-gateway
```

Not `docker rm`. The container holds no data of its own — everything it has
generated is in PostgreSQL — but keeping it lets you bring it back with
`docker start` instead of a rebuild. `rm` is recoverable but slower and gives
you one more thing that can go wrong.

### 3. Take the final copy

```bash
cd /data/atm-monitoring
bash scripts/split_portal_db.sh
```

With the writers stopped, nothing changes underneath the snapshot, so every
table must match with no drift reported at all. Any drift here means something
is still writing, and the reason must be found before continuing.

### 4. Prove the copy is good

```bash
bash scripts/split_portal_db.sh --verify
docker exec -e DB_NAME=portal -i report-portal python3 - < scripts/probe_portal_db.py
```

### 5. Point the services at the new database

On **both** hosts:

```bash
# show the current value first
grep -n '^PORTAL_DB_NAME=' .env || echo "not set yet"
```

Set `PORTAL_DB_NAME=portal` (add the line if it is absent). On UAT-02 the file
is at whatever path that host uses; it must be set there too, or the gateway
keeps writing to the old database.

### 6. Start the services

UAT-01:

```bash
docker compose up -d report-portal cash-watcher anomaly-detector network-correlator state-manager
```

UAT-02:

```bash
docker start iso8583-gateway
```

### 7. Prove the writes actually moved

This is the step that matters. Counts are not evidence that a switch worked —
only movement is.

```bash
# before
docker exec zabbix-db psql -U zabbix -d zabbix -tAc "SELECT count(*) FROM atm_network_metrics;"
docker exec zabbix-db psql -U zabbix -d portal  -tAc "SELECT count(*) FROM atm_network_metrics;"
sleep 180
# after: the portal figure must have risen, the zabbix figure must not have
docker exec zabbix-db psql -U zabbix -d zabbix -tAc "SELECT count(*) FROM atm_network_metrics;"
docker exec zabbix-db psql -U zabbix -d portal  -tAc "SELECT count(*) FROM atm_network_metrics;"
```

`network-correlator` runs every 120 seconds, so three minutes is enough. Then
confirm the site works, and that new transactions are arriving:

```bash
docker exec zabbix-db psql -U zabbix -d portal -tAc "SELECT max(id) FROM atm_transactions;"
docker exec zabbix-db psql -U zabbix -d portal -tAc "SELECT count(*) FROM atm_transactions_visible;"
```

---

## Rollback

The old database is never modified, so rollback is a value change and a
restart. Nothing is lost in either direction, which is the point of doing this
in stages.

1. On both hosts, set `PORTAL_DB_NAME=zabbix`.
2. UAT-01: `docker compose up -d report-portal cash-watcher anomaly-detector network-correlator state-manager`
3. UAT-02: `docker restart iso8583-gateway`

The rows written to `portal` after the switch are still there if you switch
forward again, but they will not be merged back. On UAT this does not matter;
on production it is the reason to take a final full backup immediately before
step 5.

---

## Verifying backups afterwards

`backup_db.sh` detects where the portal tables live, so no edit is needed at
switch time. Run it once by hand and check it names the right database:

```bash
bash scripts/backup_db.sh
```

It must print `Portal tables live in database 'portal'` and produce both
`zabbix_full_*.sql.gz` and `portal_full_*.sql.gz`.

If it says `zabbix`, the services are not actually reading from `portal` — the
variable did not take, and the switch has not happened however healthy
everything looks.

Restore-test the portal dump before trusting it, the same way the zabbix one
was tested:

```bash
docker exec zabbix-db psql -U zabbix -d postgres -c "CREATE DATABASE restore_probe;"
docker exec -i zabbix-db psql -U zabbix -d restore_probe -q < <(zcat config/postgres/portal_full_*.sql.gz | sed 's/^//')
```

Better to run `scripts/backup_db.sh` and then restore its output into a scratch
database with `psql -f`, rather than piping, so the exit status is meaningful.
Drop the scratch database when finished.

---

## Outcome on UAT (2026-10-17)

Completed. Every writer and the reader verified by observing rows move into
`portal` while the old database stayed flat — configuration alone was not
treated as proof, because a stranded writer produces no error at all.

The old database is frozen and intact at the moment of the switch
(181,265 transactions, `last_seen` 16:48:27), which is what makes rollback
clean in either direction.

UAT-02 holds local, uncommitted work predating this split: its
`docker-compose.yml` is a deliberately reduced local version, and
`camel/iso8583_gateway.py` and `config/opensearch/atm_ej_parser.json` differ
from git. That work was committed to a local branch `uat02-local`
(`ec5dc2f`) before the switch, so it is no longer at risk from a stray
`git pull`. **UAT-02 is 358 commits behind `uat-prep`** and reconciling it is
its own piece of work.

Backup writes both databases and the `portal` dump was restore-tested: every
table matched the live database except `atm_transactions`, which was 17 rows
behind because the gateway kept writing during the restore. That direction is
correct for a point-in-time dump.

## Scripts that still name the `zabbix` database

The split moved data; it did not rewrite every script that referenced it. These
were found on 2026-10-17 and are **not** demo-blocking, but they are wrong for
a switched host and are worth triaging before production.

| Script | Verdict |
|---|---|
| `backup_db.sh` | fixed — detects where the tables live |
| `split_portal_db.sh` | correct — `zabbix` is its source by definition |
| `probe_portal_db.py` | correct — takes `DB_NAME` from the environment |
| `ej_pull_targets.py` | **was wrong — fixed.** Ran every minute and read `zabbix` |
| `add_perf_indexes.py` | fixed — now requires `DB_NAME` |
| `sync_atms_to_zabbix.py` | fixed — now requires `DB_NAME` |
| `uat_preflight.sh` | fixed — portal check follows the detection, `hosts` stays on `zabbix` |
| `probe_portal_db.py` | fixed — now requires `DB_NAME` |
| `restore_db.sh` | **worst** — restores only `zabbix`, so a restore would silently omit portal data |
| `setup_new_machine.sh` | builds the pre-split layout |
| `update_host_visible_names.py` | correct — writes Zabbix's own `hosts` table |

### The `DB_NAME` default, and why one of these was much worse

The pattern was `os.environ.get('DB_NAME', 'zabbix')`. After the split that
fallback is the one database the portal no longer reads.

`ej_pull_targets.py` was the serious one. It runs **every minute** from cron, and
the cron line exports `ZBX_URL`/`ZBX_USER`/`ZBX_PASS` but not `DB_NAME` — so the
EJ pipeline was resolving its target ATMs from the pre-split database. Nothing
about that fails. The read succeeds and returns the old rows, so an ATM
registered through the portal would simply never have its journals pulled, and
the only symptom would be an ATM quietly having no data. It appeared to work on
UAT only because `atm_locations` had not changed since the copy.

It now detects the database using the same `portal_schema_version` marker the
backup uses, and falls back to the nightly CSV snapshot - which *is* written
from the portal - with a message saying so, when detection fails.

The other three now **require** `DB_NAME` instead of assuming one. Defaulting
to `portal` would be wrong on a host that has not switched, and defaulting to
`zabbix` is what caused this. Neither is guessable, so they stop.

`restore_db.sh` deserves attention first. A restore that quietly omits the
portal is the same failure class as the backup that omitted it: the command
succeeds, the data is gone, and nobody finds out until it is needed.

---

## What this does not fix

The split separates the portal's *storage* from Zabbix's. It does not address
the sources that remain blocked: switch/Core Banking integration, GRG telemetry
over UDP/161, SMTP and Africa's Talking credentials, physical NCR cassette
balances, and camera images. Those are tracked in `docs/BRD-traceability.md`.
