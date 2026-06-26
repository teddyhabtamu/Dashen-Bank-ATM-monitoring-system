#!/usr/bin/env python3
"""
app.py — Report Portal entry point.

Registers all blueprints:
  - blueprints.portal    → /               (Report Portal dashboard)
  - blueprints.admin     → /admin/atm*     (ATM Registration)
  - blueprints.ej_search → /ej-search*     (Electronic Journal Search)
  - routes.bp            → /api/* /report/* (API + report downloads)
"""
import os
from flask import Flask

REPORT_PORTAL_PORT = os.environ.get('REPORT_PORTAL_PORT', '8888')


def create_app():
    app = Flask(__name__)

    # ── Blueprints ──────────────────────────────────────────────
    from blueprints.portal    import bp as portal_bp
    from blueprints.admin     import bp as admin_bp
    from blueprints.ej_search import bp as ej_bp
    from routes               import bp as report_bp

    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(ej_bp)
    app.register_blueprint(report_bp)

    # Register the enumerate filter for Jinja2 templates
    app.jinja_env.globals['enumerate'] = enumerate

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(REPORT_PORTAL_PORT), debug=True)
