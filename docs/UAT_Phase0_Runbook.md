# UAT Phase 0 Runbook — Point the stack at the 2 real test ATMs

This is the cutover checklist run **once the 2 UAT VMs are up and the bank has
provided the test-ATM IPs, SNMP community, and (optionally) the ISO 8583 switch
feed**. It turns the simulator-shaped templates/inventory into real-ATM-shaped
data. Everything here is executed on UAT-01 unless noted.

## 0. Action items to send to IT / bank NOW (while waiting for the UAT VMs)
These are the only things that unblock the cutover and can be requested today:
- **Network / firewall openings** (raise with IT / network team):
  - UAT-01 (`172.22.10.45`) → test ATMs on **UDP 161** (SNMP poll).
  - Test ATMs → **UAT-01:514** (UDP/TCP) for EJ forwarding.
  - ISO 8583 switch → **UAT-02:9876** (TCP) — only if real transactions are in scope.
- **SNMP credentials** (request from the ATM / Channel Support team):
  - SNMP version per test ATM: **v2c** or **v3**?
  - v2c: the **community string** for each test ATM.
  - v3: **security name**, **auth protocol + passphrase**, **priv protocol + passphrase**, **security level** (authNoPriv / authPriv).
  - Until these arrive, the seed holds `CHANGE_ME` placeholders (Steps 2–3 cannot run).

## 0. Prerequisites (bank / network)
- UAT-01 (`172.22.10.45`) can reach both test ATMs on **UDP 161** (VLAN/firewall open).
- The 2 test ATMs forward EJ to **UAT-01:514** (UDP/TCP) — confirm with the ATM/Channel Support team.
- OpenSearch on UAT-02 (`172.22.10.60`) is reachable from UAT-01 on **9200**.
- ISO 8583 switch can reach **UAT-02:9876** (only if using real transactions).

## 1. Confirm inventory + Zabbix reachability
```bash
# ATMs must already be seeded (fresh setup does this) — verify:
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT atm_id, vendor, ip_address, status FROM atm_locations;"
# Zabbix API reachable?
curl -s -o /dev/null -w "%{http_code}\n" http://172.22.10.45:8080/api_jsonrpc.php
```

## 2. Set the real SNMP community / version
The seed uses a `CHANGE_ME` placeholder. Replace it with the bank-provided string.
For **v2c**:
```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix -c \
  "UPDATE atm_locations SET snmp_community='<REAL_COMMUNITY>', snmp_version=2 WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');"
```
For **v3**, `atm_locations` only stores `snmp_version` (it has no columns for v3
credentials), so set the version in the DB and supply the v3 secrets via environment
variables when you run the sync in Step 6:
```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix -c \
  "UPDATE atm_locations SET snmp_version=3 WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');"
export SNMPV3_USER=<user> SNMPV3_AUTH_PROTO=SHA SNMPV3_AUTH_PASS=<authpass> \
       SNMPV3_PRIV_PROTO=AES SNMPV3_PRIV_PASS=<privpass> SNMPV3_LEVEL=authPriv
```

## 3. Capture a real SNMP walk (per vendor)
> UAT polling uses NAT: from UAT-01 poll `10.41.30.114` (NCR) and
> `10.41.30.115` (GRG). Real IPs `172.23.248.4/.6` stay the asset identity
> in `atm_locations`; Zabbix SNMP interfaces use the NAT IPs.
```bash
# v2c (via NAT from UAT-01):
./config/snmp/capture_walk.sh NCR 10.41.30.114 <REAL_COMMUNITY>
./config/snmp/capture_walk.sh GRG 10.41.30.115 <REAL_COMMUNITY>
# v3 (community arg is ignored):
./config/snmp/capture_walk.sh NCR 172.23.248.4 '' -v3 -u <user> -a SHA -A <authpass> -x AES -X <privpass> -l authPriv
./config/snmp/capture_walk.sh GRG 172.23.248.6 '' -v3 -u <user> -a SHA -A <authpass> -x AES -X <privpass> -l authPriv
```
Sanity-check reachability first (v2c shown; use `-v3 …` flags for v3):
```bash
snmpwalk -v2c -c <REAL_COMMUNITY> -On 10.41.30.114 .1.3.6.1.2.1.1
```

### 3.1 Walk evidence — NCR test ATM, 2026-09-30 (UAT-01 → NAT .114, community `public`)

Full walk saved on UAT-01 as `snmpwalk_output.txt` (repo-ignored; kept off-git).
Findings:

- **Target confirmed genuine NCR ATM** (not just a Windows box): APTRA stack
  present — Advance NDC, XFS CDM/CIM/PIN/TTU/BRM/cash-dispenser providers
  (`USBCurrencyDispenser`, `PcGBRU`, `USBMediaDispenser`), `aptraResourceManager`.
- **Only standard MIBs exposed** (system/interfaces/IP/TCP/UDP/host-resources).
  **No NCR enterprise OIDs** — cassette counters are NOT visible through this
  agent view. → **Ask ATM team:** which agent/port/community exposes the
  NCR/APTRA MIB, then walk that subtree and feed §4.
- **`zabbix_agentd.exe` already installed and listening on 10050** (plus the
  legacy NetXMS agent). OS-level metrics via agent items are unblocked today;
  test with `nc -zv -w 5 10.41.30.114 10050`.
- Box is SYN-flooding `192.168.21.218:10051` (unanswered) — stale pointer to a
  previous Zabbix server; ask ATM team to repoint at UAT-01 when ready.
- **Path finding 2026-09-30:** UAT-01 reaches the ATMs **only via NAT**
  (no route to `172.23.248.0/24`). NAT forwards UDP 161 but **not** TCP
  10050 (`nc` times out both ways) — so Zabbix agent items are unreachable
  until the network team forwards 10050 or a route is added. Standard:
  real IPs stay the identity in `atm_locations`, NAT IPs on Zabbix
  interfaces. SNMP OS metrics work today; agent items pending.
- Hygiene: sysName still `TESTMACHINE`, empty contact/location; community is
  `public` read-only (zero SetVars observed — rotate before go-live).
- Decision: create the Zabbix host with OS metrics first; hardware/cassette
  items only after the NCR MIB lands (blocks `generate_zabbix_templates.py`
  guard and the vendor-isolation end-to-end test).
- **Verdict 2026-09-30 (ATM team): cassette data flows via the NetXMS agent,
  NOT SNMP.** The Windows SNMP view will never carry cassette counters.
  Path: ask which command/script NetXMS runs for the counts → ship the same
  as Zabbix-agent `UserParameter`s on the ATMs (agent already installed).
  Fallback: read levels from the NetXMS server DB/API into our pipeline.
  SNMP stays for OS/network metrics only.

## 4. Fill `config/snmp/oid_mapping.csv`
Start from the tracked header-only template, then add one row per monitored OID:
```bash
cp config/snmp/oid_mapping.csv.example config/snmp/oid_mapping.csv
```
Row format: `sim_oid,real_oid,item_name,vendor,data_type`
- `sim_oid` = the suffix used in the current templates (e.g. `1.1.0` for ATM Operational Status).
- `real_oid` = the real vendor enterprise OID from the walk (e.g. `.1.3.6.1.4.1.37513.1.1.0` for NCR ATM Operational Status).

## 5. Remap the Zabbix templates (one command)
```bash
python3 scripts/generate_zabbix_templates.py --dry-run   # review mapped/unmapped counts
python3 scripts/generate_zabbix_templates.py             # rewrites in place; backs up to *.simulator.bak
```
Every `<snmp_oid>` still on the simulator root `1.3.6.1.4.1.99999.*` that has a
mapping row is replaced with the real OID. **Guardrail:** in a real (non `--dry-run`)
run, if *any* simulator OID remains unmapped the generator aborts **before writing
any file** (exits 1). You must complete `oid_mapping.csv` so all monitored OIDs are
covered, then re-run; the run only succeeds once every item is mapped.

> If the walk forces `status_map.json` code changes, re-render triggers first
> (`python3 scripts/build_zabbix_triggers.py` — thresholds read the map, so
> they stay consistent), then import: the sync in Step 6 brings triggers along.

## 6. Re-import templates + register hosts
The `state-manager` image must be rebuilt first — its `state_manager` now imports
`zabbix_modules`, so the old image would crash on import:
```bash
docker compose build state-manager
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
```
This imports the remapped templates and creates Zabbix hosts named by `atm_id`
(`ATM-TEST-NCR`, `ATM-TEST-GRG`) with SNMP v2c (or v3 if `snmp_version=3` and the
`SNMPV3_*` env vars are exported — see Step 2), port 161, and the real community.
After a successful remap, **commit the rewritten templates to `uat-prep`** so the
cutover is reproducible:
```bash
git add config/zabbix/template_ncr_snmp.xml config/zabbix/template_grg_snmp.xml
git commit -m "UAT: remap Zabbix templates to real ATM OIDs"
```

## 7. Verify collection
- Zabbix → **Monitoring → Latest data** for `ATM-TEST-NCR` / `ATM-TEST-GRG`: items should populate (not "Not supported").
- `state-manager` (every 30 s) writes `atm_current_state`; confirm it is not all `UNREACHABLE`:
  ```bash
  docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT atm_id, state FROM atm_current_state;"
  ```
- `network-correlator` writes `atm_network_metrics` from real `atm.net.latency`/`packetloss`/`link`.
- Report Portal → ATM detail page live cassette/hardware/state widgets now read the same Zabbix item last values (via the Zabbix API), so they populate from the real ATMs.

## 8. Tune the EJ parser
```bash
tcpdump -i any udp port 514 -A -nn   # on UAT-01, trigger an EJ event on a test ATM
```
Capture one real EJ line, then extend the grok patterns in
`config/opensearch/atm_ej_parser.json` for the NCR/GRG format. Until then, raw
lines are retained in `ej_raw` and tagged `ej_parsed:false` (searchable in
OpenSearch Dashboards).

## 9. (Optional) Real ISO 8583 transactions
In `.env.uat` set `ISO8583_MODE=tcp` and re-create the gateway:
```bash
ISO8583_MODE=tcp docker compose -f docker-compose.gateway.yml up -d iso8583-gateway
```
The bank must route the 2 test ATMs' ISO 8583 traffic to UAT-02:9876.

## 9b. WMI part-state collection (NCR Windows cores)

SNMP cannot serve NCR data on this fleet (agent view ends before `.191`,
proven 2026-09-30) — collect device state through the Zabbix agent instead.
No new service, port, or software: one script + config snippet on the ATM.

On the test ATM (read-only rehearsal first — run the script by hand, it
only queries):

```powershell
mkdir C:\monitor
# copy scripts/atm-wmi-collector.ps1 -> C:\monitor\
powershell -NoProfile -NonInteractive -File C:\monitor\atm-wmi-collector.ps1
# expect one JSON line: {"ts":"...","parts":{"First Cassette":{...},...}}
```

Then append `config/zabbix/atm-agent-userparams.conf` to
`C:\Program Files\Zabbix Agent\zabbix_agentd.conf`, restart the agent
service, and prove it from UAT-01 (needs TCP 10050 through the NAT):

```bash
zabbix_get -s 10.41.30.114 -k "atm.parts"
zabbix_get -s 10.41.30.114 -k "atm.part[First Cassette]"
```

If the agent log shows PowerShell denials, the McAfee/Solidcore whitelist
must allow `C:\monitor\atm-wmi-collector.ps1` (bank change control) —
preferred over `-ExecutionPolicy Bypass` long-term is signing the script
and switching the UserParameters to `-ExecutionPolicy RemoteSigned`.
Template items (master + JSONPath dependents per part) are built against
the live JSON shape in the next step.

**Proven 2026-10-01 (agent-side, TESTMACHINE):** local
`zabbix_get -s 127.0.0.1 -k "atm.parts"` returns all 33 parts
(cassettes report `part_status 4 / replenishment_status 1`, Transport
reports `part_status 2`). Gotchas hit along the way: `atm.parts[*]`
collides with `atm.parts` (use singular `atm.part[*]`), and the agent's
`Server=` ACL must include `127.0.0.1` for local tests
(`connection ... rejected, allowed hosts` in the agent log names the fix).
UAT-01 polling still waits on the TCP 10050 NAT forward.

**Proven 2026-10-09 (collector output framing):** `Write-Output` of the
~3.8KB JSON line wraps at console width (~80 cols) with literal CRLF,
snapping key names mid-token (`Card Captu<CRLF>re Bin`) — payload fails
`json.tool` at char 159, all 41 dependents die (JSONPath control-character
+ Duktape offset-161 errors are downstream symptoms of the same broken
document). Fix is `[System.Console]::WriteLine(...)` (bypasses formatting,
one clean line). Signature: `wc -l` shows ~48 lines pre-fix, 1 post-fix.

**Calibrated 2026-10-12 (RDP legend hunt, TESTMACHINE — no guessing):**
Part legend (`NCR_DevDevicePart` qualifiers): PartStatus 0 OK, 1 Warning,
2 Error, 3 Not Configured, 4 Unknown, 5 Accessed; PMStatus 0 OK, 1 PM
Required; ReplenishmentStatus ValueMap {0,1,2,3} = OK, Warning, Error,
Unknown (provider text wrongly says "4 - Unknown" — ValueMap wins).
Device legend (`NCR_DevDevice.DeviceStatus`): 0 OK, 1 Warning,
2 Unavailable, 3+ Unknown. `NCR_ATM` is unpopulated (4 empty props) —
device-level state comes from `NCR_Device` (14 instances), never `NCR_ATM`.
Duplicate names are physical pairs (Control Board dev 65674/65722,
Transport dev 65673/65674) — collector binds bare name to lowest
(DeviceInstance,PartId), suffixes rest " (2)".
Live cross-validation: RPNT printer device Unavailable(2) == Paper
replenishment Error(2); CASH dispenser device Warning(1) alongside
Transport(2) part Error — two independent sources agree.
ATM-state policy (parts-only until device items land): any part Error →
HARDWARE_FAULT, else IN_SERVICE ("reporting and fault-free", not
"dispensing"); supervisor/out-of-cash explicitly unsourced (APTRA app
state and note counts exist nowhere in this provider — never infer).

## 9c. Import + link the WMI template

```bash
python3 scripts/generate_wmi_template.py   # regenerates config/zabbix/template_atm_wmi.xml
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
```

Then link **Dashen Bank ATM WMI Parts** to the test host in Zabbix
(Configuration → Hosts → Templates) and watch Latest Data fill within
~5 min. Deliberately **no triggers yet**: `PartStatus`/`ReplenishmentStatus`
code meanings are uncalibrated — observe one full cycle (including a
cassette state you can explain, e.g. post-CIT) before writing alert
thresholds, or the first page will fire on healthy ATMs.

## 10. Smoke test
- Grafana dashboards show live state + transactions for the 2 ATMs.
- Trigger a fault → Zabbix problem → GLPI ticket.
- Report Portal → EJ Search returns parsed (or raw) records.

## Rollback
If a remap goes wrong: `cp config/zabbix/template_ncr_snmp.xml.simulator.bak config/zabbix/template_ncr_snmp.xml` (same for GRG), then re-run step 6.
