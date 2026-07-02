-- ============================================
-- Dashen Bank ATM Monitoring System
-- Custom Database Tables & Seed Data
-- ============================================
-- Run against the zabbix database (the shared
-- PostgreSQL instance used by Zabbix server).
--
-- Usage:
--   docker exec -i zabbix-db psql -U zabbix -d zabbix < this_file
-- ============================================

-- ─── ATM LOCATIONS (geography & metadata) ────────────────────

CREATE TABLE IF NOT EXISTS atm_locations (
    atm_id       VARCHAR(20) PRIMARY KEY,
    branch       VARCHAR(100),
    district     VARCHAR(100),
    city         VARCHAR(100),
    region       VARCHAR(100),
    latitude     DECIMAL(10,7),
    longitude    DECIMAL(10,7),
    terminal_id  VARCHAR(20),
    vendor       VARCHAR(50),
    model        VARCHAR(50),
    install_date DATE,
    status       VARCHAR(20) DEFAULT 'active'
);

INSERT INTO atm_locations VALUES
('ATM-001','Addis Ababa Main Branch',
 'Kirkos','Addis Ababa','Addis Ababa',
 9.0300,38.7578,'TID001','NCR','SelfServ 34','2023-01-15','active'),
('ATM-002','Bole International Branch',
 'Bole','Addis Ababa','Addis Ababa',
 8.9806,38.7894,'TID002','NCR','SelfServ 34','2023-03-20','active'),
('ATM-003','Merkato Branch',
 'Addis Ketema Sub-City','Addis Ababa','Addis Ababa',
 9.0350,38.7469,'TID003','NCR','SelfServ 34','2023-02-10','active'),
('ATM-004','Hawassa Branch',
 'Hawassa Central','Hawassa','Sidama Region',
 7.0621,38.4760,'TID004','NCR','SelfServ 34','2023-04-05','active'),
('ATM-005','Dire Dawa Branch',
 'Dire Dawa Central','Dire Dawa','Dire Dawa',
 9.5931,41.8661,'TID005','NCR','SelfServ 34','2023-05-12','active')
ON CONFLICT (atm_id) DO NOTHING;

-- ─── ATM TRANSACTIONS ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS atm_transactions (
    id                  SERIAL PRIMARY KEY,
    recorded_at         TIMESTAMP DEFAULT NOW(),
    atm_id              VARCHAR(20),
    terminal_id         VARCHAR(20),
    branch              VARCHAR(100),
    txn_type            VARCHAR(30),
    card_masked         VARCHAR(20),
    amount              DECIMAL(15,2),
    currency            VARCHAR(5),
    status              VARCHAR(20),
    auth_code           VARCHAR(20),
    error_code          VARCHAR(10),
    seq_number          VARCHAR(20),
    iso_mti             VARCHAR(10),
    iso_processing_code VARCHAR(10),
    iso_stan            VARCHAR(20),
    source              VARCHAR(20) DEFAULT 'SIMULATOR'
);

CREATE INDEX IF NOT EXISTS idx_atm_txn_atm_id      ON atm_transactions(atm_id);
CREATE INDEX IF NOT EXISTS idx_atm_txn_recorded_at ON atm_transactions(recorded_at);
CREATE INDEX IF NOT EXISTS idx_atm_txn_card        ON atm_transactions(card_masked);

-- ─── ATM ANOMALIES (from anomaly-detector) ───────────────────

CREATE TABLE IF NOT EXISTS atm_anomalies (
    id              SERIAL PRIMARY KEY,
    detected_at     TIMESTAMP DEFAULT NOW(),
    atm_id          VARCHAR(20),
    branch          VARCHAR(100),
    anomaly_type    VARCHAR(50),
    severity        VARCHAR(10),
    card_masked     VARCHAR(20),
    detail          TEXT,
    txn_count       INT,
    amount          DECIMAL(15,2),
    acknowledged    BOOLEAN DEFAULT FALSE,
    acknowledged_at TIMESTAMP,
    acknowledged_by VARCHAR(100),
    zabbix_event_id VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_anomaly_detected_at ON atm_anomalies(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_atm_id       ON atm_anomalies(atm_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_type         ON atm_anomalies(anomaly_type);

-- ─── NETWORK EVENTS & CORRELATION (from network-correlator) ──

CREATE TABLE IF NOT EXISTS atm_network_events (
    id              SERIAL PRIMARY KEY,
    recorded_at     TIMESTAMP DEFAULT NOW(),
    atm_id          VARCHAR(20),
    branch          VARCHAR(100),
    event_type      VARCHAR(30),
    metric_value    FLOAT,
    threshold       FLOAT,
    duration_sec    INT,
    zabbix_event_id VARCHAR(50),
    correlated      BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_net_event_atm ON atm_network_events(atm_id);

CREATE TABLE IF NOT EXISTS atm_network_correlation (
    id                  SERIAL PRIMARY KEY,
    correlated_at       TIMESTAMP DEFAULT NOW(),
    atm_id              VARCHAR(20),
    branch              VARCHAR(100),
    network_event_id    INT REFERENCES atm_network_events(id),
    event_type          VARCHAR(30),
    metric_value        FLOAT,
    window_start        TIMESTAMP,
    window_end          TIMESTAMP,
    txns_in_window      INT,
    txns_failed         INT,
    txns_approved       INT,
    failure_rate        FLOAT,
    baseline_fail_rate  FLOAT,
    uplift              FLOAT,
    cards_affected      INT,
    detail              TEXT
);

CREATE INDEX IF NOT EXISTS idx_net_corr_atm ON atm_network_correlation(atm_id);
CREATE INDEX IF NOT EXISTS idx_net_corr_at  ON atm_network_correlation(correlated_at DESC);

CREATE TABLE IF NOT EXISTS atm_network_metrics (
    id          SERIAL PRIMARY KEY,
    recorded_at TIMESTAMP DEFAULT NOW(),
    atm_id      VARCHAR(20),
    branch      VARCHAR(100),
    latency_ms  FLOAT,
    packet_loss FLOAT,
    jitter_ms   FLOAT,
    status      VARCHAR(20)
);

CREATE INDEX IF NOT EXISTS idx_net_metrics_atm
    ON atm_network_metrics(atm_id, recorded_at DESC);

-- ─── AUDIT LOG ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    performed_at TIMESTAMP DEFAULT NOW(),
    username     VARCHAR(100) NOT NULL,
    action       VARCHAR(50) NOT NULL,
    detail       TEXT,
    ip_address   VARCHAR(45)
);

CREATE INDEX IF NOT EXISTS idx_audit_performed ON audit_log(performed_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_username  ON audit_log(username);
CREATE INDEX IF NOT EXISTS idx_audit_action    ON audit_log(action);

-- ─── MISSING COMPOSITE INDEXES ────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_txn_atm_time
    ON atm_transactions(atm_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_status_type_time
    ON atm_transactions(status, txn_type, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_recorded_status
    ON atm_transactions(recorded_at DESC, status);
CREATE INDEX IF NOT EXISTS idx_txn_type_status
    ON atm_transactions(txn_type, status, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_loc_branch
    ON atm_locations(branch);

CREATE INDEX IF NOT EXISTS idx_anomaly_severity
    ON atm_anomalies(severity, detected_at DESC);

-- ─── APP USERS (RBAC) ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app_users (
    username      VARCHAR(100) PRIMARY KEY,
    password_hash VARCHAR(256) NOT NULL,
    role          VARCHAR(20) NOT NULL DEFAULT 'viewer',
    created_at    TIMESTAMP DEFAULT NOW(),
    last_login    TIMESTAMP
);

INSERT INTO app_users (username, password_hash, role) VALUES
    ('admin', '$2b$12$LJ3m4ys3Lk0TSwHnbfOMiOXPm1Qlq5Gz0n3t7s5YxVvKj2qWqGqO', 'admin')
ON CONFLICT (username) DO NOTHING;

-- ─── SCHEDULED REPORTS ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scheduled_reports (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    report_type VARCHAR(50) NOT NULL,
    format      VARCHAR(10) NOT NULL DEFAULT 'pdf',
    schedule    VARCHAR(50) NOT NULL,
    recipients  TEXT NOT NULL,
    params      TEXT,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT NOW(),
    last_run    TIMESTAMP,
    next_run    TIMESTAMP
);
