FROM python:3.11-slim

WORKDIR /app

# Dependencies first. The previous order ran pip before COPY, so a clean
# build failed with "No such file or directory: requirements.txt". Copying
# only the requirements keeps the pip layer cached across code-only builds.
COPY requirements.txt ./
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
# A committed lock is optional; apply it after the source copy so the
# image is reproducible whenever requirements.lock is present.
RUN if [ -f requirements.lock ]; then pip install --no-cache-dir -r requirements.lock; fi

EXPOSE 8888

# Production server: 3 sync workers (isolation + parallelism the Flask dev
# server lacks). 300s worker timeout: a verified video can take over a minute
# over the Zabbix-agent chunk transport, and the old 120s limit could kill the
# worker mid-stream. The portal's bridge client has its own 180s read timeout,
# so a stalled ATM fails as a clean transport error before this limit.
# Port honors REPORT_PORTAL_PORT; INTERNAL_API_KEY must be set in .env so all
# workers share the scheduler auth key (see app.py).
CMD ["sh", "-c", "exec gunicorn -w 3 --timeout 300 -b 0.0.0.0:${REPORT_PORTAL_PORT:-8888} app:app"]
