#!/usr/bin/env python3
import os
from flask import Flask
from routes import bp as report_bp
from static_html import PORTAL_HTML

app = Flask(__name__)
app.register_blueprint(report_bp)

# Configurable via .env — defaults work for local dev
GRAFANA_URL        = os.environ.get('GRAFANA_URL', 'http://localhost:3002')
REPORT_PORTAL_PORT = os.environ.get('REPORT_PORTAL_PORT', '8888')

@app.route('/')
def index():
    html = PORTAL_HTML\
        .replace('{{GRAFANA_URL}}', GRAFANA_URL)\
        .replace('{{REPORT_PORTAL_PORT}}', REPORT_PORTAL_PORT)
    return html


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(REPORT_PORTAL_PORT), debug=True)
