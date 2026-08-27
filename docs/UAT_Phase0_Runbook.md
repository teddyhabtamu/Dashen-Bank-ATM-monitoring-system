# UAT Phase 0 Runbook — Point the stack at the 2 real test ATMs

This is the cutover checklist run **once the 2 UAT VMs are up and the bank has
provided the test-ATM IPs, SNMP community, and (optionally) the ISO 8583 switch
feed**. It turns the simulator-shaped templates/inventory into real-ATM-shaped
data. Everything here is executed on UAT-01 unless noted.

## 0. Action items to send to IT / bank NOW (while waiting for the UAT VMs)
These are the only things that unblock the cutover and can be requested today:
- **Network / firewall openings** (raise with IT / network team):
  - UAT-01 (`172.26.208.176`) → test ATMs on **UDP 161** (SNMP poll).
  - Test ATMs → **UAT-01:514** (UDP/TCP) for EJ forwarding.
  - ISO 8583 switch → **UAT-02:9876** (TCP) — only if real transactions are in scope.
- **SNMP credentials** (request from the ATM / Channel Support team):
  - SNMP version per test ATM: **v2c** or **v3**?
  - v2c: the **community string** for each test ATM.
  - v3: **security name**, **auth protocol + passphrase**, **priv protocol + passphrase**, **security level** (authNoPriv / authPriv).
  - Until these arrive, the seed holds `CHANGE_ME` placeholders (Steps 2–3 cannot run).

## 0. Prerequisites (bank / network)
- UAT-01 (`172.26.208.176`) can reach both test ATMs on **UDP 161** (VLAN/firewall open).
- The 2 test ATMs forward EJ to **UAT-01:514** (UDP/TCP) — confirm with the ATM/Channel Support team.
- OpenSearch on UAT-02 (`172.26.21.50`) is reachable from UAT-01 on **9200**.
- ISO 8583 switch can reach **UAT-02:9876** (only if using real transactions).

## 1. Confirm inventory + Zabbix reachability
```bash
# ATMs must already be seeded (fresh setup does this) — verify:
docker exec zabbix-db psql -U zabbix -d zabbix -c "SELECT atm_id, vendor, ip_address, status FROM atm_locations;"
# Zabbix API reachable?
curl -s -o /dev/null -w "%{http_code}\n" http://172.26.208.176:8080/api_jsonrpc.php
```

## 2. Set the real SNMP community / version
The seed uses a `CHANGE_ME` placeholder. Replace it with the bank-provided string.
For **v2c**:
```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix -c \
  "UPDATE atm_locations SET snmp_community='<REAL_COMMUNITY>', snmp_version='v2c' WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');"
```
For **v3**, `atm_locations` only stores `snmp_version` (it has no columns for v3
credentials), so set the version in the DB and supply the v3 secrets via environment
variables when you run the sync in Step 6:
```bash
docker exec -i zabbix-db psql -U zabbix -d zabbix -c \
  "UPDATE atm_locations SET snmp_version='v3' WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');"
export SNMPV3_USER=<user> SNMPV3_AUTH_PROTO=SHA SNMPV3_AUTH_PASS=<authpass> \
       SNMPV3_PRIV_PROTO=AES SNMPV3_PRIV_PASS=<privpass> SNMPV3_LEVEL=authPriv
```

## 3. Capture a real SNMP walk (per vendor)
```bash
# v2c:
./config/snmp/capture_walk.sh NCR 172.22.248.4 <REAL_COMMUNITY>
./config/snmp/capture_walk.sh GRG 172.23.248.6 <REAL_COMMUNITY>
# v3 (community arg is ignored):
./config/snmp/capture_walk.sh NCR 172.22.248.4 '' -v3 -u <user> -a SHA -A <authpass> -x AES -X <privpass> -l authPriv
./config/snmp/capture_walk.sh GRG 172.23.248.6 '' -v3 -u <user> -a SHA -A <authpass> -x AES -X <privpass> -l authPriv
```
Sanity-check reachability first (v2c shown; use `-v3 …` flags for v3):
```bash
snmpwalk -v2c -c <REAL_COMMUNITY> -On 172.22.248.4 .1.3.6.1.2.1.1
```

## 4. Fill `config/snmp/oid_mapping.csv`
For every OID the walk exposes that we monitor, add a row:
`sim_oid,real_oid,item_name,vendor,data_type`
- `sim_oid` = the suffix used in the current templates (e.g. `1.1.0` for ATM Operational Status).
- `real_oid` = the real vendor enterprise OID from the walk (e.g. `.1.3.6.1.4.1.37513.1.1.0`).
The first example row is NCR ATM Operational Status — extend it, do **not** delete it.

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

## 6. Re-import templates + register hosts
The `state-manager` image must be rebuilt first — its `state_manager` now imports
`zabbix_modules`, so the old image would crash on import:
```bash
docker compose build state-manager
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
```
This imports the remapped templates and creates Zabbix hosts named by `atm_id`
(`ATM-TEST-NCR`, `ATM-TEST-GRG`) with SNMP v2c (or v3 if `snmp_version='v3'` and the
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

## 10. Smoke test
- Grafana dashboards show live state + transactions for the 2 ATMs.
- Trigger a fault → Zabbix problem → GLPI ticket.
- Report Portal → EJ Search returns parsed (or raw) records.

## Rollback
If a remap goes wrong: `cp config/zabbix/template_ncr_snmp.xml.simulator.bak config/zabbix/template_ncr_snmp.xml` (same for GRG), then re-run step 6.
