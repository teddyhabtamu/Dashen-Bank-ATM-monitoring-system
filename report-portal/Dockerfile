FROM python:3.11-slim

WORKDIR /app

# Wheel-only installs are configured through the environment so the pip
# command stays free of option values that can be mistaken for packages.
# Bytecode is not written into the bind-mounted /app directory.
ENV PIP_ONLY_BINARY=:all: \
    PYTHONDONTWRITEBYTECODE=1

# Dependencies first.  requirements.txt is the named intent; the lock carries
# the resolved versions and is what actually gets installed.  Copy both before
# the source so the pip layer is cached across code-only builds.
COPY requirements.txt requirements.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --requirement=requirements.lock \
    && useradd --create-home --uid 10001 --shell /bin/false appuser

# Copy only what the portal serves.  A recursive `COPY . .` would also ship
# local .env files, caches and editor state if .dockerignore ever drifted.
COPY --chown=appuser:appuser *.py ./
COPY --chown=appuser:appuser blueprints/ ./blueprints/
COPY --chown=appuser:appuser templates/ ./templates/
COPY --chown=appuser:appuser static/ ./static/
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser zabbix_modules/ ./zabbix_modules/

EXPOSE 8888

# Production server: 3 sync workers (isolation + parallelism the Flask dev
# server lacks). 300s worker timeout: a verified video can take over a minute
# over the Zabbix-agent chunk transport, and the old 120s limit could kill the
# worker mid-stream. The portal's bridge client has its own 180s read timeout,
# so a stalled ATM fails as a clean transport error before this limit.
# Port honors REPORT_PORTAL_PORT; INTERNAL_API_KEY must be set in .env so all
# workers share the scheduler auth key (see app.py).
# The scheduler lock lives under the app user's home.  Set HOME explicitly so
# os.path.expanduser('~') cannot fall back to the image's /root.
ENV HOME=/home/appuser
USER appuser
CMD ["sh", "-c", "exec gunicorn -w 3 --timeout 300 -b 0.0.0.0:${REPORT_PORTAL_PORT:-8888} app:app"]
