-- Add the per-ATM EJ journal hostname.
--
-- Why: scripts/pull_ej_logs.sh used to carry a hardcoded ATM list, so adding
-- a machine meant editing a script. The list now comes from atm_locations
-- (scripts/ej_pull_targets.py), which needs the machine hostname because the
-- NCR journal file is named <date>_<HOSTNAME>_EJDATA.log.
--
-- Safe to run repeatedly: IF NOT EXISTS, no data rewrite, no locks beyond a
-- brief ACCESS EXCLUSIVE on the table (instant for 1,202 rows). Takes a
-- backup first per docs/backup-and-restore.md.

ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS ej_hostname character varying(64);

COMMENT ON COLUMN atm_locations.ej_hostname IS
  'Machine hostname as it appears in the NCR journal filename <date>_<HOST>_EJDATA.log. Set by an admin; empty means "no journal pull for this ATM".';

-- Seed the UAT pilot box so the proven hourly pull keeps working the moment
-- this migration lands (no-ops on any host that does not have it).
UPDATE atm_locations SET ej_hostname = 'TESTMACHINE' WHERE atm_id = 'ATM-TEST-NCR';

-- Confirm before moving on: every row the pull will use must have a hostname.
SELECT count(*) AS ncr_active_missing_hostname
  FROM atm_locations
 WHERE UPPER(vendor) = 'NCR'
   AND LOWER(COALESCE(status, 'active')) = 'active'
   AND COALESCE(ip_address, '') <> ''
   AND COALESCE(ej_hostname, '') = '';