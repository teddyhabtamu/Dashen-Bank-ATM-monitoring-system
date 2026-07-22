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


def init_audit_schema():
    index_queries = [
        AUDIT_TABLE_SQL,
        USERS_TABLE_SQL,
    ] + USERS_MIGRATIONS + VENDOR_CONSTRAINTS + [
        "CREATE INDEX IF NOT EXISTS idx_audit_performed ON audit_log(performed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_username  ON audit_log(username)",
        "CREATE INDEX IF NOT EXISTS idx_audit_action    ON audit_log(action)",
        # Composite indexes for atm_transactions (performance)
        "CREATE INDEX IF NOT EXISTS idx_txn_atm_time        ON atm_transactions(atm_id, recorded_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_txn_status_type_time ON atm_transactions(status, txn_type, recorded_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_txn_recorded_status  ON atm_transactions(recorded_at DESC, status)",
        "CREATE INDEX IF NOT EXISTS idx_txn_type_status      ON atm_transactions(txn_type, status, recorded_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_txn_branch_time      ON atm_transactions(branch, recorded_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_txn_card_time        ON atm_transactions(card_masked, recorded_at DESC)",
        # Composite indexes for atm_locations
        "CREATE INDEX IF NOT EXISTS idx_loc_branch           ON atm_locations(branch)",
        "CREATE INDEX IF NOT EXISTS idx_loc_status           ON atm_locations(status)",
        "CREATE INDEX IF NOT EXISTS idx_loc_region_city      ON atm_locations(region, city)",
        "CREATE INDEX IF NOT EXISTS idx_loc_terminal         ON atm_locations(terminal_id)",
        # Anomalies
        "CREATE INDEX IF NOT EXISTS idx_anomaly_severity     ON atm_anomalies(severity, detected_at DESC)",
    ]
    try:
        with get_db() as conn:
            cur = conn.cursor()
            for q in index_queries:
                cur.execute(q)
            conn.commit()
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
