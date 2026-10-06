import os
import re
import time
import logging
from functools import wraps
from flask import Blueprint, render_template, request, redirect, session, url_for, current_app, flash
from urllib.parse import urlparse
from audit import log_action
from db import get_db
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', '')
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')

# Directory auth (bank AD down the road, mock LDAP in the lab).
# AUTH_MODE=local (default): DB users only — current UAT behavior, untouched.
# AUTH_MODE=ldap: directory first; local DB only if the directory is DOWN
# (bad directory credentials never fall through to local).
AUTH_MODE = os.environ.get('AUTH_MODE', 'local').strip().lower()
LDAP_HOST = os.environ.get('LDAP_HOST', '')
try:
    LDAP_PORT = int(os.environ.get('LDAP_PORT', '389') or 389)
except (ValueError, TypeError):
    logger.warning('Bad LDAP_PORT value, defaulting to 389')
    LDAP_PORT = 389
LDAP_BASE_DN = os.environ.get('LDAP_BASE_DN', '')
LDAP_BIND_DN = os.environ.get('LDAP_BIND_DN', '')
LDAP_BIND_PASSWORD = os.environ.get('LDAP_BIND_PASSWORD', '')
LDAP_LOGIN_ATTR = os.environ.get('LDAP_LOGIN_ATTR', 'uid')  # sAMAccountName on AD
LDAP_ADMINS_GROUP = os.environ.get('LDAP_ADMINS_GROUP', 'ATM-Admins')
LDAP_OPERATORS_GROUP = os.environ.get('LDAP_OPERATORS_GROUP', 'ATM-Operators')
# TLS for real AD (LDAPS). Lab default: unset/false = plain, byte-identical.
LDAP_USE_SSL = os.environ.get('LDAP_USE_SSL', 'false').strip().lower() == 'true'
LDAP_CA_CERT_FILE = os.environ.get('LDAP_CA_CERT_FILE', '').strip()

try:
    from ldap3 import Server, Connection, ALL, Tls
    from ldap3.core.exceptions import LDAPSocketOpenError
    _LDAP_LIB = True
    try:
        from ssl import CERT_REQUIRED
        _SSL_FLAG = True
    except ImportError:
        _SSL_FLAG = False
except ImportError:
    _LDAP_LIB = False
    _SSL_FLAG = False

# Rate limiting: {ip: [(timestamp, attempt_count)]}
_login_attempts = {}
RATE_LIMIT = int(os.environ.get('LOGIN_RATE_LIMIT', '5'))
RATE_WINDOW = int(os.environ.get('LOGIN_RATE_WINDOW', '60'))  # seconds

def _is_rate_limited(ip):
    """Check if an IP has exceeded the login rate limit."""
    now = time.time()
    # Clean up old entries
    _login_attempts[ip] = [t for t in _login_attempts.get(ip, []) if now - t < RATE_WINDOW]
    # Sweep fully-stale IPs so the dict can't grow without bound
    # (never the caller itself — it was just refreshed above).
    if len(_login_attempts) > 1000:
        for _ip in list(_login_attempts):
            if _ip == ip:
                continue
            _login_attempts[_ip] = [t for t in _login_attempts[_ip] if now - t < RATE_WINDOW]
            if not _login_attempts[_ip]:
                del _login_attempts[_ip]
    if len(_login_attempts[ip]) >= RATE_LIMIT:
        return True
    _login_attempts[ip].append(now)
    return False


def _is_internal():
    """Internal scheduler calls skip login/RBAC — but ONLY on the report
    generation endpoints the scheduler actually uses. A valid key on any
    other path (admin, users, incidents, ...) authenticates nothing."""
    if not INTERNAL_API_KEY:
        return False
    if not request.path.startswith('/report/'):
        return False
    return request.headers.get('X-Internal-Key', '') == INTERNAL_API_KEY


def _ldap_escape(value):
    """Escape RFC 4515 special characters in LDAP search filter values."""
    return (str(value).replace('\\', '\\5c').replace('*', '\\2a')
            .replace('(', '\\28').replace(')', '\\29').replace('\x00', '\\00'))

# Role constants
ROLE_VIEWER = 'viewer'
ROLE_OPERATOR = 'operator'
ROLE_ADMIN = 'admin'
ROLE_HIERARCHY = {
    ROLE_VIEWER: 1,
    ROLE_OPERATOR: 2,
    ROLE_ADMIN: 3,
}


def password_error(password, username=None):
    """Bank password policy: min 10 chars with ALL of UPPER/lowercase,
    digits and symbols. (Interim until AD cutover, when the domain policy
    governs.) Returns an error string or None. Shared by admin user
    management and self-service change (single source of truth)."""
    if not password or len(password) < 10:
        return 'Password must be at least 10 characters.'
    missing = []
    if not re.search(r'[A-Z]', password):
        missing.append('UPPERCASE')
    if not re.search(r'[a-z]', password):
        missing.append('lowercase')
    if not re.search(r'[0-9]', password):
        missing.append('digits')
    if not re.search(r'[^A-Za-z0-9]', password):
        missing.append('symbols')
    if missing:
        return 'Password must include: ' + ', '.join(missing) + '.'
    return None


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
                # Re-enforce the admin role (prevents lockout), but NEVER
                # overwrite an existing password hash — a password changed
                # via User Management must survive container restarts.
                cur.execute(
                    "UPDATE app_users SET role = %s WHERE username = %s",
                    (ROLE_ADMIN, ADMIN_USER)
                )
                conn.commit()
                logger.info('Re-enforced admin role (password untouched)')
            cur.close()
    except Exception as e:
        logger.warning('Could not seed admin user (non-fatal): %s', e)


def _ldap_auth(username, password):
    """Directory bind + group→role mapping.

    Returns (ok, role, reachable). Bad credentials yield (False, None, True)
    and must NOT fall through to local auth; only an unreachable directory
    yields reachable=False (caller may then fail over to local).
    """
    if not _LDAP_LIB or not LDAP_HOST or not LDAP_BASE_DN or not password:
        logger.error('LDAP auth unavailable (lib/host/base-dn misconfigured)')
        return False, None, False
    # TLS setup for LDAPS (AD). Fail closed: SSL on + CA configured but
    # file missing => do not connect insecurely.
    tls = None
    if LDAP_USE_SSL:
        if LDAP_CA_CERT_FILE:
            import os as _os
            if not _os.path.isfile(LDAP_CA_CERT_FILE):
                logger.error('LDAP CA file missing: %s', LDAP_CA_CERT_FILE)
                return False, None, False
            tls = Tls(ca_certs_file=LDAP_CA_CERT_FILE,
                      validate=CERT_REQUIRED) if _SSL_FLAG else Tls(
                          ca_certs_file=LDAP_CA_CERT_FILE)
        # No CA file: system trust (AD cert from public/internal CA in store).
    try:
        server = Server(LDAP_HOST, port=LDAP_PORT, get_info=ALL,
                        connect_timeout=5, use_ssl=LDAP_USE_SSL, tls=tls)
        bind_kwargs = {'auto_bind': True}
        if LDAP_BIND_DN:
            bind_kwargs.update(user=LDAP_BIND_DN, password=LDAP_BIND_PASSWORD)
        svc = Connection(server, **bind_kwargs)
        if not re.match(r'^[A-Za-z][A-Za-z0-9-]*$', LDAP_LOGIN_ATTR or ''):
            logger.error('Refusing LDAP search: bad LDAP_LOGIN_ATTR %r', LDAP_LOGIN_ATTR)
            svc.unbind()
            return False, None, True
        svc.search(LDAP_BASE_DN, f'({_ldap_escape(LDAP_LOGIN_ATTR)}={_ldap_escape(username)})',
                   attributes=['cn'])
        if not svc.entries:
            svc.unbind()
            return False, None, True
        user_dn = svc.entries[0].entry_dn
        # Password check first (bind as the user; failures raise).
        Connection(server, user=user_dn, password=password,
                   auto_bind=True).unbind()
        # Group lookup on the service connection: least privilege issues,
        # works even when users cannot read the groups branch themselves.
        svc.search(LDAP_BASE_DN,
                   f'(&(objectClass=groupOfNames)(member={_ldap_escape(user_dn)}))',
                   attributes=['cn'])
        groups = {str(e.cn) for e in svc.entries}
        svc.unbind()
        if LDAP_ADMINS_GROUP in groups:
            role = ROLE_ADMIN
        elif LDAP_OPERATORS_GROUP in groups:
            role = ROLE_OPERATOR
        else:
            role = ROLE_VIEWER
        return True, role, True
    except LDAPSocketOpenError as e:
        # Directory itself unreachable: caller may fail over to local auth.
        logger.warning('LDAP directory unreachable: %s', e)
        return False, None, False
    except Exception as e:
        # Server answered (bad credentials, unknown user, ...): no fallback.
        logger.warning('LDAP auth refused for user=%s: %s', username, e)
        return False, None, True


def _check_password(username, password):
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


def _must_change_pw(username):
    """Whether the user still owes a first-login (or admin-reset) password
    change. Missing column on ancient DBs (pre-migration) means False."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT must_change_pw FROM app_users WHERE username = %s",
                        (username,))
            row = cur.fetchone()
            cur.close()
            return bool(row and row[0])
    except Exception:
        return False


def _force_change_redirect():
    """Redirect users who owe a password change to /profile — from every
    page except the profile/logout endpoints themselves. Returns a
    redirect response or None."""
    if not session.get('must_change_pw'):
        return None
    if request.path in ('/profile', '/profile/password', '/logout'):
        return None
    return redirect(url_for('auth.profile'))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_internal():
            return f(*args, **kwargs)
        if not session.get('logged_in'):
            return redirect(url_for('auth.login', next=request.path))
        forced = _force_change_redirect()
        if forced:
            return forced
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
            forced = _force_change_redirect()
            if forced:
                return forced
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

        # Rate limiting
        if _is_rate_limited(request.remote_addr):
            logger.warning('Rate limited login attempt from %s', request.remote_addr)
            error = f'Too many login attempts. Please wait {RATE_WINDOW} seconds.'
            return render_template('login.html', error=error, next=next_page)

        if AUTH_MODE != 'ldap' and not ADMIN_PASS:
            # No seed password configured: the break-glass seed is skipped,
            # but existing DB users can still log in (fail open for auth,
            # closed for privilege — nobody gains anything they lack).
            logger.warning('ADMIN_PASS unset: break-glass seed disabled, DB users only')

        authed, role = False, None
        if AUTH_MODE == 'ldap':
            authed, role, reachable = _ldap_auth(username, password)
            if not authed and not reachable:
                logger.warning('LDAP unreachable, failing over to local auth')
                if _check_password(username, password):
                    authed, role = True, _get_role(username)
        else:
            if _check_password(username, password):
                authed, role = True, _get_role(username)

        if authed:
            session['logged_in'] = True
            session.permanent = True  # honor PERMANENT_SESSION_LIFETIME (4h), not browser-close
            session['username'] = username
            session['role'] = role
            session['must_change_pw'] = _must_change_pw(username) if AUTH_MODE != 'ldap' else False
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
            if session.get('must_change_pw'):
                flash('You must change your password before continuing.', 'warning')
                return redirect(url_for('auth.profile'))
            return redirect(next_page)

        logger.warning('Login failed: user=%s from=%s', username, request.remote_addr)
        try:
            log_action('LOGIN_FAILED', f'user={username} from={request.remote_addr}')
        except Exception:
            pass
        error = 'Invalid username or password.'
        return render_template('login.html', error=error, next=next_page)

    return render_template('login.html', next=next_page)


@bp.route('/profile')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def profile():
    """Self-service account page (password change). All roles."""
    return render_template('profile.html', username=session.get('username', ''),
                           must_change=bool(session.get('must_change_pw')))


@bp.route('/profile/password', methods=['POST'])
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def change_password():
    username = session.get('username', '')
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if not username:
        flash('Session expired — please log in again.', 'error')
        return redirect(url_for('auth.login'))
    if not _check_password(username, current):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('auth.profile'))
    if new != confirm:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('auth.profile'))
    pw_err = password_error(new, username)
    if pw_err:
        flash(pw_err, 'error')
        return redirect(url_for('auth.profile'))
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE app_users SET password_hash = %s, must_change_pw = FALSE WHERE username = %s",
                        (generate_password_hash(new), username))
            conn.commit()
            cur.close()
        session['must_change_pw'] = False
        session.modified = True
        log_action('PASSWORD_CHANGE', f'User {username} changed their own password')
        flash('Password changed successfully.', 'success')
    except Exception as e:
        flash(f'Could not change password: {e}', 'error')
    return redirect(url_for('auth.profile'))


@bp.route('/logout')
def logout():
    log_action('LOGOUT', f'User {session.get("username", "unknown")} logged out')
    session.clear()
    return redirect(url_for('auth.login'))
