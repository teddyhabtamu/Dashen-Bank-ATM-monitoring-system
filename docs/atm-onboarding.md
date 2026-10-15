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

---

## Before You Start — collect these (ask the ATM/hardware team)

- [ ] ATM ID (example: `ATM-0107`) — short, unique, never reused
- [ ] ATM name, branch, district **code** (example: `Kirkos` today;
      fleet uses zone codes like NAD/SAD — confirm which list applies),
      city, region
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
- [ ] If the form is unreachable, the same row can be added with SQL
      (ask the team — never invent column names; the table has grown).
- [ ] Record the row: `SELECT atm_id, vendor, ip_address, district FROM
      atm_locations WHERE atm_id = '<ATM-ID>';` must show your values.

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

## Step 3 — Create the monitoring host (UAT-01, one command)

Registering is automatic and safe to repeat (existing hosts are skipped,
never duplicated):

- [ ] Run, no passwords to type (they come from the server config):
      `python3 scripts/sync_atms_to_zabbix.py --apply`
- [ ] Expected last line: `Done: 1 created, N skipped, 0 without IP`
      (the `1 created` is your box; reruns print `0 created` — healthy).
- [ ] What it wires for you: NCR gets the SNMP template + the WMI Parts
      template + both network interfaces; GRG gets its SNMP template.
      You do not link anything by hand.

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
- [ ] Whole fleet at once (bulk rollout / after a Zabbix restore):
      `docker exec report-portal python3 verify.py`
      prints one line per ATM and counts; exit code 1 if any ATM is silent.
      Add `--json` to feed the onboarding wizard in Phase 4.
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

## Step 5 — Journals for disputes (NCR: same day; GRG: when SNMP lands)

- [ ] Nothing to edit. The pull list is data, not code: open portal →
      ATM Registration → the new ATM → set **EJ Hostname** to the machine
      name that appears in `E:\EJLogs` filenames on the box (the middle
      part of `<date>_<HOST>_EJDATA.log`) and save.
- [ ] Confirm the resolver agrees, on the pull host:
      `python3 scripts/ej_pull_targets.py --explain`
      → prints `ATM-ID:IP:HOSTNAME` for every eligible box plus the reason
      any active NCR box was skipped. Skip reason `no EJ hostname recorded`
      means step one above is not done.
- [ ] After the next hour, open portal → EJ Search → filter the ATM:
      journal rows with masked cards must appear. Empty result with a
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
