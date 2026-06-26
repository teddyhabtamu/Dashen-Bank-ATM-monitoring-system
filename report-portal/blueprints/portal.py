"""
blueprints/portal.py
Main Report Portal dashboard routes.
"""
from flask import Blueprint, render_template

bp = Blueprint('portal', __name__)


@bp.route('/')
def index():
    return render_template('portal.html')
