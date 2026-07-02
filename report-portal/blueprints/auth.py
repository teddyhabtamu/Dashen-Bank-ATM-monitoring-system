import os
import logging
from functools import wraps
from flask import Blueprint, render_template, request, redirect, session, url_for, current_app
from urllib.parse import urlparse
from audit import log_action
from db import get_db
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', '')
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')


def _is_internal():
    """Check if the request is an internal scheduler call."""
    if not INTERNAL_API_KEY:
        return False
    return request.headers.get('X-Internal-Key', '') == INTERNAL_API_KEY

# Role constants
ROLE_VIEWER = 'viewer'
ROLE_OPERATOR = 'operator'
ROLE_ADMIN = 'admin'
ROLE_HIERARCHY = {
    ROLE_VIEWER: 1,
    ROLE_OPERATOR: 2,
    ROLE_ADMIN: 3,
}


def init_admin_user():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT username, role FROM app_users WHERE username = %s", (ADMIN_USER,))
            row = cur.fetchone()
            if row is None and ADMIN_PASS:
                cur.execute(
                    "INSERT INTO app_users (username, password_hash, role) VALUES (%s, %s, %s)",
                    (ADMIN_USER, generate_password_hash(ADMIN_PASS), ROLE_ADMIN)
                )
                conn.commit()
                logger.info('Default admin user seeded')
            elif row and row[1] != ROLE_ADMIN and ADMIN_PASS:
                # Update existing admin user role to admin
                cur.execute(
                    "UPDATE app_users SET role = %s, password_hash = %s WHERE username = %s",
                    (ROLE_ADMIN, generate_password_hash(ADMIN_PASS), ADMIN_USER)
                )
                conn.commit()
                logger.info('Updated admin user role to admin')
            cur.close()
    except Exception as e:
        logger.warning('Could not seed admin user (non-fatal): %s', e)


def _check_password(username, password):
    if username == ADMIN_USER and password == ADMIN_PASS:
        return True
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT password_hash FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            cur.close()
            if row and check_password_hash(row[0], password):
                return True
    except Exception:
        pass
    return False


def _get_role(username):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT role FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            cur.close()
            if row:
                return row[0]
    except Exception:
        pass
    return ROLE_ADMIN if username == ADMIN_USER else ROLE_VIEWER


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_internal():
            return f(*args, **kwargs)
        if not session.get('logged_in'):
            return redirect(url_for('auth.login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Require one of the given roles. Supports role hierarchy."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if _is_internal():
                return f(*args, **kwargs)
            if not session.get('logged_in'):
                return redirect(url_for('auth.login', next=request.path))
            user_role = session.get('role', ROLE_VIEWER)
            user_level = ROLE_HIERARCHY.get(user_role, 0)
            required_level = min(ROLE_HIERARCHY.get(r, 0) for r in roles)
            if user_level < required_level:
                logger.warning('Access denied: user=%s role=%s required=%s',
                               session.get('username'), user_role, roles)
                return render_template('base.html', error='Access denied. Insufficient permissions.'), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def csrf_exempt(f):
    """Mark a view as exempt from CSRF protection (placeholder).
    Actual exemption is done in create_app() after csrf.init_app(app)."""
    return f


@bp.route('/login', methods=['GET', 'POST'])
@csrf_exempt
def login():
    next_page = request.args.get('next') or '/'
    parsed = urlparse(next_page)
    if parsed.netloc or parsed.scheme:
        next_page = '/'

    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if not ADMIN_PASS:
            logger.error('Login failed: ADMIN_PASS not set on server')
            error = 'ADMIN_PASS environment variable is not set on the server.'
            return render_template('login.html', error=error)

        if _check_password(username, password):
            session['logged_in'] = True
            session['username'] = username
            session['role'] = _get_role(username)
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE app_users SET last_login = NOW() WHERE username = %s", (username,))
                    conn.commit()
                    cur.close()
            except Exception:
                pass
            logger.info('Login successful: user=%s role=%s from=%s',
                        username, session['role'], request.remote_addr)
            log_action('LOGIN', f'User {username} (role={session["role"]}) logged in from {request.remote_addr}')
            return redirect(next_page)

        logger.warning('Login failed: user=%s from=%s', username, request.remote_addr)
        error = 'Invalid username or password.'
        return render_template('login.html', error=error, next=next_page)

    return render_template('login.html', next=next_page)


@bp.route('/logout')
def logout():
    log_action('LOGOUT', f'User {session.get("username", "unknown")} logged out')
    session.clear()
    return redirect(url_for('auth.login'))
