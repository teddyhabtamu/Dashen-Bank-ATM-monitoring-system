#!/usr/bin/env python3
"""
Dashen Bank ATM — Suspicious Transaction Pattern Detector
Batch 3 / BRD Req 5.5 — "Suspicious transaction pattern" alert trigger

Runs continuously. Detects:
  1. Velocity abuse     — same card, 3+ withdrawals within 10 minutes
  2. Failure spike      — ATM failure rate jumps above 40% in last 15 minutes
  3. Large withdrawal   — single withdrawal above threshold (ETB 8,000)
  4. Rapid sequential   — 5+ transactions from same card within 5 minutes (skimming indicator)
  5. Off-hours spike    — ATM transaction volume > 3x normal between 00:00-05:00

Writes findings to: atm_anomalies table
Exposes: /tmp/zabbix_anomaly_count  (Zabbix external check reads this)
Also writes: /tmp/zabbix_atm_anomaly_{ATM_ID} per-ATM for individual triggers
"""

import os, time, json, psycopg2, logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ANOMALY] %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

CHECK_INTERVAL   = int(os.environ.get('CHECK_INTERVAL',   '60'))   # seconds between scans
VELOCITY_WINDOW  = int(os.environ.get('VELOCITY_WINDOW',  '10'))   # minutes
VELOCITY_LIMIT   = int(os.environ.get('VELOCITY_LIMIT',   '3'))    # withdrawals
LARGE_TXN_ETB    = int(os.environ.get('LARGE_TXN_ETB',    '8000')) # ETB threshold
RAPID_WINDOW     = int(os.environ.get('RAPID_WINDOW',     '5'))    # minutes
RAPID_LIMIT      = int(os.environ.get('RAPID_LIMIT',      '5'))    # txns from same card
FAILURE_WINDOW   = int(os.environ.get('FAILURE_WINDOW',   '15'))   # minutes
FAILURE_THRESHOLD= float(os.environ.get('FAILURE_THRESHOLD','0.4'))# 40%
OFFHOURS_MULT    = float(os.environ.get('OFFHOURS_MULT',   '3.0')) # 3x normal volume

def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
        connect_timeout=10
    )

def init_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS atm_anomalies (
                id              SERIAL PRIMARY KEY,
                detected_at     TIMESTAMP DEFAULT NOW(),
                atm_id          VARCHAR(20),
                branch          VARCHAR(100),
                anomaly_type    VARCHAR(50),   -- VELOCITY / FAILURE_SPIKE / LARGE_TXN / RAPID_SEQ / OFFHOURS
                severity        VARCHAR(10),   -- HIGH / MEDIUM / LOW
                card_masked     VARCHAR(20),
                detail          TEXT,
                txn_count       INT,
                amount          DECIMAL(15,2),
                acknowledged    BOOLEAN DEFAULT FALSE,
                acknowledged_at TIMESTAMP,
                acknowledged_by VARCHAR(100),
                zabbix_event_id VARCHAR(50)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_anomaly_detected_at
            ON atm_anomalies(detected_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_anomaly_atm_id
            ON atm_anomalies(atm_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_anomaly_type
            ON atm_anomalies(anomaly_type)
        """)
    conn.commit()
    log.info("Schema ready — atm_anomalies table initialised")

def already_detected(cur, atm_id, anomaly_type, card_masked, window_minutes=30):
    """Prevent duplicate alerts for the same event within window_minutes."""
    cur.execute("""
        SELECT id FROM atm_anomalies
        WHERE atm_id = %s
        AND anomaly_type = %s
        AND (card_masked = %s OR card_masked IS NULL)
        AND detected_at >= NOW() - INTERVAL '%s minutes'
        LIMIT 1
    """ , (atm_id, anomaly_type, card_masked, window_minutes))
    return cur.fetchone() is not None

def insert_anomaly(conn, cur, atm_id, branch, anomaly_type, severity,
                   card_masked, detail, txn_count=None, amount=None):
    cur.execute("""
        INSERT INTO atm_anomalies
        (atm_id, branch, anomaly_type, severity, card_masked, detail, txn_count, amount)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (atm_id, branch, anomaly_type, severity, card_masked, detail, txn_count, amount))
    anomaly_id = cur.fetchone()[0]
    conn.commit()
    log.warning(f"ANOMALY #{anomaly_id} | {severity} | {atm_id} | {anomaly_type} | {detail}")
    return anomaly_id

# ── DETECTION RULES ───────────────────────────────────────────────────────────

def check_velocity(conn, cur):
    """Rule 1: Same card, 3+ withdrawals within VELOCITY_WINDOW minutes."""
    cur.execute("""
        SELECT
            atm_id, branch, card_masked,
            COUNT(*) AS cnt,
            SUM(COALESCE(amount, 0)) AS total_amount
        FROM atm_transactions
        WHERE txn_type = 'WITHDRAWAL'
        AND status = 'APPROVED'
        AND recorded_at >= NOW() - INTERVAL %s
        AND card_masked IS NOT NULL
        GROUP BY atm_id, branch, card_masked
        HAVING COUNT(*) >= %s
    """, (f"{VELOCITY_WINDOW} minutes", VELOCITY_LIMIT))
    hits = cur.fetchall()
    for atm_id, branch, card, cnt, total in hits:
        if already_detected(cur, atm_id, 'VELOCITY', card, window_minutes=VELOCITY_WINDOW):
            continue
        detail = (f"Card {card} made {cnt} withdrawals in {VELOCITY_WINDOW} min "
                  f"totalling ETB {total:,.0f} at {atm_id} ({branch})")
        insert_anomaly(conn, cur, atm_id, branch, 'VELOCITY', 'HIGH',
                       card, detail, txn_count=cnt, amount=total)

def check_failure_spike(conn, cur):
    """Rule 2: ATM failure rate > FAILURE_THRESHOLD in last FAILURE_WINDOW minutes."""
    cur.execute("""
        SELECT
            atm_id, branch,
            COUNT(*) AS total,
            SUM(CASE WHEN status IN ('DECLINED','ERROR','TIMEOUT') THEN 1 ELSE 0 END) AS failed,
            ROUND(100.0 * SUM(CASE WHEN status IN ('DECLINED','ERROR','TIMEOUT')
                THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS fail_rate
        FROM atm_transactions
        WHERE recorded_at >= NOW() - INTERVAL %s
        GROUP BY atm_id, branch
        HAVING COUNT(*) >= 5
        AND (SUM(CASE WHEN status IN ('DECLINED','ERROR','TIMEOUT') THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0)::float) >= %s
    """, (f"{FAILURE_WINDOW} minutes", FAILURE_THRESHOLD))
    hits = cur.fetchall()
    for atm_id, branch, total, failed, fail_rate in hits:
        if already_detected(cur, atm_id, 'FAILURE_SPIKE', None, window_minutes=FAILURE_WINDOW):
            continue
        detail = (f"{atm_id} ({branch}) has {fail_rate}% failure rate "
                  f"({failed}/{total} txns) in last {FAILURE_WINDOW} min")
        insert_anomaly(conn, cur, atm_id, branch, 'FAILURE_SPIKE', 'HIGH',
                       None, detail, txn_count=int(failed))

def check_large_withdrawal(conn, cur):
    """Rule 3: Single withdrawal above LARGE_TXN_ETB threshold."""
    cur.execute("""
        SELECT
            atm_id, branch, card_masked, amount, recorded_at
        FROM atm_transactions
        WHERE txn_type = 'WITHDRAWAL'
        AND status = 'APPROVED'
        AND amount >= %s
        AND recorded_at >= NOW() - INTERVAL '2 minutes'
    """, (LARGE_TXN_ETB,))
    hits = cur.fetchall()
    for atm_id, branch, card, amount, ts in hits:
        if already_detected(cur, atm_id, 'LARGE_TXN', card, window_minutes=10):
            continue
        detail = (f"Large withdrawal of ETB {amount:,.0f} by card {card} "
                  f"at {atm_id} ({branch}) at {ts.strftime('%H:%M:%S')}")
        insert_anomaly(conn, cur, atm_id, branch, 'LARGE_TXN', 'MEDIUM',
                       card, detail, amount=amount)

def check_rapid_sequential(conn, cur):
    """Rule 4: Same card 5+ transactions in 5 min (possible skimming/cloning)."""
    cur.execute("""
        SELECT
            atm_id, branch, card_masked, COUNT(*) AS cnt
        FROM atm_transactions
        WHERE recorded_at >= NOW() - INTERVAL %s
        AND card_masked IS NOT NULL
        GROUP BY atm_id, branch, card_masked
        HAVING COUNT(*) >= %s
    """, (f"{RAPID_WINDOW} minutes", RAPID_LIMIT))
    hits = cur.fetchall()
    for atm_id, branch, card, cnt in hits:
        if already_detected(cur, atm_id, 'RAPID_SEQ', card, window_minutes=RAPID_WINDOW):
            continue
        detail = (f"Card {card} triggered {cnt} transactions in {RAPID_WINDOW} min "
                  f"at {atm_id} ({branch}) — possible card clone/skimming")
        insert_anomaly(conn, cur, atm_id, branch, 'RAPID_SEQ', 'HIGH',
                       card, detail, txn_count=cnt)

def check_offhours_spike(conn, cur):
    """Rule 5: Off-hours (00:00-05:00) volume > 3x average for that hour."""
    now = datetime.now()
    if not (0 <= now.hour <= 5):
        return  # Only run during off-hours

    cur.execute("""
        WITH current_hour AS (
            SELECT atm_id, branch, COUNT(*) AS current_cnt
            FROM atm_transactions
            WHERE recorded_at >= DATE_TRUNC('hour', NOW())
            GROUP BY atm_id, branch
        ),
        historical_avg AS (
            SELECT atm_id, branch,
                AVG(hourly_cnt) AS avg_cnt
            FROM (
                SELECT atm_id, branch,
                    DATE_TRUNC('hour', recorded_at) AS hr,
                    COUNT(*) AS hourly_cnt
                FROM atm_transactions
                WHERE EXTRACT(HOUR FROM recorded_at) = EXTRACT(HOUR FROM NOW())
                AND recorded_at BETWEEN NOW() - INTERVAL '30 days' AND NOW() - INTERVAL '1 day'
                GROUP BY atm_id, branch, DATE_TRUNC('hour', recorded_at)
            ) sub
            GROUP BY atm_id, branch
        )
        SELECT c.atm_id, c.branch, c.current_cnt, COALESCE(h.avg_cnt, 1) AS avg_cnt
        FROM current_hour c
        LEFT JOIN historical_avg h ON c.atm_id = h.atm_id
        WHERE c.current_cnt > COALESCE(h.avg_cnt, 1) * %s
        AND c.current_cnt >= 3
    """, (OFFHOURS_MULT,))
    hits = cur.fetchall()
    for atm_id, branch, current, avg in hits:
        if already_detected(cur, atm_id, 'OFFHOURS_SPIKE', None, window_minutes=60):
            continue
        detail = (f"Off-hours spike at {atm_id} ({branch}): {current} txns this hour "
                  f"vs avg {avg:.1f} ({OFFHOURS_MULT}x threshold)")
        insert_anomaly(conn, cur, atm_id, branch, 'OFFHOURS_SPIKE', 'MEDIUM',
                       None, detail, txn_count=int(current))

# ── ZABBIX INTERFACE ──────────────────────────────────────────────────────────

def write_zabbix_files(cur):
    """
    Write plain-text files that Zabbix external checks or UserParameter read.
    Zabbix item key: system.run[cat /tmp/zabbix_anomaly_count]
    Returns: integer count of unacknowledged anomalies in last 60 minutes.
    """
    cur.execute("""
        SELECT COUNT(*) FROM atm_anomalies
        WHERE detected_at >= NOW() - INTERVAL '60 minutes'
        AND acknowledged = FALSE
    """)
    total = cur.fetchone()[0]
    with open('/tmp/zabbix_anomaly_count', 'w') as f:
        f.write(str(total))

    # Per-ATM counts for individual host triggers
    cur.execute("""
        SELECT atm_id, COUNT(*) AS cnt
        FROM atm_anomalies
        WHERE detected_at >= NOW() - INTERVAL '60 minutes'
        AND acknowledged = FALSE
        GROUP BY atm_id
    """)
    per_atm = {row[0]: row[1] for row in cur.fetchall()}

    # Write per-ATM files for all active ATMs (dynamic, not hardcoded)
    cur.execute("SELECT atm_id FROM atm_locations WHERE status = 'active'")
    active_atms = [row[0] for row in cur.fetchall()]
    for atm_id in active_atms:
        cnt = per_atm.get(atm_id, 0)
        safe = atm_id.replace('-', '_').lower()
        with open(f'/tmp/zabbix_{safe}_anomalies', 'w') as f:
            f.write(str(cnt))

    # Write JSON summary for Grafana HTTP data source (optional)
    cur.execute("""
        SELECT
            id, detected_at, atm_id, branch, anomaly_type,
            severity, card_masked, detail, txn_count, amount, acknowledged
        FROM atm_anomalies
        ORDER BY detected_at DESC
        LIMIT 50
    """)
    cols = [d[0] for d in cur.description]
    rows = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        d['detected_at'] = d['detected_at'].isoformat() if d['detected_at'] else None
        d['amount'] = float(d['amount']) if d['amount'] else None
        rows.append(d)
    with open('/tmp/anomaly_feed.json', 'w') as f:
        json.dump({'anomalies': rows, 'total_unacked': int(total),
                   'generated_at': datetime.now().isoformat()}, f, default=str)

    return total

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    log.info("Starting Anomaly Detector — Dashen Bank ATM Monitoring")
    log.info(f"Config: velocity={VELOCITY_LIMIT} in {VELOCITY_WINDOW}m | "
             f"large_txn={LARGE_TXN_ETB} ETB | "
             f"failure_threshold={FAILURE_THRESHOLD*100:.0f}% | "
             f"rapid_seq={RAPID_LIMIT} in {RAPID_WINDOW}m")

    conn = None
    for attempt in range(30):
        try:
            conn = get_db()
            with conn.cursor() as cur:
                init_schema(conn)
            log.info("Connected to PostgreSQL")
            break
        except Exception as e:
            log.warning(f"DB not ready ({attempt+1}/30): {e}")
            time.sleep(5)

    if not conn:
        log.error("Could not connect to PostgreSQL. Exiting.")
        return

    log.info(f"Scanning every {CHECK_INTERVAL} seconds...")

    while True:
        try:
            with conn.cursor() as cur:
                check_velocity(conn, cur)
                check_failure_spike(conn, cur)
                check_large_withdrawal(conn, cur)
                check_rapid_sequential(conn, cur)
                check_offhours_spike(conn, cur)
                total = write_zabbix_files(cur)

            if total > 0:
                log.warning(f"Active unacknowledged anomalies: {total}")
            else:
                log.info("Scan complete — no active anomalies")

        except psycopg2.OperationalError as e:
            log.error(f"DB connection lost: {e} — reconnecting...")
            try:
                conn = get_db()
            except Exception:
                pass
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    run()