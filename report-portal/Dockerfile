FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/* \
    && pip install flask psycopg2-binary openpyxl reportlab requests apscheduler flask-wtf ldap3

COPY . .

EXPOSE 8888

CMD ["python3", "-u", "app.py"]
