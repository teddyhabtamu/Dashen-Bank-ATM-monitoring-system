"""
blueprints/auth.py
Authentication routes — login, logout, and login_required decorator.
Credentials come from environment variables ADMIN_USER / ADMIN_PASS.
"""
import os
import logging
from functools import wraps
from flask import Blueprint, render_template, request, redirect, session, url_for

logger = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', '')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('auth.login', next=request.path))
        return f(*args, **kwargs)
    return decorated


@bp.route('/login', methods=['GET', 'POST'])
def login():
    next_page = request.args.get('next') or '/'

    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if not ADMIN_PASS:
            logger.error('Login failed: ADMIN_PASS not set on server')
            error = 'ADMIN_PASS environment variable is not set on the server.'
            return render_template('login.html', error=error)

        if username == ADMIN_USER and password == ADMIN_PASS:
            session['logged_in'] = True
            session['username'] = username
            logger.info('Login successful: user=%s from=%s', username, request.remote_addr)
            return redirect(next_page)

        logger.warning('Login failed: user=%s from=%s', username, request.remote_addr)
        error = 'Invalid username or password.'
        return render_template('login.html', error=error, next=next_page)

    return render_template('login.html', next=next_page)


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
