# Onboarding a New ATM — Complete Step-by-Step Guide
(plain language;ity tick every box in order)

> This is the repeat-use checklist for connecting ONE new real ATM.
> Two vendor paths exist because the machines talk differently:
> **NCR = Zabbix agent + WMI collector** (proven live), **GRG = SNMP**
> (needs its firewall rule first). Everything below was proven on the
> 4 Kilo test ATMs — follow it exactly and the new box behaves the same.
> Words used here: *UAT-01* = our monitoring server, *ATM* = the cash
> machine, *Zabbix* = the monitoring dashboard, *portal* = the bank's
> ATM web pages, *GLPI* = the ticket system.

## What is automatic now

Saving an ATM creates a durable provisioning job. The portal retries it with
backoff until the Zabbix host exists, instead of losing the work if Zabbix is
briefly unavailable. The Agent setup page shows **Synced / Retrying / Needs
registry change** and has a **Retry provisioning now** button.

The agent's `atm.identity` item is collected passively by Zabbix every hour.
The portal reads its latest value every five minutes and stores it as evidence;
the manual paste is only a fallback. **Refresh from Zabbix** asks for a check
immediately after an install.

---

## Before You Start — collect these (ask the ATM/hardware team)

- [ ] ATM name, branch, district **code** (example: `Kirkos` today;
      fleet uses zone codes like NAD/SAD — confirm which list applies),
      city, region
- [ ] ATM ID — **optional**. Leave blank and the portal generates
      `ATM-<Terminal ID>`; only supply one for an integration that already
      uses a specific key
- [ ] GPS coordinates (latitude, longitude) — for the map
- [ ] IP address of the ATM on the bank network
- [ ] Terminal ID / TID (must match the switch records exactly)
- [ ] Vendor and model (example: NCR SelfServ 34, GRG AD2600)
- [ ] For GRG only: SNMP community string (or v3 user/passwords)
- [ ] For NCR only: RDP access + the branch custodian present
- [ ] Confirm the machine is powered on, networked, and serving customers

---

## Step 0 — Network permission (do FIRST, takes the longest)

Nothing below works without packets. Ask the network team for the
forwarding rules to the new ATM's IP (same treatment the test ATMs got):

- [ ] **NCR needs:** TCP 10050 (monitoring) and TCP 3389 (remote desktop).
      TCP 445 later, only when journal collection starts for this box.
- [ ] **GRG needs:** UDP 161 (monitoring). Nothing else yet.
- [ ] Confirm each rule with the matching check in Step 3/4 — a "done"
      email is not proof; a live value is.

---

## Step 1 — Write the ATM into the inventory (UAT-01)

The database row drives everything downstream (monitoring hosts,
portal pages, maps, reports). Preferred method — the portal form:

- [ ] Open the portal → **ATM Registration → Add New ATM**, fill every
      field from the Before-You-Start list, **Vendor** spelled exactly
      `NCR` or `GRG`, status **active**, Save.

      > **ATM ID vs Terminal ID.** The ATM team usually knows the terminal ID
      > (TID), not the portal's `atm_id`. The ATM list shows the terminal ID
      > first and the internal ATM ID as the small system line under it.
      > Search **ATMs** by TID first: an existing row gives you its ATM ID.
      > For a new ATM, leave **ATM ID** blank: the portal generates
      > `ATM-<TID>` (for example `TID006` → `ATM-TID006`). A known TID keeps
      > its existing key. Only type an ATM ID when an integration already
      > depends on a specific one; see `docs/system-workflow.md` §4.1.
- [ ] If the form is unreachable, the same row can be added with SQL
      (ask the team — never invent column names; the table has grown).
- [ ] Record the row: `SELECT atm_id, vendor, ip_address, district FROM
      atm_locations WHERE atm_id = '<ATM-ID>';` must show your values.
- [ ] Open the ATM → **Agent setup** and confirm **Zabbix provisioning** is
      **Synced** (or **Retrying** with a named error). The host is created
      automatically after save; Step 3 is the manual fallback, not the normal
      path.

---

## Step 2A — NCR boxes only: install the collector (on the ATM)

Do this over RDP with the custodian watching. It only READS — no
changes to cash, software, or settings.

- [ ] On UAT-01, confirm you have the newest script:
      `git pull origin uat-prep` (in `/data/atm-monitoring`).
- [ ] Copy `scripts/atm-wmi-collector.ps1` to the ATM as
      `C:\monitor\atm-wmi-collector.ps1` (overwrite if present).
- [ ] On the ATM, open `C:\Program Files\Zabbix Agent\zabbix_agentd.conf`
      and confirm it contains exactly one line starting with
      `UserParameter=atm.parts,` pointing at that path. If the Zabbix
      agent is not installed at all, install it first, then add the
      config snippet from `config/zabbix/atm-agent-userparams.conf`,
      then restart the *Zabbix agent service* (services.msc) — nothing else.
- [ ] Prove the file on disk is the new one (open it, check for the
      word `device_instance`) — a stale copy is the #1 onboarding
      failure; this 10-second check prevents an hour of debugging.

## Step 2B — GRG boxes only: SNMP credentials (on UAT-01)

- [ ] No ATM visit needed. Confirm the community string matches what
      the box expects (ask the GRG/hardware team which community the
      device serves).
- [ ] Nothing to install on the box for monitoring phase 1.

---

## Step 2C — Agent profile and installer (portal, admin)

The portal renders the exact agent files for one ATM, and one signed
`AtmMonitorSetup.exe` applies them. The installer never re-implements policy:
it writes the profile's own text, verifies every probe hash, and tests the
result as the agent's service account. This is the supported path for a new
rollout; the manual copy in Step 2A is the fallback for an NCR box already
wired by hand.

- [ ] Open the portal → the ATM → **Agent setup** (admin only).
- [ ] **Download profile.** It lists every deployable probe with its SHA-256,
      the agent `Server=`/`Hostname=` config, the approved image roots, and an
      HMAC signature. It never contains the SNMP community or a PSK.
- [ ] Approve image roots on the same page. The probe only *proposes* roots it
      can see; a human approves them, and the roots file stays fail-closed.
      Approval changes the desired profile, so approve **before** downloading
      it. After the reapply, the box reports the applied `deploy_hash`; Fleet
      health keeps Images at **Reapply needed** until it matches the current
      deployable profile.
- [ ] Put the profile next to `AtmMonitorSetup.exe` and run `/DRYRUN` first.
      The dry run prints every action and writes nothing. Then run it again
      without `/DRYRUN` and confirm the ATM id.
- [ ] Confirm the ATM's local Windows Firewall allows inbound TCP 10050. The
      Agent 2 MSI adds an exception, but a GPO or endpoint policy may strip it;
      the district-proxy rule is not enough on its own.
- [ ] The installer writes `C:\monitor`, installs/updates Zabbix Agent 2 6.4,
      writes the passive-only config, restarts the service, and verifies
      `atm.identity`/`atm.parts` as `SYSTEM`. A running Agent 1 needs
      `/REPLACELEGACY`; it is stopped and disabled, not uninstalled.
- [ ] Identity arrives automatically: Zabbix collects `atm.identity` hourly and
      the portal stores it within five minutes. Use **Refresh from Zabbix** on
      the Agent setup page for an immediate check. If that is unavailable, paste
      the probe output into **Submit identity** as the fallback. Conflicts are
      shown for review; the registry is never changed automatically.
- [ ] Set `ATM_PROFILE_SECRET` in `.env` before production. An unsigned
      profile is allowed only in the UAT lab, and the page says so.
- [ ] Full installer flags, exit codes, rollback and signing steps:
      `installer/README.md`.

---

## Step 3 — Monitoring host (automatic, with a manual fallback)

Saving the ATM in Step 1 already queued host creation. The worker retries
transient Zabbix failures on its own; existing hosts are skipped, never
duplicated. Use the command below only when the Agent setup page says
**Needs registry change**, after a Zabbix restore, or for a bulk import:

- [ ] Run, no passwords to type (they come from the server config):
      `python3 scripts/sync_atms_to_zabbix.py --apply`
- [ ] Expected last line: `Done: 1 created, N skipped, 0 without IP`
      (the `1 created` is your box; reruns print `0 created` — healthy).
- [ ] What it wires for you: NCR gets the SNMP template + the WMI Parts
      template + the shared Agent template + both network interfaces; GRG gets
      its SNMP/XFS template + the shared Agent template + both interfaces.
      You do not link anything by hand.
- [ ] One-time after upgrading to this build: create the shared Agent template
      and move `atm.identity` onto it:
      `python3 scripts/import_agent_template_api.py --link-all`
      This also removes the old copy from the WMI and GRG XFS templates. That
      removal is required: a host that links both vendor templates cannot
      inherit the same key twice. Then refresh the vendor templates:
      `python3 scripts/import_wmi_template_api.py` and
      `python3 scripts/import_grg_xfs_template_api.py --no-link`.
      The shared item is harmless on a box without the agent; it simply stays
      unsupported.

## Step 4 — Prove data flows (one command, then Zabbix UI)

Read-only. Nothing is written; safe to run any time, and it is the gate for
calling an ATM live.

- [ ] On UAT-01 run:
      `docker exec report-portal python3 verify.py <ATM-ID>`
      `OK    <ATM-ID>   reporting` — items arriving within the freshness window.
      `SILENT <ATM-ID> provisioned_not_reporting` — host exists, nothing is
      arriving; the printed blocker names the cause (unreachable IP, Zabbix
      agent not running, all items unsupported).
      `NONE  <ATM-ID>   not_provisioned` — no Zabbix host yet: run Step 3.
      Widen the window when a box polls slowly: `--window 30`.
- [ ] Confirm identity is automatic, not pasted:
      `docker exec report-portal python3 -c "from identity_sync import sync_one; print(sync_one('<ATM-ID>'))"`
      → `stored` or `unchanged`, and **Agent setup → Box identity** shows
      **Reported**. `no_item` means the `atm.identity` template item was not
      imported/linked; `no_value` means the agent has not answered yet.
- [ ] Whole fleet at once (bulk rollout / after a Zabbix restore):
      `docker exec report-portal python3 verify.py`
      prints one line per ATM — status, vendor, IP and the next action —
      worst first, and counts at the end. Exit code 1 if anything needs a
      human. `--csv` writes the same report as CSV for the rollout tracker.
      This is the bulk tool: importing 1,202 ATMs is one CSV, and this report
      tells you exactly which rows did not come up.
- [ ] Portal → **Fleet health** shows the same fleet with the current operator
      state (eight NetXMS states/colours) and five stages per ATM:
      **Provisioning, Reporting, Identity, EJ, Images**, plus the single next
      action. Filter by **Identity**, **EJ** or **Images** to see only the rows
      whose stage is not ready. A row is **Onboarded** only when all five are
      ready; reporting telemetry alone is not enough. Images is ready only
      when the approved roots are on the box *and* the reported applied
      `deploy_hash` matches the profile the portal would hand out now.
- [ ] **NCR:** on UAT-01 run
      `zabbix_get -s <ATM-IP> -k "atm.parts" | python3 -m json.tool > /dev/null && echo JSON-OK`
      then open Zabbix → Monitoring → Latest data → filter the new host:
      ~58 rows (35 parts + 14 devices + master) with fresh check times.
- [ ] **GRG:** open Latest data → filter the new host: 24 SNMP rows
      filling within 2–3 polls. Empty rows with red marks mean the
      firewall/community from Step 0/2B is not right yet — fix there,
      not here.
- [ ] Open Monitoring → Problems filtered to the host: a brand-new
      healthy box shows nothing (correct — silence means healthy);
      any ERROR/UNAVAILABLE row names a real fault — call the custodian,
      not the monitoring team.

## Step 5 — Journals for disputes (NCR and GRG)

- [ ] The pull list is data, not code. After identity ingest, the portal seeds
      an **empty** EJ Hostname from the journal filename
      (`<date>_<HOST>_EJDATA.log`) — for NCR the middle part of the
      `E:\EJLogs` filename; for GRG the box hostname (`EJ<yymmdd>.log`). It
      never overwrites an existing value; a mismatch is shown as a conflict.
      Review it in ATM Registration if the identity page reports one.
- [ ] Confirm the resolver agrees, on the pull host:
      `python3 scripts/ej_pull_targets.py --vendor GRG --explain`
      (use no `--vendor` for the NCR list).
      → prints `ATM-ID:IP:HOSTNAME` for every eligible box plus skip reasons.
      `no EJ hostname recorded` means identity has not arrived yet, or the
      journal filename had no host tag; set it manually in Registration.
- [ ] Pull GRG journals:
      `python3 scripts/pull_grg_ej_logs.py --days 3`
      → writes `ej-logs/<date>_<HOST>_EJDATA.log` and raw evidence under
      `ej-logs/raw/`. NCR uses `scripts/pull_ej_logs.sh` as before.
- [ ] Make the GRG pull automatic (every 5 minutes):
      `./scripts/install_grg_pull_cron.sh`
      → logs to `logs/grg-ej-pull.log`; remove with `--remove`.
      The separate ingest cron and filebeat run automatically after the pull.
- [ ] After the next hour, open portal → EJ Journal or EJ Search → filter the
      ATM: journal rows with masked cards must appear. Empty result with a
      working pull means the box writes no journals (check with custodian).

## Step 6 — Portal, dashboards, reports (click-through)

- [ ] Portal → ATM Registration: the new row shows a live state
      (IN_SERVICE green, HARDWARE_FAULT red with cause — never grey
      unless the box is truly silent).
- [ ] Open its detail page: hardware lights green/amber, parts list
      faults-first, cassette panel honest about missing counts.
- [ ] Grafana → ATM Operations Centre: row + map pin for the new box.
- [ ] Reports → run one Transaction Summary filtered to the ATM.

## Step 7 — Tickets (prove once per vendor, not per box)

- [ ] The Zabbix→GLPI chain is fleet-wide (proven on the test hosts) —
      no per-box test needed. Exception: a brand-new *vendor/model*
      gets one supervised fault drill with the custodian to prove its
      codes page the right people, exactly like the test NCR did.

---

## Operational guardrails (added 2026-10-16)

After a cron job failed silently for ~19 days (its crontab invoked
`venv/bin/python`, which does not exist on this host), three guardrails were
added so that class of failure cannot hide again.

```bash
# 1. Are the cron jobs still running? Exit 0 = jobs ran (warnings allowed),
#    1 = something is overdue or failed
python3 scripts/check_jobs.py

# 2. What is this environment actually running? Captures image DIGESTS (a tag
#    can be repointed; a digest cannot) and the installed Python packages.
bash scripts/capture_env_lock.sh          # read-only
bash scripts/capture_env_lock.sh --apply  # also appends commented pins to .env

# 3. Can the portal actually serve a request, and if not, which dependency?
curl -s localhost:8888/ready | python3 -m json.tool
```

`/ready` returns 503 when the database or Zabbix is unreachable and names the
failing dependency. `/health` is unchanged and stays unconditional — it answers
"is the process alive", which must not fail during a dependency restart.

Wire `check_jobs.py` to whatever alerts you use. Its exit code is the contract.
A `WARN` means the job ran but the ATM had no journal at the configured path.
That is expected for an idle or out-of-service ATM and exits 0; a missing log,
an overdue job, or a broken command still exits 1.

**Log paths.** Jobs are located by the log they write, so the paths in
`check_jobs.py` must match your crontab. `backup_db.sh` and
`backup_configs.sh` both write to `$HOME/atm-backup.log` — **and cron runs as
its own user**, which may not be you. If a job reports `NEVER_RAN` but you know
it runs, override the path:

```bash
CHECK_JOBS_LOG_backup_db=/root/atm-backup.log python3 scripts/check_jobs.py
```

**Those two backup jobs share one log file.** That means neither can be verified
independently: if one starts failing nightly while the other succeeds, the log
still updates and the failing one looks healthy. The checker flags this as a
known blind spot rather than pretending otherwise. Giving each job its own log
is the fix.

## Deploying a change to the portal

```bash
cd /data/atm-monitoring
git pull origin uat-prep
docker restart report-portal
docker logs --tail 20 report-portal      # expect no ERROR lines
```

Schema changes that the portal depends on are applied automatically at startup,
so **code and schema can no longer arrive in the wrong order**. That is not
decoration: shipping the transaction-source view once took the whole portal down,
because every page reads it and it had not been created yet. `create_app()` now
runs `txn_sources.ensure_schema_best_effort()`, which checks for the view first
and issues no DDL on a normal restart.

To apply a migration by hand instead (e.g. during a maintenance window):

```bash
# The portal's tables live in the portal database, not zabbix. See
# docs/portal-db-split.md for which is which on a given host.
docker exec -i zabbix-db psql -U zabbix -d portal < scripts/migrate_txn_sources.sql
```

**Where the scripts run.** `./report-portal` is mounted at `/app` in the
container, so `scripts/` is only on the host — and the host has no `venv/`
(`venv/bin/python` does not exist there, which is also why the 06:00
`reconcile_zabbix_glpi.py` cron job fails; see Troubleshooting).

Stdlib-only scripts run directly on the host with system python3:

```bash
python3 scripts/ej_profile.py          # what the EJ journals actually contain
```

Scripts that need the database run inside the container, which has psycopg2:

```bash
docker exec -i report-portal python3 - < scripts/set_txn_sources.py
docker exec -i report-portal python3 - < scripts/set_txn_sources.py --hide-demo
```

Do **not** `docker exec report-portal python3 scripts/...` — `scripts/` is not
mounted there, and it will always say "No such file or directory".

## Troubleshooting (symptom → cause, in order)

- `parts: 33` (or missing new keys) after copying the script → the ATM
  runs the old file; verify content on disk, re-copy, re-check.
- `JSON-BAD` / control-character errors → output framing broken in
  transit; capture `zabbix_get ... > /tmp/x.json` and validate locally.
- Login rejected by scripts → wait 60s (temporary blocks), never loop
  the command; credentials come from server config, not typing.
- All SNMP rows empty on GRG → firewall/community (Step 0/2B), always.
- Portal shows stale/wrong states → restart `report-portal`, wait 60s,
  reload; states derive from live values every poll.

## Sign-Off

- [ ] All sections above completed (Steps 0–7)
- [ ] ATM ID and onboarding date recorded in the inventory tracking sheet
- [ ] Issues encountered documented (helps speed up the next one)

**Date onboarded:** ____________________
**Onboarded by:** ____________________
**Notes:**

---

## See Also

- `docs/architecture.md` — what each part of the system does
- `docs/troubleshooting.md` — deeper fixes
- `docs/UAT_Phase0_Runbook.md` — calibrated code tables, root-cause log
- `docs/vendor-data-requests.md` — pending vendor answers affecting fleet
