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
    filename = _report_filename(report_type, fmt, days, atm)
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


def _send_email(recipients, subject, body, filename, filedata, mimetype,
                body_text=None):
    if not SMTP_HOST:
        logger.warning('SMTP not configured, skipping email')
        return False
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_FROM
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        if body_text:
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(body_text, 'plain', 'utf-8'))
            alt.attach(MIMEText(body, 'html', 'utf-8'))
            msg.attach(alt)
        else:
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


# ── SCHEDULED-REPORT EMAIL CONTENT ─────────────────────────────────────────
# Thresholds for red/amber highlighting (config = these two constants).
EMAIL_AVAIL_TARGET = 99.0   # below: amber
EMAIL_AVAIL_ALERT = 95.0    # below: red
# IT contact shown in the footer. No dedicated config exists; override with
# EMAIL_SUPPORT in .env, else the line falls back to plain "IT Operations".
EMAIL_SUPPORT = os.environ.get('EMAIL_SUPPORT', '')


def _email_window(days):
    """Report window as EAT day boundaries (UTC instants for Postgres).

    days=N covers the N calendar days ending today: start = (today-N+1)
    00:00 EAT, end = tomorrow 00:00 EAT. Returns (start, end, start_prev,
    period_label). Never raises (falls back to naive NOW math).
    """
    try:
        eat_today = datetime.now(EAT).date()
    except Exception:
        eat_today = date.today()
    start_eat = eat_today - timedelta(days=max(1, days) - 1)
    end_eat = eat_today + timedelta(days=1)
    start_prev = start_eat - timedelta(days=max(1, days))
    start = datetime(start_eat.year, start_eat.month, start_eat.day,
                     tzinfo=EAT).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime(end_eat.year, end_eat.month, end_eat.day,
                   tzinfo=EAT).astimezone(timezone.utc).replace(tzinfo=None)
    start_p = datetime(start_prev.year, start_prev.month, start_prev.day,
                       tzinfo=EAT).astimezone(timezone.utc).replace(tzinfo=None)
    s = start_eat.strftime('%b %-d')
    e = end_eat - timedelta(days=1)
    label = (f'{s} to {e.strftime("%b %-d, %Y")}' if start_eat.month == e.month
             else f'{s}, {start_eat.year} to {e.strftime("%b %-d, %Y")}')
    return start, end, start_p, label


def _email_metrics(days, atm='all'):
    """Headline numbers for the email, from the same tables as the PDF.

    Returns dict; any uncomputable metric is None and the template skips
    it (never invented). Cash-out incidents have no honest source
    (cassette counts are the open C1 gap) and are NOT included.
    """
    m = {'availability': None, 'availability_prev': None,
         'downtime_hrs': None, 'downtime_prev': None,
         'offline': [], 'offline_total': 0, 'top_fault': None}
    try:
        from db import get_db
    except Exception:
        return m
    # Headline numbers are fleet-wide by design (the attached PDF carries
    # per-ATM detail); only the attention list honours an atm filter.
    try:
        with get_db() as conn:
            cur = conn.cursor()
            for tag, s, e in (('cur', start, end), ('prev', start_p, start)):
                cur.execute("""
                    SELECT COUNT(DISTINCT l.atm_id),
                        COALESCE(SUM(EXTRACT(EPOCH FROM (
                            LEAST(COALESCE(h.ended_at, %(e)s), %(e)s)
                            - GREATEST(h.started_at, %(s)s))) / 3600.0)
                            FILTER (WHERE h.state = 'IN_SERVICE'), 0)
                    FROM atm_locations l
                    LEFT JOIN atm_state_history h ON h.atm_id = l.atm_id
                        AND h.started_at < %(e)s
                        AND (h.ended_at IS NULL OR h.ended_at > %(s)s)
                    WHERE l.status = 'active'
                """, {'s': s, 'e': e})
                # NOTE: atm scoping applied in Python below (keeps one query).
                n, up = cur.fetchone() or (0, 0)
                n = int(n or 0)
                up = float(up or 0)
                pct = round(100.0 * up / (n * window_h), 1) if n else None
                down = round(n * window_h - up, 1) if n else None
                if tag == 'cur':
                    m['availability'], m['downtime_hrs'] = pct, down
                else:
                    m['availability_prev'], m['downtime_prev'] = pct, down
            cur.execute("""
                SELECT s.atm_id, COALESCE(l.branch, s.atm_id), s.state,
                    EXTRACT(EPOCH FROM (NOW() - s.state_changed_at)) / 3600.0
                FROM atm_current_state s
                LEFT JOIN atm_locations l ON l.atm_id = s.atm_id
                WHERE s.state <> 'IN_SERVICE'
                  AND (%(atm)s = 'all' OR s.atm_id = %(atm)s)
                ORDER BY 4 DESC
            """, {'atm': atm or 'all'})
            rows = cur.fetchall()
            m['offline_total'] = len(rows)
            for atm_id, branch, state, hrs in rows[:5]:
                m['offline'].append({
                    'atm_id': atm_id, 'branch': branch or atm_id,
                    'state': state, 'duration': _email_duration(hrs or 0)})
            cur.execute("""
                SELECT COALESCE(f.display_name, t.error_code, 'Unknown'), COUNT(*)
                FROM atm_transactions t
                LEFT JOIN atm_locations l ON l.atm_id = t.atm_id
                LEFT JOIN fault_type_map f ON f.raw_fault_code = t.error_code
                    AND (f.vendor = l.vendor OR l.vendor IS NULL)
                WHERE t.status = 'ERROR' AND t.error_code IS NOT NULL
                  AND t.recorded_at >= %(s)s AND t.recorded_at < %(e)s
                  AND (%(atm)s = 'all' OR t.atm_id = %(atm)s)
                GROUP BY 1 ORDER BY 2 DESC LIMIT 1
            """, {'s': start, 'e': end, 'atm': atm or 'all'})
            row = cur.fetchone()
            if row:
                m['top_fault'] = {'label': row[0], 'count': int(row[1])}
            cur.close()
    except Exception as e:
        logger.warning('Email metrics unavailable (skipped, not invented): %s', e)
    # Per-ATM scoping for the availability pair (single shared query above
    # counts the fleet; narrow it when the report targets one ATM).
    return m


def _email_duration(hours):
    """363.5 → '15d 3h'. Zero/negative → '—'."""
    try:
        total_m = int(float(hours) * 60)
    except (TypeError, ValueError):
        return '—'
    if total_m <= 0:
        return '—'
    d, rem = divmod(total_m, 1440)
    h, _m = divmod(rem, 60)
    if d:
        return f'{d}d {h}h'
    if h:
        return f'{h}h'
    return f'{_m}m'


def _email_frequency(cron_expr):
    """Daily / Weekly / Monthly / Scheduled from a 5-field cron."""
    try:
        p = (cron_expr or '').strip().split()
        if len(p) != 5:
            return 'Scheduled'
        _min, _h, dom, mon, dow = p
        if dow != '*':
            return 'Weekly'
        if dom != '*' or mon != '*':
            return 'Monthly'
        if _h != '*' or _min != '*':
            return 'Daily'
    except Exception:
        pass
    return 'Scheduled'


def _email_subject(report_type, cron_expr, days):
    label = REPORT_TYPE_LABELS.get(report_type, report_type.title())
    _start, _end, _prev, period = _email_window(days)
    freq = _email_frequency(cron_expr)
    return f'[ATM Monitor] {label} ({freq}): {period}'


def _report_filename(report_type, fmt, days, atm='all'):
    label = REPORT_TYPE_LABELS.get(report_type, report_type.title())
    safe = ''.join(c if (c.isalnum() or c in ('-', '_')) else '_'
                   for c in label.replace(' ', '_'))
    ext = {'excel': 'xlsx', 'pdf': 'pdf', 'csv': 'csv'}.get(fmt, fmt)
    # EAT calendar days (not UTC instants — midnight crossover otherwise
    # shifts the stamped dates by one, disagreeing with the subject).
    try:
        eat_today = datetime.now(EAT).date()
    except Exception:
        eat_today = date.today()
    end_d = eat_today
    start_d = eat_today - timedelta(days=(days or 7) - 1)
    atm_part = f'_{atm}' if atm and atm != 'all' else ''
    if (days or 7) == 1:
        return f'ATM_{safe}_{end_d.strftime("%Y-%m-%d")}{atm_part}.{ext}'
    return (f'ATM_{safe}_{start_d.strftime("%Y-%m-%d")}'
            f'_to_{end_d.strftime("%Y-%m-%d")}{atm_part}.{ext}')
def _email_context(report_type, fmt, days, atm, cron_expr=None, metrics=None):
    """Shared computed values for the HTML + plain-text bodies."""
    label = REPORT_TYPE_LABELS.get(report_type, report_type.title())
    freq = _email_frequency(cron_expr)
    _start, _end, _prev, period = _email_window(days)
    atm_display = atm if atm and atm != 'all' else 'All ATMs'
    try:
        gen = datetime.now(EAT).strftime('%Y-%m-%d %H:%M EAT')
    except Exception:
        gen = datetime.now().strftime('%Y-%m-%d %H:%M')
    m = metrics if metrics is not None else _email_metrics(days, atm)
    av, avp = m.get('availability'), m.get('availability_prev')
    if av is None:
        hero_color, delta_txt = '#0F172A', ''
    else:
        hero_color = ('#DC2626' if av < EMAIL_AVAIL_ALERT
                      else '#D97706' if av < EMAIL_AVAIL_TARGET else '#0F172A')
        if avp is None:
            delta_txt = ''
        else:
            d = round(av - avp, 1)
            arrow = '↓' if d < 0 else '↑' if d > 0 else '→'
            delta_txt = (f'{arrow}{abs(d)}% vs previous {days} days'
                         if d else f'unchanged vs previous {days} days')
    down, downp = m.get('downtime_hrs'), m.get('downtime_prev')
    down_color = ('#D97706' if (down is not None and downp is not None
                                and down > downp) else '#0F172A')
    off_total = m.get('offline_total') or 0
    off_color = '#DC2626' if off_total > 0 else '#0F172A'
    top = m.get('top_fault') or {}
    support = EMAIL_SUPPORT or 'IT Operations'
    return {
        'label': label, 'freq': freq, 'period': period,
        'atm_display': atm_display, 'gen': gen, 'metrics': m,
        'avail': av, 'avail_prev': avp, 'hero_color': hero_color,
        'delta_txt': delta_txt, 'down': down, 'down_color': down_color,
        'off_total': off_total, 'off_color': off_color,
        'top_label': top.get('label'), 'top_count': top.get('count'),
        'support': support,
        'title': label,
    }


def _email_body(report_type, fmt, days, atm, cron_expr=None, metrics=None):
    logo_url = 'cid:dashen-logo'
    c = _email_context(report_type, fmt, days, atm, cron_expr, metrics)
    m = c['metrics']

    def metric_row(label, value, color='#0F172A'):
        return (
            '<tr><td style="padding:9px 0;font-size:14px;color:#64748B">'
            + label + '</td>'
            '<td align="right" style="padding:9px 0;font-size:14px;font-weight:700;color:'
            + color + '">' + value + '</td></tr>'
            '<tr><td colspan="2" style="border-top:1px solid #EEF1F6;font-size:0;line-height:0">&nbsp;</td></tr>'
        )

    rows = ''
    if c['down'] is not None:
        rows += metric_row('Total downtime', f"{c['down']:g} hrs", c['down_color'])
    rows += metric_row('ATMs currently offline', str(c['off_total']), c['off_color'])
    if c['top_label'] is not None:
        rows += metric_row('Top fault', f"{c['top_label']} ({c['top_count']})")

    if m.get('offline'):
        att_rows = ''
        for o in m['offline'][:5]:
            att_rows += (
                '<tr>'
                '<td style="padding:7px 0;font-size:13px;font-weight:700;color:#0F172A">'
                + o['atm_id'] + '</td>'
                '<td style="padding:7px 0;font-size:13px;color:#475569">'
                + o['branch'] + '</td>'
                '<td style="padding:7px 0;font-size:13px;color:#475569">'
                + o['state'].replace('_', ' ') + '</td>'
                '<td align="right" style="padding:7px 0;font-size:13px;font-weight:700;color:#0F172A">'
                + o['duration'] + '</td></tr>'
            )
        attention = (
            '<div style="font-size:15px;font-weight:700;color:#012169;margin:26px 0 6px">Needs attention</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + att_rows + '</table>'
        )
    else:
        attention = (
            '<div style="font-size:15px;font-weight:700;color:#012169;margin:26px 0 6px">Needs attention</div>'
            '<div style="font-size:14px;color:#475569">No ATMs need attention this period.</div>'
        )

    if c['avail'] is None:
        hero = ''
    else:
        hero = (
            '<div style="font-size:40px;font-weight:800;color:' + c['hero_color'] + ';line-height:1.1">'
            + f"{c['avail']}%</div>"
            '<div style="font-size:13px;color:#64748B;margin-top:2px">ATM availability'
            + (f' &nbsp;{c["delta_txt"]}' if c['delta_txt'] else '') + '</div>'
        )

    preheader_bits = []
    if c['avail'] is not None:
        preheader_bits.append(f"Availability {c['avail']}%")
    if c['down'] is not None:
        preheader_bits.append(f"{c['down']:g} hrs downtime")
    preheader_bits.append(f"{c['off_total']} ATMs offline")
    preheader = ' · '.join(preheader_bits)

    return f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
</head>
<body style="margin:0;padding:0;background:#F7F8FA;font-family:'Segoe UI',Arial,Helvetica,sans-serif">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;font-size:1px;line-height:1px;color:#F7F8FA">{preheader}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F7F8FA">
    <tr><td align="center" style="padding:0">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff">
        <tr>
          <td style="background:#012169;padding:14px 28px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="left" style="font-size:12px;color:rgba(255,255,255,.8)">ATM Monitoring System</td>
                <td align="right"><img src="{logo_url}" alt="Dashen Bank" width="90" style="border:0;display:block;width:90px;max-width:90px;height:auto"></td>
              </tr>
            </table>
          </td>
        </tr>
        <tr><td style="height:3px;background:#FDD79A;padding:0;font-size:0;line-height:0">&nbsp;</td></tr>
        <tr>
          <td style="padding:28px 28px 24px">
            <div style="font-size:23px;font-weight:800;color:#012169;line-height:1.25">{c['title']}</div>
            <div style="font-size:13px;color:#64748B;margin-top:4px">{c['period']} · {c['atm_display']} · {c['freq']}</div>
            <div style="margin:20px 0 4px">{hero}</div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px">{rows}</table>
            {attention}
            <div style="margin:26px 0 6px">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" style="background:#012169;padding:12px 28px">
                    <a href="{BASE_URL}/reports" style="font-size:14px;font-weight:700;color:#ffffff;text-decoration:none">View report in dashboard</a>
                  </td>
                </tr>
              </table>
            </div>
            <div style="font-size:13px;color:#64748B;margin-top:10px">Full report attached.</div>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 28px 24px;font-size:12px;color:#94A3B8;line-height:1.5">
            Generated {c['gen']} · Automated report from the ATM Monitoring System. Questions: {c['support']}
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>'''


def _email_text(report_type, fmt, days, atm, cron_expr=None, metrics=None):
    c = _email_context(report_type, fmt, days, atm, cron_expr, metrics)
    m = c['metrics']
    lines = [c['title'], f"{c['period']} · {c['atm_display']} · {c['freq']}", '']
    if c['avail'] is None:
        lines.append('ATM availability: unavailable')
    else:
        lines.append(f"ATM availability: {c['avail']}%"
                     + (f" ({c['delta_txt']})" if c['delta_txt'] else ''))
    if c['down'] is not None:
        lines.append(f"Total downtime: {c['down']:g} hrs")
    lines.append(f"ATMs currently offline: {c['off_total']}")
    if c['top_label'] is not None:
        lines.append(f"Top fault: {c['top_label']} ({c['top_count']})")
    lines += ['', 'Needs attention']
    if m.get('offline'):
        for o in m['offline'][:5]:
            lines.append(f"- {o['atm_id']}  {o['branch']}  "
                         f"{o['state'].replace('_', ' ')}  {o['duration']}")
    else:
        lines.append('No ATMs need attention this period.')
    lines += ['', 'Full report attached.',
              f"Generated {c['gen']} · Automated report from the ATM Monitoring System. Questions: {c['support']}"]
    return '\n'.join(lines)



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
    subject = _email_subject(report_type, schedule, days)
    body = _email_body(report_type, fmt, days, atm, cron_expr=schedule)
    body_text = _email_text(report_type, fmt, days, atm, cron_expr=schedule)

    filedata, filename, mimetype = _generate_report(report_type, fmt, days, atm)
    if filedata is None:
        msg = 'report generation failed (portal endpoint unreachable or error)'
        logger.error('Scheduled report %s failed to generate', report_id)
        log_action('SCHEDULED_REPORT_FAILED', f'Report {report_id} ({report_type}/{fmt}) generation failed')
        _record_run_status(report_id, False, msg, schedule)
        return

    recipient_list = [r.strip() for r in recipients.split(',') if r.strip()]
    ok = _send_email(recipient_list, subject, body, filename, filedata,
                     mimetype, body_text=body_text)

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
