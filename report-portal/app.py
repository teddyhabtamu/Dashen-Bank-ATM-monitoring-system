import os
import time
from flask import Flask, g, request

REPORT_PORTAL_PORT = os.environ.get('REPORT_PORTAL_PORT', '8888')
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')


def create_app():
    app = Flask(__name__)
    secret = os.environ.get('FLASK_SECRET_KEY')
    app.secret_key = secret if secret else os.urandom(24).hex()
    app.debug = FLASK_DEBUG

    if not FLASK_DEBUG:
        import logging
        logging.getLogger('werkzeug').setLevel(logging.WARNING)

    from blueprints.auth       import bp as auth_bp
    from blueprints.portal     import bp as portal_bp
    from blueprints.admin      import bp as admin_bp
    from blueprints.ej_search  import bp as ej_bp
    from routes                import bp as report_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(ej_bp)
    app.register_blueprint(report_bp)

    app.jinja_env.globals['enumerate'] = enumerate

    @app.route('/health')
    def health():
        return {'status': 'ok'}, 200

    @app.before_request
    def start_timer():
        g.start = time.time()

    @app.after_request
    def log_request(response):
        if not FLASK_DEBUG:
            dt = time.time() - g.start
            app.logger.info('%s %s → %s (%.0fms)',
                            request.method, request.path,
                            response.status_code, dt * 1000)
        return response

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(REPORT_PORTAL_PORT), debug=FLASK_DEBUG)
