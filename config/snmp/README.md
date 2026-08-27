# config/snmp — Real-ATM SNMP onboarding (UAT prep)

This directory holds the artifacts needed to point the monitoring stack at the
bank's **real test ATMs** over SNMP (UAT target). These are scaffolds — the
real values are filled in during UAT once the ATM team provides IPs + community.

## Flow (see `docs/uat-pilot-checklist.md` Phase 0)

1. **Capture** a walk from each test ATM:
   ```bash
   ./capture_walk.sh NCR 172.22.248.4 <community>
   ./capture_walk.sh GRG 172.23.248.6 <community>
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
   (the two test ATMs — `ATM-TEST-NCR` @ 172.22.248.4, `ATM-TEST-GRG` @ 172.23.248.6 —
   must exist in the `atm_locations` inventory first; import them via
   `scripts/import_atms_from_excel.py --apply` from Abinet's Excel).

## Test ATM endpoints (from docs/UAT_Migration_Guide.md Step F)
- NCR ATM: 172.22.248.4  (SNMP UDP 161)
- GRG ATM Terminal: 172.23.248.6  (SNMP UDP 161)

The Network/Cloud team must open UDP 161 from UAT-01 (172.26.208.176, VLAN 4029)
to both ATM IPs.
