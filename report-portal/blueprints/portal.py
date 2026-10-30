"""
blueprints/portal.py
Main ATM Monitoring System dashboard routes.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN

bp = Blueprint('portal', __name__)
EAT = timezone(timedelta(hours=3))


@bp.route('/')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def index():
    return render_template('portal.html')


@bp.route('/reports')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def reports():
    today = datetime.now(EAT).date()
    start = today - timedelta(days=6)
    return render_template('reports.html',
                           default_start=start.isoformat(),
                           default_end=today.isoformat())
