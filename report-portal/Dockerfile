FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/* \
    && pip install flask psycopg2-binary openpyxl reportlab requests apscheduler flask-wtf ldap3 gunicorn

COPY . .

EXPOSE 8888

# Production server: 3 sync workers (isolation + parallelism the Flask dev
# server lacks), 120s timeout for slow PDF/Excel generations at fleet scale.
# Port honors REPORT_PORTAL_PORT; INTERNAL_API_KEY must be set in .env so all
# workers share the scheduler auth key (see app.py).
CMD ["sh", "-c", "exec gunicorn -w 3 --timeout 120 -b 0.0.0.0:${REPORT_PORTAL_PORT:-8888} app:app"]
