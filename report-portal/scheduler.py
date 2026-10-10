import os
import io
import csv
import base64
import logging
import smtplib
import ssl
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, date, timezone, timedelta

# Bank wall-clock: cron expressions mean EAST AFRICA TIME (no DST there —
# fixed +3 forever). Evaluating in UTC silently shifted every schedule by
# 3 hours (0 7 * * * fired at 10:00 EAT). All cron matching and next-run
# math below uses EAT; second-level dedupe stays in UTC instants.
EAT = timezone(timedelta(hours=3))
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from db import get_db
from audit import log_action

logger = logging.getLogger(__name__)

# ── Data retention (BRD row 17) ─────────────────────────────────────────────
# Runs daily ~03:00 EAT inside the schedule poll. Periods are PLACEHOLDERS —
# confirm with Risk/Compliance (NBE requirements) and adjust. OpenSearch EJ
# indices are governed separately by config/opensearch/ism_ej_retention.json.
# cash_replenishments is NEVER auto-deleted (financial evidence).
RETENTION_TXN_DAYS = int(os.environ.get('RETENTION_TXN_DAYS', '365'))
RETENTION_AUDIT_DAYS = int(os.environ.get('RETENTION_AUDIT_DAYS', '365'))
RETENTION_STATE_DAYS = int(os.environ.get('RETENTION_STATE_DAYS', '180'))
_last_retention_date = None

SCHEDULER_TABLE_SQL = """
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
    next_run    TIMESTAMP,
    last_status VARCHAR(20) DEFAULT NULL,
    last_error  TEXT DEFAULT NULL
);
"""

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
SMTP_FROM = os.environ.get('SMTP_FROM', 'atm-monitor@dashenbank.com')
# C9: bound every network wait so one hung relay can't stall the poller.
SMTP_TIMEOUT = int(os.environ.get('SMTP_TIMEOUT', '15'))
# Implicit TLS (port 465) for relays that mandate it; default stays STARTTLS.
SMTP_USE_SSL = os.environ.get('SMTP_USE_SSL', 'false').lower() in ('1', 'true', 'yes', 'on')
# Bank relays often present private-CA/self-signed certs that public trust
# stores reject (CERTIFICATE_VERIFY_FAILED). Preferred: mount the bank CA
# bundle and point SMTP_CA_FILE at it (verification stays ON). Escape hatch:
# SMTP_INSECURE=true skips verification — acceptable on the bank LAN to keep
# morning reports flowing, but log a warning every time and replace with the
# CA file as soon as the team supplies it. Default is secure.
SMTP_CA_FILE = os.environ.get('SMTP_CA_FILE', '')
SMTP_INSECURE = os.environ.get('SMTP_INSECURE', 'false').lower() in ('1', 'true', 'yes', 'on')
BASE_URL  = os.environ.get('BASE_URL', 'http://localhost:8888')
# Internal generation URL: the scheduler runs INSIDE the portal container,
# so it always reaches the app via loopback — never via the public BASE_URL
# (which may be a reviewer tunnel or LAN hostname unreachable from in here).
_INTERNAL_BASE = f"http://localhost:{os.environ.get('REPORT_PORTAL_PORT', '8888')}"
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')

# Embedded logo (CID attachment — renders in every client, unlike remote URLs).
_LOGO_BYTES = None
_logo_path = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')
try:
    with open(_logo_path, 'rb') as _f:
        _LOGO_BYTES = _f.read()
except Exception:
    pass


def init_scheduler_table():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(SCHEDULER_TABLE_SQL)
            # Backfill status columns on DBs created before C8.
            cur.execute("ALTER TABLE scheduled_reports ADD COLUMN IF NOT EXISTS last_status VARCHAR(20) DEFAULT NULL")
            cur.execute("ALTER TABLE scheduled_reports ADD COLUMN IF NOT EXISTS last_error TEXT DEFAULT NULL")
            conn.commit()
            cur.close()
        logger.info('scheduled_reports table ready')
    except Exception as e:
        logger.error('Failed to init scheduler table: %s', e)


def _generate_report(report_type, fmt, days=7, atm='all'):
    buf = io.BytesIO()
    _headers = {'X-Health-Check': '1'}
    if INTERNAL_API_KEY:
        _headers['X-Internal-Key'] = INTERNAL_API_KEY
    today = date.today().strftime('%Y-%m-%d')
    label = REPORT_TYPE_LABELS.get(report_type, report_type.title())
    atm_part = f'_{atm}' if atm and atm != 'all' else ''
    filename = f'Dashen_Bank_{label.replace(" ", "_")}{atm_part}_{today}.{fmt}'
    # Expected bytes per format: never attach a login page (or any HTML)
    # as a report — a 302→login slipping through used to get emailed out
    # as a fake PDF. Fail loudly instead.
    want_ct = {'csv': 'text/csv', 'pdf': 'application/pdf',
               'excel': 'officedocument.spreadsheetml'}.get(fmt, '')
    timeout = 60 if fmt in ('pdf', 'excel') else 30
    import requests
    url = f'{_INTERNAL_BASE}/report/{report_type}/{fmt}?days={days}&atm={atm}'
    try:
        resp = requests.get(url, timeout=timeout, headers=_headers)
        if resp.status_code != 200:
            logger.error('Report generation failed: HTTP %s for %s', resp.status_code, url)
            return None, None, None
        got_ct = resp.headers.get('Content-Type', '')
        if want_ct and want_ct not in got_ct:
            logger.error('Report generation refused: expected %s, got %s for %s',
                         want_ct, got_ct, url)
            return None, None, None
        buf.write(resp.content)
        buf.seek(0)
        send_ct = {'csv': 'text/csv', 'pdf': 'application/pdf',
                   'excel': 'application/vnd.openxmlformats-'
                            'officedocument.spreadsheetml.sheet'}.get(fmt, 'application/octet-stream')
        return buf, filename, send_ct
    except Exception as e:
        logger.error('Report generation failed: %s', e)
        return None, None, None
    return None, None, None


def _smtp_context():
    """TLS context for the relay: verified by default, bank CA if supplied,
    unverified only under explicit SMTP_INSECURE (warns loudly)."""
    if SMTP_INSECURE:
        logger.warning('SMTP_INSECURE is set: relay certificate is NOT verified '
                       '(bank LAN only — install the CA bundle and unset it)')
        return ssl._create_unverified_context()
    if SMTP_CA_FILE:
        return ssl.create_default_context(cafile=SMTP_CA_FILE)
    return ssl.create_default_context()


def _send_email(recipients, subject, body, filename, filedata, mimetype):
    if not SMTP_HOST:
        logger.warning('SMTP not configured, skipping email')
        return False
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_FROM
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        if _LOGO_BYTES:
            logo = MIMEImage(_LOGO_BYTES)
            logo.add_header('Content-ID', '<dashen-logo>')
            logo.add_header('Content-Disposition', 'inline', filename='dashen-logo.png')
            msg.attach(logo)

        # Real MIME type per format — generic application/octet-stream gets
        # flagged by Gmail (552 security block) and confuses other relays.
        try:
            _main, _sub = (mimetype or 'application/octet-stream').split('/', 1)
        except ValueError:
            _main, _sub = 'application', 'octet-stream'
        part = MIMEBase(_main, _sub)
        part.set_payload(filedata.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=filename)
        msg.attach(part)

        if SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT,
                                      context=_smtp_context())
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
        with server:
            if not SMTP_USE_SSL:
                server.starttls(context=_smtp_context())
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info('Email sent to %s: %s', recipients, subject)
        return True
    except Exception as e:
        logger.error('Failed to send email: %s', e)
        return False


REPORT_TYPE_LABELS = {
    'transaction': 'Transaction Summary',
    'cash': 'Cash Level & Dispensing',
    'error': 'Error & Incident',
    'performance': 'ATM Performance',
    'vendor': 'Vendor Performance',
    'availability': 'ATM Availability',
    'outage': 'Daily ATM Outage',
    'full': 'Complete Management Report',
}


def _email_body(report_type, fmt, days, atm):
    label = REPORT_TYPE_LABELS.get(report_type, report_type.title())
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    atm_display = atm if atm and atm != 'all' else 'All ATMs'
    logo_url = 'cid:dashen-logo'
    return f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#F0F2F6;font-family:'Segoe UI',Helvetica,Arial,sans-serif">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F0F2F6;min-width:100%">
    <tr><td align="center" style="padding:32px 16px">

      <!-- ── MAIN CARD ── -->
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(1,33,105,.08)">

        <!-- ── HEADER (compact: internal mail, not marketing) ── -->
        <tr>
          <td style="background:#012169;padding:20px 40px 16px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center">
                  <img src="{logo_url}" alt="Dashen Bank" style="border:0;display:block;margin:0 auto 8px;width:76px;max-width:100%;height:auto">
                  <div style="font-size:16px;font-weight:700;color:#fff;letter-spacing:-.2px">Dashen Bank S.C. <span style="font-weight:400;color:rgba(255,255,255,.65);font-size:11px;letter-spacing:1px">&nbsp;·&nbsp; ATM MONITORING SYSTEM</span></div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- ── GOLD BAR ── -->
        <tr><td style="height:4px;background:#FDD79A;padding:0;font-size:0;line-height:0">&zwnj;</td></tr>

        <!-- ── CONTENT ── -->
        <tr>
          <td style="padding:36px 40px 24px">

            <div style="font-size:22px;font-weight:800;color:#012169;margin-bottom:4px">Scheduled Report</div>
            <div style="font-size:13px;color:#64748B;margin-bottom:24px">{label}</div>

            <!-- ── INFO PANEL ── -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FAFBFC;border:1px solid #EEF1F6;border-radius:10px;margin-bottom:24px">
              <tr>
                <td style="padding:20px 24px">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="width:50%;padding:6px 0">
                        <div style="font-size:10px;font-weight:600;color:#94A3B8;text-transform:uppercase;letter-spacing:.6px">Report Type</div>
                        <div style="font-size:14px;font-weight:600;color:#0F172A;margin-top:2px">{label}</div>
                      </td>
                      <td style="width:50%;padding:6px 0">
                        <div style="font-size:10px;font-weight:600;color:#94A3B8;text-transform:uppercase;letter-spacing:.6px">Period</div>
                        <div style="font-size:14px;font-weight:600;color:#0F172A;margin-top:2px">Last {days} day{'s' if days != 1 else ''}</div>
                      </td>
                    </tr>
                    <tr>
                      <td style="width:50%;padding:6px 0">
                        <div style="font-size:10px;font-weight:600;color:#94A3B8;text-transform:uppercase;letter-spacing:.6px">ATM(s)</div>
                        <div style="font-size:14px;font-weight:600;color:#0F172A;margin-top:2px">{atm_display}</div>
                      </td>
                      <td style="width:50%;padding:6px 0">
                        <div style="font-size:10px;font-weight:600;color:#94A3B8;text-transform:uppercase;letter-spacing:.6px">Generated</div>
                        <div style="font-size:14px;font-weight:600;color:#0F172A;margin-top:2px">{now} EAT</div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <!-- ── ATTACHMENT NOTE ── -->
            <div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:16px 20px;margin-bottom:24px">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td width="28" valign="top" style="padding-right:12px">
                    <div style="width:28px;height:28px;background:#2563EB;border-radius:6px;text-align:center;line-height:28px;color:#fff;font-size:11px;font-weight:700;font-family:Arial,sans-serif">{fmt.upper()}</div>
                  </td>
                  <td>
                    <div style="font-size:13px;font-weight:600;color:#1E40AF">Report Attached</div>
                    <div style="font-size:12px;color:#475569;margin-top:2px">The <b>{label}</b> is attached to this email as a <b>{fmt.upper()}</b> file. Please download and review at your convenience.</div>
                  </td>
                </tr>
              </table>
            </div>

            <!-- ── CTA ── -->
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:#012169;border-radius:8px;padding:0">
                  <a href="{BASE_URL}" style="display:inline-block;padding:12px 28px;font-size:13px;font-weight:600;color:#fff;text-decoration:none;border-radius:8px">&#8594; Open ATM Monitoring System</a>
                </td>
              </tr>
            </table>

          </td>
        </tr>

        <!-- ── FOOTER ── -->
        <tr>
          <td style="background:#FAFBFC;border-top:1px solid #EEF1F6;padding:20px 40px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="font-size:11px;color:#94A3B8;line-height:1.6">
                  <div style="font-weight:600;color:#64748B;margin-bottom:2px">Dashen Bank S.C.</div>
                  <div>ATM Monitoring System &mdash; Confidential Management Report</div>
                  <div style="margin-top:8px;padding-top:8px;border-top:1px solid #EEF1F6">
                    This email was automatically generated by the Dashen Bank ATM Monitoring System.<br>
                    Please do not reply to this message. For support, contact IT Operations.
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>

      <!-- ── FOOTER SPACER ── -->
      <div style="text-align:center;font-size:10px;color:#CBD5E1;margin-top:16px">
        &copy; {datetime.now().strftime('%Y')} Dashen Bank S.C. All rights reserved.
      </div>

    </td></tr>
  </table>
</body>
</html>'''


def _record_run_status(report_id, ok, error, schedule):
    """Persist a run outcome. last_run/next_run advance ONLY on success —
    a failure keeps last_run stale (so the next due poll retries) and
    records last_status/last_error for the portal banner. Never raises."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if ok:
                cur.execute(
                    "UPDATE scheduled_reports SET last_run = NOW(), next_run = %s,"
                    " last_status = 'ok', last_error = NULL WHERE id = %s",
                    (_calc_next_run(schedule), report_id)
                )
            else:
                cur.execute(
                    "UPDATE scheduled_reports SET last_status = 'failed',"
                    " last_error = %s WHERE id = %s",
                    ((error or 'unknown error')[:500], report_id)
                )
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error('Failed to record schedule %s outcome: %s', report_id, e)


def _run_scheduled_report(report_id, report_type, fmt, schedule, recipients, params):
    today = date.today()
    days = 7
    atm = 'all'
    if params:
        try:
            for p in params.split(','):
                if '=' in p:
                    k, v = p.split('=', 1)
                    if k.strip() == 'days':
                        days = int(v.strip())
                    if k.strip() == 'atm':
                        atm = v.strip() or 'all'
        except (ValueError, AttributeError) as e:
            msg = f'malformed params {params!r}: {e}'
            logger.error('Scheduled report %s bad params: %s', report_id, msg)
            log_action('SCHEDULED_REPORT_FAILED', f'Report {report_id} ({report_type}/{fmt}) {msg}')
            _record_run_status(report_id, False, msg, schedule)
            return

    label = REPORT_TYPE_LABELS.get(report_type, report_type.upper())
    subject = f'[ATM Monitor] {label} - {today}'
    body = _email_body(report_type, fmt, days, atm)

    filedata, filename, mimetype = _generate_report(report_type, fmt, days, atm)
    if filedata is None:
        msg = 'report generation failed (portal endpoint unreachable or error)'
        logger.error('Scheduled report %s failed to generate', report_id)
        log_action('SCHEDULED_REPORT_FAILED', f'Report {report_id} ({report_type}/{fmt}) generation failed')
        _record_run_status(report_id, False, msg, schedule)
        return

    recipient_list = [r.strip() for r in recipients.split(',') if r.strip()]
    ok = _send_email(recipient_list, subject, body, filename, filedata, mimetype)

    if ok:
        log_action('SCHEDULED_REPORT', f'Report {report_id} ({report_type}/{fmt}) sent to {len(recipient_list)} recipients')
    else:
        log_action('SCHEDULED_REPORT_FAILED', f'Report {report_id} ({report_type}/{fmt}) email failed to {len(recipient_list)} recipients')
    _record_run_status(report_id, ok,
                       None if ok else 'email delivery failed (SMTP unreachable, refused, or unconfigured)',
                       schedule)


# ── POLLING SCHEDULER ──

# Report ids currently executing in a worker thread. The 30s poller only
# dispatches; slow PDF fetches / SMTP waits must never block it (C9), and a
# still-running report is not dispatched twice (dedupes the retry window
# that C8 intentionally leaves open by keeping last_run stale on failure).
_running = set()
_running_lock = threading.Lock()


def _dispatch(report_id, report_type, fmt, cron_expr, recipients, params):
    try:
        _run_scheduled_report(report_id, report_type, fmt, cron_expr, recipients, params)
    except Exception as e:
        logger.error('Scheduled report %s crashed: %s', report_id, e)
        _record_run_status(report_id, False, f'worker crashed: {e}', cron_expr)
    finally:
        with _running_lock:
            _running.discard(report_id)

_cron_trigger_cache = {}

def _cron_matches(cron_expr, dt):
    try:
        parts = cron_expr.split()
        if len(parts) != 5:
            return False
        minute, hour, day, month, weekday = parts
        return (_cron_field(minute, dt.minute) and
                _cron_field(hour, dt.hour) and
                _cron_field(day, dt.day) and
                _cron_field(month, dt.month) and
                _cron_field(weekday, (dt.weekday() + 1) % 7))
    except Exception:
        return False

def _cron_field(pattern, value):
    if pattern == '*':
        return True
    for part in pattern.split(','):
        if '/' in part:
            base, step = part.split('/')
            base = 0 if base == '*' else int(base)
            if value >= base and (value - base) % int(step) == 0:
                return True
        elif '-' in part:
            lo, hi = part.split('-')
            if int(lo) <= value <= int(hi):
                return True
        else:
            if int(part) == value:
                return True
    return False


def _calc_next_run(schedule):
    try:
        parts = schedule.split()
        if len(parts) == 5:
            trigger = CronTrigger.from_crontab(schedule)
            return trigger.get_next_fire_time(None, datetime.now(EAT))
    except Exception:
        pass
    return None


def _run_retention():
    """Delete rows older than the retention periods. Idempotent; runs daily."""
    global _last_retention_date
    today = datetime.now(timezone.utc).date()
    if _last_retention_date == today:
        return
    _last_retention_date = today
    try:
        with get_db() as conn:
            cur = conn.cursor()
            total = 0
            for table, col, days in (('atm_transactions', 'recorded_at', RETENTION_TXN_DAYS),
                                    ('audit_log', 'performed_at', RETENTION_AUDIT_DAYS)):
                cur.execute(
                    f"DELETE FROM {table} WHERE {col} < NOW() - INTERVAL '1 day' * %s",
                    (days,))
                total += cur.rowcount or 0
            # State history: only CLOSED segments fully outside the window.
            # Open segments are never deleted — they represent current state.
            cur.execute(
                "DELETE FROM atm_state_history WHERE ended_at IS NOT NULL"
                " AND ended_at < NOW() - INTERVAL '1 day' * %s",
                (RETENTION_STATE_DAYS,))
            total += cur.rowcount or 0
            conn.commit()
            cur.close()
        logger.info('Retention sweep: %d old row(s) removed', total)
    except Exception as e:
        logger.error('Retention sweep failed: %s', e)


def _check_schedules():
    try:
        now_utc = datetime.now(timezone.utc)
        now = now_utc.astimezone(EAT)
        if now_utc.hour == 0:
            _run_retention()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, report_type, format, schedule, recipients, params, last_run "
                "FROM scheduled_reports WHERE enabled = TRUE"
            )
            rows = cur.fetchall()
            cur.close()
        for row in rows:
            rid, report_type, fmt, cron_expr, recipients, params, last_run = row
            try:
                if not _cron_matches(cron_expr, now):
                    continue
                if last_run and (now_utc - last_run.replace(tzinfo=timezone.utc)).total_seconds() < 60:
                    continue
                with _running_lock:
                    if rid in _running:
                        continue
                    _running.add(rid)
                t = threading.Thread(target=_dispatch,
                                     args=(rid, report_type, fmt, cron_expr, recipients, params),
                                     daemon=True)
                t.start()
            except Exception as e:
                logger.error('Error checking schedule %s: %s', rid, e)
    except Exception as e:
        logger.error('Schedule poll failed: %s', e)


def load_schedules(scheduler):
    """Legacy — polling handles everything, no need to preload."""
    pass


# Held open for process lifetime by whichever worker won the scheduler lock.
_sched_lock_fh = None


def create_scheduler(app):
    # Singleton across gunicorn workers: only the worker holding the lock
    # runs the poller, so scheduled emails never duplicate. Single container
    # = single host, so a local file lock is sufficient (no Redis needed).
    import fcntl
    global _sched_lock_fh
    try:
        _sched_lock_fh = open('/tmp/portal-scheduler.lock', 'w')
        fcntl.flock(_sched_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        logger.info('Scheduler already running in a sibling worker — skipping')
        return None
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_check_schedules, trigger='interval', seconds=30, id='schedule_poller')
    sched.start()
    logger.info('Report scheduler started (polling every 30s)')
    return sched
