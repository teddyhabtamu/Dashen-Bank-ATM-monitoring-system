-- UAT schema for tables no service auto-creates on a fresh deploy.
-- (atm_locations is seeded separately via seed_uat_atms.sql.)
-- Run once on UAT-01 after postgres is up:
--   docker exec -i zabbix-db psql -U zabbix -d zabbix < config/postgres/uat_tables.sql

-- Written by the ISO 8583 gateway (simulation now, real switch later) and
-- read by anomaly-detector. DDL mirrored from simulators/txn_engine.py
-- (the simulator that owned it but does not run in UAT).
CREATE TABLE IF NOT EXISTS atm_transactions (
    id SERIAL PRIMARY KEY,
    recorded_at TIMESTAMP DEFAULT NOW(),
    atm_id VARCHAR(20),
    terminal_id VARCHAR(20),
    branch VARCHAR(100),
    txn_type VARCHAR(30),
    card_masked VARCHAR(20),
    amount DECIMAL(15,2),
    currency VARCHAR(5),
    status VARCHAR(20),
    auth_code VARCHAR(20),
    error_code VARCHAR(10),
    seq_number VARCHAR(20),
    iso_mti VARCHAR(10),
    iso_processing_code VARCHAR(10),
    iso_stan VARCHAR(20),
    source VARCHAR(20) DEFAULT 'SIMULATOR',
    vendor VARCHAR(20),
    fault_type VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_atm_txn_atm_id ON atm_transactions(atm_id);
CREATE INDEX IF NOT EXISTS idx_atm_txn_recorded_at ON atm_transactions(recorded_at);

-- Written/read by state-manager every poll cycle. Columns match its
-- INSERT/UPDATE/SELECT usage (no service creates it otherwise).
CREATE TABLE IF NOT EXISTS atm_current_state (
    atm_id VARCHAR(20) PRIMARY KEY,
    state VARCHAR(30),
    previous_state VARCHAR(30),
    state_changed_at TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
