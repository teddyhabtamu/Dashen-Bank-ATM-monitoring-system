-- Stop the ATM Status Distribution chart counting ATMs that no longer exist.
--
-- Why: atm_current_state on UAT has a primary key but NO foreign key to
-- atm_locations (config/postgres/uat_tables.sql omits it; the reference schema
-- config/postgres/atm_custom_tables.sql has fk_acs_atm_id). Deleting an ATM from
-- atm_locations therefore leaves its state row behind forever, and
-- routes.py's donut counted atm_current_state directly - so the dashboard
-- reported 4 unreachable machines while ATM Registration listed 3.
--
-- ON DELETE CASCADE is right here: a state row is a cached status, not financial
-- evidence. (cash_replenishments deliberately has no cascade for the opposite
-- reason - see admin.py's ATM_DELETE comment.)
--
-- Safe to run repeatedly. Takes a backup first per docs/backup-and-restore.md.
--
--   docker exec -i zabbix-db psql -U zabbix -d zabbix < scripts/migrate_state_fk.sql

-- 1. Confirm before changing anything.
SELECT s.atm_id, s.state, s.last_seen AS orphan_state_row
  FROM atm_current_state s
  LEFT JOIN atm_locations l ON l.atm_id = s.atm_id
 WHERE l.atm_id IS NULL;

-- 2. Remove the orphans. Without this the FK below cannot be created.
DELETE FROM atm_current_state s
 WHERE NOT EXISTS (SELECT 1 FROM atm_locations l WHERE l.atm_id = s.atm_id);

-- 3. Add the constraint. Wrapped so a host that already has it is a no-op.
DO $$ BEGIN
    ALTER TABLE atm_current_state
        ADD CONSTRAINT fk_acs_atm_id
        FOREIGN KEY (atm_id) REFERENCES atm_locations(atm_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 4. Confirm: no orphans, and every state row belongs to a registered ATM.
SELECT (SELECT count(*) FROM atm_current_state) AS state_rows,
       (SELECT count(*) FROM atm_locations WHERE LOWER(COALESCE(status,'active'))='active') AS active_atms,
       (SELECT count(*) FROM atm_current_state s
          WHERE NOT EXISTS (SELECT 1 FROM atm_locations l WHERE l.atm_id = s.atm_id)) AS orphans;
