-- Move the Report Portal's tables out of Zabbix's own database.
--
-- WHY: docker-compose sets DB_NAME="zabbix" for the portal, cash-watcher,
-- anomaly-detector, correlator and state-manager. So atm_transactions,
-- atm_current_state, app_users, audit_log, txn_source_policy and the rest live
-- in the SAME database as Zabbix's configuration and history. Consequences:
--
--   * a Zabbix upgrade, restore or schema reset can clobber portal data, and a
--     portal migration runs in Zabbix's schema transaction space;
--   * backup and restore are coupled - neither system can be recovered alone;
--   * the two have different release cadences and different support lifetimes,
--     and that coupling is invisible until the first bad upgrade.
--
-- This is deliberately a COPY, not a move. Nothing is deleted, the old
-- database is left intact, and switching the application over is a separate
-- step you take deliberately after verifying the copy.
--
-- ── STAGED USE ───────────────────────────────────────────────────────────────
-- Run the sections ONE AT A TIME, verifying between each. Days, not minutes.
--
--   Section 1  (safe, repeatable)  create the database and copy the tables
--   Section 2  (safe, repeatable)  verify the copy row-for-row
--   Section 3  (manual)            point ONE service at the new database
--   Section 4  (manual)            switch the portal; KEEP the old database
--   Section 5  (later)             decommission - NOT included in this file
--
--   docker exec -i zabbix-db psql -U zabbix -d zabbix < scripts/split_portal_db.sql
--
-- To run a single section, delete the others or comment them out. psql runs the
-- whole file top to bottom, so section 5 being absent is deliberate.
--
-- Rollback for sections 1-4: nothing has been deleted. Set DB_NAME back to
-- "zabbix" in docker-compose and restart. That is the entire rollback.

-- ══ SECTION 1 ═════════════════════════════════════════════════════════════
-- Create the database and copy every portal-owned table into it.
-- Safe and repeatable: CREATE DATABASE IF NOT EXISTS is emulated, and the
-- CREATE TABLE ... AS runs harmlessly if the table already exists.

SELECT 'CREATE DATABASE portal' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'portal')
\gexec

\connect portal

-- Portal-owned tables only. Zabbix's own tables are deliberately NOT listed:
-- nothing here should copy or touch Zabbix configuration or history.
CREATE TABLE IF NOT EXISTS atm_locations          AS TABLE zabbix.atm_locations;
CREATE TABLE IF NOT EXISTS atm_transactions       AS TABLE zabbix.atm_transactions;
CREATE TABLE IF NOT EXISTS atm_current_state      AS TABLE zabbix.atm_current_state;
CREATE TABLE IF NOT EXISTS atm_state_history      AS TABLE zabbix.atm_state_history;
CREATE TABLE IF NOT EXISTS atm_anomalies          AS TABLE zabbix.atm_anomalies;
CREATE TABLE IF NOT EXISTS atm_incidents          AS TABLE zabbix.atm_incidents;
CREATE TABLE IF NOT EXISTS atm_devices            AS TABLE zabbix.atm_devices;
CREATE TABLE IF NOT EXISTS cash_replenishments    AS TABLE zabbix.cash_replenishments;
CREATE TABLE IF NOT EXISTS app_users              AS TABLE zabbix.app_users;
CREATE TABLE IF NOT EXISTS audit_log              AS TABLE zabbix.audit_log;
CREATE TABLE IF NOT EXISTS fault_type_map         AS TABLE zabbix.fault_type_map;
CREATE TABLE IF NOT EXISTS status_map             AS TABLE zabbix.status_map;
CREATE TABLE IF NOT EXISTS txn_source_policy      AS TABLE zabbix.txn_source_policy;
CREATE TABLE IF NOT EXISTS fleet_report_cache     AS TABLE zabbix.fleet_report_cache;
CREATE TABLE IF NOT EXISTS scheduled_reports      AS TABLE zabbix.scheduled_reports;

-- Sequences: CREATE TABLE AS copies data but NOT the sequence default, so an
-- insert with no explicit id would collide with what was copied. This is the
-- step that is easy to miss and shows up as "duplicate key" weeks later.
SELECT setval(pg_get_serial_sequence('atm_transactions', 'id'),
              COALESCE((SELECT max(id) FROM atm_transactions), 1))
 WHERE pg_get_serial_sequence('atm_transactions', 'id') IS NOT NULL;
SELECT setval(pg_get_serial_sequence('audit_log', 'id'),
              COALESCE((SELECT max(id) FROM audit_log), 1))
 WHERE pg_get_serial_sequence('audit_log', 'id') IS NOT NULL;
SELECT setval(pg_get_serial_sequence('atm_incidents', 'id'),
              COALESCE((SELECT max(id) FROM atm_incidents), 1))
 WHERE pg_get_serial_sequence('atm_incidents', 'id') IS NOT NULL;
SELECT setval(pg_get_serial_sequence('cash_replenishments', 'id'),
              COALESCE((SELECT max(id) FROM cash_replenishments), 1))
 WHERE pg_get_serial_sequence('cash_replenishments', 'id') IS NOT NULL;

-- Indexes. CREATE TABLE AS does not bring them over, and without them every
-- portal page degrades to a sequential scan.
CREATE INDEX IF NOT EXISTS idx_txn_atm_time        ON atm_transactions(atm_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_status_type_time ON atm_transactions(status, txn_type, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_recorded_status  ON atm_transactions(recorded_at DESC, status);
CREATE INDEX IF NOT EXISTS idx_txn_type_status      ON atm_transactions(txn_type, status, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_branch_time      ON atm_transactions(branch, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_card_time        ON atm_transactions(card_masked, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_loc_branch           ON atm_locations(branch);
CREATE INDEX IF NOT EXISTS idx_loc_status           ON atm_locations(status);
CREATE INDEX IF NOT EXISTS idx_loc_region_city      ON atm_locations(region, city);
CREATE INDEX IF NOT EXISTS idx_loc_terminal         ON atm_locations(terminal_id);
CREATE INDEX IF NOT EXISTS idx_audit_performed      ON audit_log(performed_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_username       ON audit_log(username);
CREATE INDEX IF NOT EXISTS idx_audit_action         ON audit_log(action);

-- ══ SECTION 2 ═════════════════════════════════════════════════════════════
-- Verify the copy. Every row count must match or the copy is not trustworthy
-- and section 3 must not happen.

\connect zabbix

SELECT l.relname AS table_name,
       (SELECT count(*) FROM zabbix.pg_class zc
          JOIN pg_catalog.pg_attribute a ON a.attrelid = zc.oid AND a.attnum > 0
         WHERE zc.relname = l.relname) AS in_zabbix,
       (SELECT count(*) FROM portal.pg_class pc
          JOIN pg_catalog.pg_attribute a ON a.attrelid = pc.oid AND a.attnum > 0
         WHERE pc.relname = l.relname) AS in_portal
  FROM pg_catalog.pg_class l
  JOIN pg_catalog.pg_namespace n ON n.oid = l.relnamespace AND n.nspname = 'zabbix'
 WHERE l.relkind = 'r' AND l.relname IN (
       'atm_locations','atm_transactions','atm_current_state','atm_state_history',
       'atm_anomalies','atm_incidents','atm_devices','cash_replenishments',
       'app_users','audit_log','fault_type_map','txn_source_policy','fleet_report_cache')
 ORDER BY 1;

-- Row counts on the tables where a mismatch would actually matter.
SELECT 'atm_locations'     AS t, (SELECT count(*) FROM zabbix.atm_locations)     AS zabbix_db,
                            (SELECT count(*) FROM portal.atm_locations)      AS portal_db
UNION ALL
SELECT 'atm_transactions',  (SELECT count(*) FROM zabbix.atm_transactions),  (SELECT count(*) FROM portal.atm_transactions)
UNION ALL
SELECT 'app_users',         (SELECT count(*) FROM zabbix.app_users),         (SELECT count(*) FROM portal.app_users)
UNION ALL
SELECT 'txn_source_policy', (SELECT count(*) FROM zabbix.txn_source_policy), (SELECT count(*) FROM portal.txn_source_policy);

-- Sequences must be AHEAD of the copied data, or the next insert collides.
SELECT 'atm_transactions' AS t, last_value FROM atm_transactions_id_seq
UNION ALL SELECT 'audit_log', last_value FROM audit_log_id_seq;

\connect portal

-- ══ NOTE: NO SECTION 3 OR 4 IN THIS FILE ═══════════════════════════════════
-- Sections 3 and 4 are deliberately absent. They change what the application
-- connects to, and they are the only steps here that can affect the running
-- system. Do them by hand, one at a time, verifying between:
--
--   Section 3 - point ONE non-production service at the new database:
--       DB_NAME: "portal"
--     e.g. UAT-02's report-portal, or a one-off container:
--       docker run --rm --network atm-monitoring_default \
--         -e DB_NAME=portal -e DB_HOST=postgres -e DB_USER=zabbix \
--         -e DB_PASS="$DB_PASS" report-portal \
--         python3 -c "from db import get_db; \
--           c=get_db().cursor(); c.execute('SELECT count(*) FROM atm_locations'); \
--           print(c.fetchone())"
--     Expect the SAME number as section 2 reported for zabbix.atm_locations.
--
--   Section 4 - switch the portal itself:
--       DB_NAME: "portal"      in docker-compose.yml
--       docker compose up -d report-portal
--     LEAVE THE OLD DATABASE ALONE. Rollback is one env var and a restart.
--
--   Only after days of clean operation, and with a verified backup of BOTH
--   databases, should anything in the zabbix database be dropped.
--
-- Reminder for whoever does section 4: the Zabbix containers must keep
-- DB_NAME=zabbix. Only the portal services move. Changing it globally would
-- point Zabbix itself at an empty database.