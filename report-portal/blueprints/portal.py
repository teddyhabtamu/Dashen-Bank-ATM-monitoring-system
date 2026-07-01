"""
blueprints/portal.py
Main Report Portal dashboard routes.
"""
from flask import Blueprint, render_template
from blueprints.auth import login_required

bp = Blueprint('portal', __name__)


@bp.route('/')
@login_required
def index():
    return render_template('portal.html')
