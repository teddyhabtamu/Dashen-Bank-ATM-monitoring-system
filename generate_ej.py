from elasticsearch import Elasticsearch
from faker import Faker
import random
from datetime import datetime, timedelta

fake = Faker()
es = Elasticsearch("http://localhost:9200")

# ATM definitions - realistic Dashen Bank branches
atms = [
    {"id": "ATM-001", "branch": "Addis Ababa Main Branch"},
    {"id": "ATM-002", "branch": "Bole International Branch"},
    {"id": "ATM-003", "branch": "Merkato Branch"},
    {"id": "ATM-004", "branch": "Hawassa Branch"},
    {"id": "ATM-005", "branch": "Dire Dawa Branch"},
]

event_types = [
    {"event": "TXN",   "sub": "WITHDRAWAL",   "weight": 50},
    {"event": "TXN",   "sub": "BALANCE_INQ",  "weight": 25},
    {"event": "TXN",   "sub": "DEPOSIT",      "weight": 10},
    {"event": "TXN",   "sub": "TRANSFER",     "weight": 5},
    {"event": "ERROR", "sub": "CASH_JAM",     "weight": 3},
    {"event": "ERROR", "sub": "CARD_JAM",     "weight": 2},
    {"event": "ERROR", "sub": "COMMS_LOST",   "weight": 2},
    {"event": "ERROR", "sub": "RECEIPT_FAIL", "weight": 1},
    {"event": "CASH",  "sub": "CASSETTE_LOADED", "weight": 1},
    {"event": "MAINT", "sub": "DOOR_OPEN",    "weight": 1},
]

statuses = ["APPROVED", "APPROVED", "APPROVED", "DECLINED", "TIMEOUT", "ERROR"]
error_codes = ["3A7F", "B2C1", "FF01", "44AA", "9E3D"]
currencies = ["ETB", "USD"]

def weighted_choice(choices):
    total = sum(c["weight"] for c in choices)
    r = random.uniform(0, total)
    upto = 0
    for c in choices:
        upto += c["weight"]
        if upto >= r:
            return c

# Generate entries spread over last 30 days
docs = []
base_time = datetime.now() - timedelta(days=30)

print("Generating 1000 EJ log entries...")

for i in range(1000):
    atm = random.choice(atms)
    evt = weighted_choice(event_types)
    ts = base_time + timedelta(
        days=random.randint(0, 29),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
        seconds=random.randint(0, 59)
    )

    doc = {
        "@timestamp": ts.isoformat(),
        "atm_id":     atm["id"],
        "branch":     atm["branch"],
        "event_type": evt["event"],
        "sub_type":   evt["sub"],
        "status":     random.choice(statuses) if evt["event"] == "TXN" else evt["sub"],
    }

    if evt["event"] == "TXN":
        last4 = str(random.randint(1000, 9999))
        doc["card_masked"] = f"************{last4}"
        doc["currency"] = random.choice(currencies)
        if evt["sub"] in ["WITHDRAWAL", "DEPOSIT", "TRANSFER"]:
            doc["amount"] = round(random.choice([100, 200, 500, 1000, 2000, 5000, 10000]), 2)
        if doc["status"] == "APPROVED":
            doc["auth_code"] = str(random.randint(100000, 999999))
        doc["message"] = f"{evt['sub']} card {doc['card_masked']} amount {doc.get('amount','N/A')} {doc['currency']} - {doc['status']}"

    elif evt["event"] == "ERROR":
        doc["error_code"] = random.choice(error_codes)
        doc["cassette_id"] = str(random.randint(1, 4))
        doc["message"] = f"ERROR {evt['sub']} cassette {doc['cassette_id']} code {doc['error_code']}"

    elif evt["event"] == "CASH":
        doc["cassette_id"] = str(random.randint(1, 4))
        doc["amount"] = random.choice([100000, 150000, 200000])
        doc["operator_id"] = f"OP{random.randint(1,10):03d}"
        doc["message"] = f"CASSETTE {doc['cassette_id']} loaded {doc['amount']} ETB by {doc['operator_id']}"

    else:
        doc["operator_id"] = f"OP{random.randint(1,10):03d}"
        doc["message"] = f"MAINTENANCE {evt['sub']} by {doc['operator_id']}"

    docs.append(doc)

# Bulk insert into Elasticsearch
print("Loading into Elasticsearch...")
for i, doc in enumerate(docs):
    es.index(index="atm-electronic-journal", document=doc)
    if (i+1) % 100 == 0:
        print(f"  {i+1}/1000 loaded...")

print("Done! 1000 EJ entries loaded successfully.")
print(f"ATMs covered: {[a['id'] for a in atms]}")
print("Now open Kibana at http://localhost:5601 to explore the data.")