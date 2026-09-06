-- Seed the 2 UAT real test ATMs (4 Kilo Building) into atm_locations.
-- Run on UAT-01 once the DB is up:
--   docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/seed_uat_atms.sql
--
-- Before syncing to Zabbix, set the real SNMP community (from ATM/Channel Support):
--   docker exec -i zabbix-db psql -U zabbix -d zabbix -c \
--     "UPDATE atm_locations SET snmp_community='<REAL_COMMUNITY>' WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG');"
--
-- Then export the CSV the sync script reads, and register the Zabbix hosts:
--   docker exec zabbix-db psql -U zabbix -d zabbix -c "COPY atm_locations TO STDOUT WITH CSV HEADER" > config/postgres/atm_locations.csv
--   python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
--
-- The Zabbix host name equals atm_id, which is what state-manager / network-correlator query.

INSERT INTO atm_locations
  (atm_id, atm_name, branch, district, city, region, terminal_id, vendor, model, status, ip_address, snmp_community, snmp_version)
VALUES
  ('ATM-TEST-NCR','4 Kilo Test NCR','4 Kilo Building','Kirkos','Addis Ababa','Addis Ababa','TID-UAT-NCR','NCR','SelfServ 34','active','172.22.248.4','CHANGE_ME',2),
  ('ATM-TEST-GRG','4 Kilo Test GRG','4 Kilo Building','Kirkos','Addis Ababa','Addis Ababa','TID-UAT-GRG','GRG','GRG AD2600','active','172.23.248.6','CHANGE_ME',2)
ON CONFLICT (atm_id) DO UPDATE SET
  atm_name = EXCLUDED.atm_name, branch = EXCLUDED.branch, district = EXCLUDED.district,
  city = EXCLUDED.city, region = EXCLUDED.region, terminal_id = EXCLUDED.terminal_id,
  vendor = EXCLUDED.vendor, model = EXCLUDED.model, status = EXCLUDED.status,
  ip_address = EXCLUDED.ip_address, snmp_community = EXCLUDED.snmp_community,
  snmp_version = EXCLUDED.snmp_version;

-- 4 Kilo Building, Addis Ababa (approx; refine with survey GPS if needed).
UPDATE atm_locations SET latitude = 9.0054, longitude = 38.7636
  WHERE atm_id IN ('ATM-TEST-NCR','ATM-TEST-GRG') AND latitude IS NULL;
