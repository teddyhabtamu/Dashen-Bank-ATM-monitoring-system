FROM python:3.11-slim

WORKDIR /app

# Wheel-only installs are configured through the environment so the pip
# command stays free of option values that can be mistaken for packages.
ENV PIP_ONLY_BINARY=:all:

# Dependencies first.  requirements.txt is the named intent; the lock carries
# the resolved versions and is what actually gets installed.  Copy both before
# the source so the pip layer is cached across code-only builds.
COPY requirements.txt requirements.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --requirement=requirements.lock

# Copy only what the portal serves.  A recursive `COPY . .` would also ship
# local .env files, caches and editor state if .dockerignore ever drifted.
COPY *.py ./
COPY blueprints/ ./blueprints/
COPY templates/ ./templates/
COPY static/ ./static/
COPY config/ ./config/
COPY zabbix_modules/ ./zabbix_modules/

EXPOSE 8888

# Production server: 3 sync workers (isolation + parallelism the Flask dev
# server lacks). 300s worker timeout: a verified video can take over a minute
# over the Zabbix-agent chunk transport, and the old 120s limit could kill the
# worker mid-stream. The portal's bridge client has its own 180s read timeout,
# so a stalled ATM fails as a clean transport error before this limit.
# Port honors REPORT_PORTAL_PORT; INTERNAL_API_KEY must be set in .env so all
# workers share the scheduler auth key (see app.py).
CMD ["sh", "-c", "exec gunicorn -w 3 --timeout 300 -b 0.0.0.0:${REPORT_PORTAL_PORT:-8888} app:app"]
