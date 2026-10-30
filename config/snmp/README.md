# config/snmp — Real-ATM SNMP onboarding (UAT prep)

This directory holds the artifacts needed to point the monitoring stack at the
bank's **real test ATMs** over SNMP (UAT target). These are scaffolds — the
real values are filled in during UAT once the ATM team provides IPs + community.

## Flow (see `docs/uat-pilot-checklist.md` Phase 0)

1. **Capture** a walk from each test ATM (via NAT from UAT-01):
    ```bash
    ./capture_walk.sh NCR 10.41.30.114 <community>
    ./capture_walk.sh GRG 10.41.30.115 <community>
    ```
    Produces `real_atm_snmpwalk_<VENDOR>.txt`.

2. **Map** real OIDs → our Zabbix item names in `oid_mapping.csv`
   (columns: `sim_oid,real_oid,item_name,vendor,data_type`). The first row is an
   example; replace with the real enterprise OIDs from the walk output.

3. **Clone** the Zabbix SNMP templates (`template_ncr_snmp.xml` /
   `template_grg_snmp.xml`) to v2 variants and swap the HTTP-agent items for SNMP
   items using the mapped OIDs + real community.

4. **Register** the host(s) in Zabbix so they go GREEN with real data:
   ```bash
   python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
   ```
   (the two test ATMs — `ATM-TEST-NCR` @ 172.23.248.4, `ATM-TEST-GRG` @ 172.23.248.6 —
   must exist in the `atm_locations` inventory first; import them via
   `scripts/import_atms_from_excel.py --apply` from Abinet's Excel).

## Test ATM endpoints (real IP = identity; NAT IP = UAT polling path)
- NCR ATM: real 172.23.248.4, poll via 10.41.30.114 (SNMP UDP 161)
- GRG ATM Terminal: real 172.23.248.6, poll via 10.41.30.115 (SNMP UDP 161)

The Network/Cloud team must open UDP 161 from UAT-01 (172.22.10.45)
to both NAT IPs. `atm_locations.ip_address` keeps the real IP;
Zabbix SNMP interfaces use the NAT IPs.
