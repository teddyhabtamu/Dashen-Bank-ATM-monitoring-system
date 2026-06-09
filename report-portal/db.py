import os
import psycopg2

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', 'zabbix_pass')


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASS)
import os
import psycopg2

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', 'zabbix_pass')


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASS)
