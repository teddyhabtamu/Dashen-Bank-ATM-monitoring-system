import logging
from db import get_db
from flask import request, session

logger = logging.getLogger(__name__)

AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    performed_at TIMESTAMP DEFAULT NOW(),
    username     VARCHAR(100) NOT NULL,
    action       VARCHAR(50) NOT NULL,
    detail       TEXT,
    ip_address   VARCHAR(45)
);
"""


USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_users (
    username      VARCHAR(100) PRIMARY KEY,
    password_hash VARCHAR(256) NOT NULL,
    role          VARCHAR(20) NOT NULL DEFAULT 'viewer',
    created_at    TIMESTAMP DEFAULT NOW(),
    last_login    TIMESTAMP
);
"""

USERS_MIGRATIONS = [
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'viewer'",
]

# Vendor constraints — enforced server-side; duplicates are ignored.
VENDOR_CONSTRAINTS = [
    """DO $$ BEGIN
        ALTER TABLE atm_locations
            ADD CONSTRAINT chk_atm_locations_vendor
            CHECK (vendor IS NOT NULL AND vendor IN ('NCR', 'GRG'));
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$""",
    """DO $$ BEGIN
        ALTER TABLE atm_transactions
            ADD CONSTRAINT chk_atm_transactions_vendor
            CHECK (vendor IS NULL OR vendor IN ('NCR', 'GRG'));
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$""",
    """DO $$ BEGIN
        ALTER TABLE fault_type_map
            ADD CONSTRAINT chk_fault_type_map_vendor
            CHECK (vendor IN ('NCR', 'GRG'));
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$""",
]


SCHEMA_MIGRATIONS = [
    "ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS ip_address VARCHAR(45)",
    "ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS sim_port INTEGER",
]

# fault_type_map: raw vendor fault codes -> standard fault types.
# NOT part of the DB backup; must be created on every machine at startup.
FAULT_TYPE_MAP_SQL = """
CREATE TABLE IF NOT EXISTS fault_type_map (
    id                  SERIAL PRIMARY KEY,
    vendor              VARCHAR(10)  NOT NULL,
    raw_fault_code      VARCHAR(100) NOT NULL,
    standard_fault_type VARCHAR(50)  NOT NULL,
    display_name        VARCHAR(50)  NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fault_type_map_vendor_code
    ON fault_type_map(vendor, raw_fault_code);
INSERT INTO fault_type_map (vendor, raw_fault_code, standard_fault_type, display_name) VALUES
    ('NCR', 'CASH_JAM',              'DISPENSER',       'Dispenser'),
    ('NCR', 'DISPENSER_ERROR',       'DISPENSER',       'Dispenser'),
    ('NCR', '9E3D',                  'DISPENSER',       'Dispenser'),
    ('NCR', 'CARD_READ_ERROR',       'CARD_READER',     'Card Reader'),
    ('NCR', 'B2C1',                  'CARD_READER',     'Card Reader'),
    ('NCR', 'RECEIPT_PAPER_LOW',     'RECEIPT_PRINTER', 'Receipt Printer'),
    ('NCR', 'FF01',                  'RECEIPT_PRINTER', 'Receipt Printer'),
    ('NCR', 'NETWORK_TIMEOUT',       'NETWORK',         'Network'),
    ('NCR', '44AA',                  'NETWORK',         'Network'),
    ('NCR', 'RETRACT_FULL',          'RETRACT_BIN',     'Retract Full'),
    ('NCR', '3A7F',                  'DISPENSER',       'Dispenser'),
    ('GRG', 'CASH_MODULE_ERROR',     'DISPENSER',       'Dispenser'),
    ('GRG', 'CARD_UNIT_FAULT',       'CARD_READER',     'Card Reader'),
    ('GRG', 'PURGE_BIN_FULL',        'RETRACT_BIN',     'Retract Full'),
    ('GRG', 'THERMAL_PRINTER_FAULT', 'RECEIPT_PRINTER', 'Receipt Printer'),
    ('GRG', 'COMM_ERROR',            'NETWORK',         'Network'),
    ('GRG', 'PIN_PAD_ERROR',         'PIN_PAD',         'PinPad'),
    ('GRG', 'CASH_JAM',              'DISPENSER',       'Dispenser')
ON CONFLICT (vendor, raw_fault_code)
    DO UPDATE SET standard_fault_type = EXCLUDED.standard_fault_type,
                  display_name        = EXCLUDED.display_name;
"""


def init_audit_schema():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(999888777)")
            got_lock = cur.fetchone()[0]
            if not got_lock:
                cur.close()
                logger.info('Schema init skipped (another container holds the lock)')
                return
            try:
                for q in [
                    AUDIT_TABLE_SQL, USERS_TABLE_SQL,
                ] + USERS_MIGRATIONS + SCHEMA_MIGRATIONS + [
                    FAULT_TYPE_MAP_SQL,
                ] + VENDOR_CONSTRAINTS + [
                    "CREATE INDEX IF NOT EXISTS idx_audit_performed ON audit_log(performed_at DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_audit_username  ON audit_log(username)",
                    "CREATE INDEX IF NOT EXISTS idx_audit_action    ON audit_log(action)",
                    "CREATE INDEX IF NOT EXISTS idx_txn_atm_time        ON atm_transactions(atm_id, recorded_at DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_txn_status_type_time ON atm_transactions(status, txn_type, recorded_at DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_txn_recorded_status  ON atm_transactions(recorded_at DESC, status)",
                    "CREATE INDEX IF NOT EXISTS idx_txn_type_status      ON atm_transactions(txn_type, status, recorded_at DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_txn_branch_time      ON atm_transactions(branch, recorded_at DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_txn_card_time        ON atm_transactions(card_masked, recorded_at DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_loc_branch           ON atm_locations(branch)",
                    "CREATE INDEX IF NOT EXISTS idx_loc_status           ON atm_locations(status)",
                    "CREATE INDEX IF NOT EXISTS idx_loc_region_city      ON atm_locations(region, city)",
                    "CREATE INDEX IF NOT EXISTS idx_loc_terminal         ON atm_locations(terminal_id)",
                    "CREATE INDEX IF NOT EXISTS idx_anomaly_severity     ON atm_anomalies(severity, detected_at DESC)",
                ]:
                    cur.execute(q)
                conn.commit()
            finally:
                cur.execute("SELECT pg_advisory_unlock(999888777)")
                cur.close()
        logger.info('Database schema verified (audit_log + indexes)')
    except Exception as e:
        logger.error('Failed to init schema: %s', e)


def log_action(action, detail=None):
    try:
        from flask import has_request_context
        if has_request_context():
            username = session.get('username', 'system')
            ip = request.remote_addr
        else:
            username = 'system'
            ip = None
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO audit_log (username, action, detail, ip_address) VALUES (%s, %s, %s, %s)",
                (username, action, detail, ip)
            )
            conn.commit()
            cur.close()
    except Exception as e:
        logger.warning('Audit log failed (non-fatal): %s', e)
