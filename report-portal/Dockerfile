FROM python:3.11-slim

WORKDIR /app

RUN pip install flask psycopg2-binary openpyxl reportlab requests

COPY . .

EXPOSE 8888

CMD ["python3", "app.py"]
