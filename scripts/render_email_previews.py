#!/usr/bin/env python3
"""Render static preview HTML files for scheduled-report email review.

Uses the PRODUCTION builders (subject, body, text, filename) with
hand-made metrics, so what you open in a browser is byte-identical in
structure to a live send. CID logo is swapped for the repo logo path
(preview-only substitution).

Usage: python3 scripts/render_email_previews.py
Output: docs/email-previews/{normal,bad,quiet}.html
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'report-portal'))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# Stub heavy deps missing from dev boxes (previews never touch DB, mail,
# or the real scheduler — builders are pure functions of their inputs).
_aps = types.ModuleType('apscheduler')
_aps_s = types.ModuleType('apscheduler.schedulers')
_aps_b = types.ModuleType('apscheduler.schedulers.background')
_aps_b.BackgroundScheduler = object
_aps_t = types.ModuleType('apscheduler.triggers')
_aps_c = types.ModuleType('apscheduler.triggers.cron')
_aps_c.CronTrigger = object
sys.modules.update({'apscheduler': _aps,
                    'apscheduler.schedulers': _aps_s,
                    'apscheduler.schedulers.background': _aps_b,
                    'apscheduler.triggers': _aps_t,
                    'apscheduler.triggers.cron': _aps_c})
_db = types.ModuleType('db')
_db.get_db = None
sys.modules['db'] = _db
_audit = types.ModuleType('audit')
_audit.log_action = lambda *a, **k: None
sys.modules['audit'] = _audit

from scheduler import (_email_body, _email_text, _email_subject,
                       _report_filename)  # noqa: E402

LOGO_CID = 'cid:dashen-logo'
LOGO_LOCAL = '../report-portal/static/logo.png'

CASES = {
    'normal': {
        'metrics': {
            'availability': 98.4, 'availability_prev': 99.0,
            'downtime_hrs': 41.0, 'downtime_prev': 38.0,
            'offline_total': 3,
            'offline': [
                {'atm_id': 'PATML266', 'branch': 'MAKIRA BRANCH',
                 'state': 'OUT_OF_SERVICE', 'duration': '2d 4h'},
                {'atm_id': 'PAGRL619', 'branch': 'Uraga',
                 'state': 'UNREACHABLE', 'duration': '1d 9h'},
                {'atm_id': 'PATML220', 'branch': 'LALIBELA BRANCH',
                 'state': 'OUT_OF_SERVICE', 'duration': '9h'},
            ],
            'top_fault': {'label': 'Dispenser', 'count': 12},
        },
    },
    'bad': {
        'metrics': {
            'availability': 91.2, 'availability_prev': 97.8,
            'downtime_hrs': 210.5, 'downtime_prev': 60.0,
            'offline_total': 7,
            'offline': [
                {'atm_id': 'PATML266', 'branch': 'MAKIRA BRANCH',
                 'state': 'OUT_OF_SERVICE', 'duration': '6d 2h'},
                {'atm_id': 'PAGRL619', 'branch': 'Uraga',
                 'state': 'UNREACHABLE', 'duration': '4d 11h'},
                {'atm_id': 'PATML220', 'branch': 'LALIBELA BRANCH',
                 'state': 'OUT_OF_SERVICE', 'duration': '3d 6h'},
                {'atm_id': 'PAGRL569', 'branch': 'LOGIYA BRANCH',
                 'state': 'OUT_OF_SERVICE', 'duration': '2d 1h'},
                {'atm_id': 'PATMT076', 'branch': 'DEBRE BIREHAN BRANCH',
                 'state': 'IN_SUPERVISOR', 'duration': '1d 5h'},
            ],
            'top_fault': {'label': 'Network', 'count': 23},
        },
    },
    'quiet': {
        'metrics': {
            'availability': 99.9, 'availability_prev': 99.9,
            'downtime_hrs': 2.4, 'downtime_prev': 2.4,
            'offline_total': 0, 'offline': [],
            'top_fault': None,
        },
    },
}


def main():
    outdir = os.path.join('docs', 'email-previews')
    os.makedirs(outdir, exist_ok=True)
    for name, case in CASES.items():
        m = case['metrics']
        html = _email_body('full', 'pdf', 7, 'all',
                           cron_expr='0 7 * * *', metrics=m)
        html = html.replace(LOGO_CID, LOGO_LOCAL)
        path = os.path.join(outdir, f'{name}.html')
        with open(path, 'w') as f:
            f.write(html)
        subject = _email_subject('full', '0 7 * * *', 7)
        fname = _report_filename('full', 'pdf', 7, 'all')
        print(f'--- {name} ---')
        print('subject:', subject)
        print('filename:', fname)
        print('text version:')
        print(_email_text('full', 'pdf', 7, 'all',
                          cron_expr='0 7 * * *', metrics=m))
        print(f'wrote {path}')
        print()


if __name__ == '__main__':
    main()
