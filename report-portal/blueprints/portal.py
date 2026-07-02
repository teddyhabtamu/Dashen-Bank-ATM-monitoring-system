"""
blueprints/portal.py
Main Report Portal dashboard routes.
"""
from flask import Blueprint, render_template
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN

bp = Blueprint('portal', __name__)


@bp.route('/')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def index():
    return render_template('portal.html')
