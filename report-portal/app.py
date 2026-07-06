import os
import time
import secrets
from datetime import timedelta
from flask import Flask, g, request, session, jsonify, render_template, redirect, flash
from flask_wtf.csrf import CSRFProtect

REPORT_PORTAL_PORT = os.environ.get('REPORT_PORTAL_PORT', '8888')
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')

csrf = CSRFProtect()

# Generate ephemeral key for scheduler → app internal requests
INTERNAL_API_KEY = secrets.token_urlsafe(32)


def create_app():
    app = Flask(__name__)
    secret = os.environ.get('FLASK_SECRET_KEY')
    app.secret_key = secret if secret else os.urandom(24).hex()
    app.debug = FLASK_DEBUG
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
        # Flask-WTF CSRF config (4 lines)
        WTF_CSRF_ENABLED=True,
        WTF_CSRF_TIME_LIMIT=None,
        WTF_CSRF_SSL_STRICT=False,
        WTF_CSRF_METHODS=['POST', 'PUT', 'PATCH', 'DELETE'],
    )

    if not FLASK_DEBUG:
        import logging
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
    import logging
    import sys
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    logging.getLogger('scheduler').setLevel(logging.DEBUG)

    # Set internal API key BEFORE importing modules that read it at module level
    os.environ.setdefault('INTERNAL_API_KEY', INTERNAL_API_KEY)

    csrf.init_app(app)

    from blueprints.auth       import bp as auth_bp, init_admin_user
    from blueprints.portal     import bp as portal_bp
    from blueprints.admin      import bp as admin_bp
    from blueprints.ej_search  import bp as ej_bp
    from blueprints.anomalies  import bp as anomalies_bp
    from routes                import bp as report_bp
    from audit                 import init_audit_schema
    from scheduler             import init_scheduler_table, create_scheduler

    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(ej_bp)
    app.register_blueprint(anomalies_bp)
    app.register_blueprint(report_bp)

    csrf.exempt(app.view_functions['auth.login'])

    init_audit_schema()
    init_admin_user()
    init_scheduler_table()
    app.config['SCHEDULER'] = create_scheduler(app)

    @app.before_request
    def start_timer():
        g.start = time.time()

    @app.after_request
    def log_request(response):
        if not FLASK_DEBUG and hasattr(g, 'start'):
            dt = time.time() - g.start
            app.logger.info('%s %s → %s (%.0fms)',
                            request.method, request.path,
                            response.status_code, dt * 1000)
        return response

    @app.errorhandler(403)
    def forbidden(e):
        flash('Access denied. You do not have permission for this page.', 'error')
        return redirect(request.referrer or '/')

    @app.errorhandler(404)
    def not_found(e):
        return render_template('404.html'), 404

    @app.route('/health')
    def health():
        return {'status': 'ok'}, 200

    return app


app = create_app()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(REPORT_PORTAL_PORT), debug=FLASK_DEBUG)
