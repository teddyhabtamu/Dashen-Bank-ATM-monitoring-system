#!/usr/bin/env python3
from flask import Flask
from routes import bp as report_bp
from static_html import PORTAL_HTML

app = Flask(__name__)
app.register_blueprint(report_bp)

@app.route('/')
def index():
    return PORTAL_HTML


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8888, debug=True)
