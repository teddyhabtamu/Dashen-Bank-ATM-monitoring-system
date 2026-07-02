import os
import io
import csv
import base64
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from db import get_db
from audit import log_action

logger = logging.getLogger(__name__)

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
    next_run    TIMESTAMP
);
"""

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
SMTP_FROM = os.environ.get('SMTP_FROM', 'atm-monitor@dashenbank.com')
BASE_URL  = os.environ.get('BASE_URL', 'http://localhost:8888')
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')

_LOGO_DATA_URI = None
_logo_path = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')
try:
    with open(_logo_path, 'rb') as _f:
        _LOGO_DATA_URI = 'data:image/png;base64,' + base64.b64encode(_f.read()).decode()
except Exception:
    pass


def init_scheduler_table():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(SCHEDULER_TABLE_SQL)
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
    if fmt == 'csv':
        import requests
        url = f'{BASE_URL}/report/{report_type}/csv?days={days}&atm={atm}'
        try:
            resp = requests.get(url, timeout=30, headers=_headers)
            if resp.status_code == 200:
                buf.write(resp.content)
                buf.seek(0)
                return buf, f'{report_type}.csv', 'text/csv'
        except Exception as e:
            logger.error('Report generation failed: %s', e)
            return None, None, None
    else:
        import requests
        url = f'{BASE_URL}/report/{report_type}/pdf?days={days}&atm={atm}'
        try:
            resp = requests.get(url, timeout=60, headers=_headers)
            if resp.status_code == 200:
                buf.write(resp.content)
                buf.seek(0)
                return buf, f'{report_type}.pdf', 'application/pdf'
        except Exception as e:
            logger.error('Report generation failed: %s', e)
            return None, None, None
    return None, None, None


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

        part = MIMEBase('application', 'octet-stream')
        part.set_payload(filedata.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename={filename}')
        msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
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
    'availability': 'ATM Availability',
    'full': 'Complete Management Report',
}


def _email_body(report_type, fmt, days, atm):
    label = REPORT_TYPE_LABELS.get(report_type, report_type.title())
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    atm_display = atm if atm and atm != 'all' else 'All ATMs'
    logo_url = 'https://i.ibb.co/93gSRkjS/id-CVy-MSM-1-logos.png'
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

        <!-- ── HEADER ── -->
        <tr>
          <td style="background:#012169;padding:32px 40px 24px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center">
                  <img src="{logo_url}" alt="Dashen Bank" style="border:0;display:block;margin:0 auto 12px;width:120px;max-width:100%;height:auto">
                  <div style="font-size:20px;font-weight:700;color:#fff;letter-spacing:-.3px">Dashen Bank S.C.</div>
                  <div style="font-size:11px;color:rgba(255,255,255,.5);font-weight:500;text-transform:uppercase;letter-spacing:1.5px;margin-top:4px">ATM Monitoring System</div>
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
                  <a href="{BASE_URL}" style="display:inline-block;padding:12px 28px;font-size:13px;font-weight:600;color:#fff;text-decoration:none;border-radius:8px">&#8594; Open Report Portal</a>
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


def _run_scheduled_report(report_id, report_type, fmt, schedule, recipients, params):
    today = date.today()
    days = 7
    atm = 'all'
    if params:
        for p in params.split(','):
            if '=' in p:
                k, v = p.split('=', 1)
                if k == 'days': days = int(v)
                if k == 'atm': atm = v

    label = REPORT_TYPE_LABELS.get(report_type, report_type.upper())
    subject = f'[ATM Monitor] {label} - {today}'
    body = _email_body(report_type, fmt, days, atm)

    filedata, filename, mimetype = _generate_report(report_type, fmt, days, atm)
    if filedata is None:
        logger.error('Scheduled report %s failed to generate', report_id)
        log_action('SCHEDULED_REPORT_FAILED', f'Report {report_id} ({report_type}/{fmt}) generation failed')
        return

    recipient_list = [r.strip() for r in recipients.split(',') if r.strip()]
    ok = _send_email(recipient_list, subject, body, filename, filedata, mimetype)

    log_action('SCHEDULED_REPORT', f'Report {report_id} ({report_type}/{fmt}) sent to {len(recipient_list)} recipients, status={"ok" if ok else "failed"}')

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE scheduled_reports SET last_run = NOW(), next_run = %s WHERE id = %s",
                (_calc_next_run(schedule), report_id)
            )
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error('Failed to update schedule run time: %s', e)


def _calc_next_run(schedule):
    from apscheduler.triggers.cron import CronTrigger
    try:
        parts = schedule.split()
        if len(parts) == 5:
            trigger = CronTrigger.from_crontab(schedule)
            return trigger.get_next_fire_time(None, datetime.now())
    except Exception:
        pass
    return None


def load_schedules(scheduler):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, report_type, format, schedule, recipients, params FROM scheduled_reports WHERE enabled = TRUE")
            rows = cur.fetchall()
            cur.close()
        for row in rows:
            rid, report_type, fmt, cron_expr, recipients, params = row
            rid_str = f'report_{rid}'
            if scheduler.get_job(rid_str):
                continue
            scheduler.add_job(
                _run_scheduled_report,
                trigger=CronTrigger.from_crontab(cron_expr),
                args=[rid, report_type, fmt, cron_expr, recipients, params],
                id=rid_str,
                name=f'{report_type}/{fmt} ({rid})',
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info('Loaded scheduled report %s: %s %s (%s)', rid, report_type, fmt, cron_expr)
    except Exception as e:
        logger.error('Failed to load schedules: %s', e)


def create_scheduler(app):
    sched = BackgroundScheduler(daemon=True)
    sched.start()
    load_schedules(sched)
    logger.info('Report scheduler started with %d jobs', len(sched.get_jobs()))
    return sched
