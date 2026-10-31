# Full-fleet import for performance testing

> **Purpose:** load the 1,202-ATM workbook into UAT to exercise import,
> provisioning, the outbox, the fleet sweep and the UI at production scale.
> This is a **test fleet**: every row gets a `TST-` ATM ID so it can be removed
> without touching real ATMs.

## 1. What the workbook lacks

`docs/ATM INFORMATION.xlsx` has the terminal ID, name, IP, owner branch,
district and vendor string. The portal import also requires `atm_id`, `city`,
`region`, `model` and `status`. The preparation script fills only those gaps:

| Field | Rule |
|---|---|
| `atm_id` | `TST-<workbook No>`, e.g. `TST-0001` |
| `city`, `region` | Derived from the district — **test placeholders**, not bank records |
| `model` | Approved default: NCR `SelfServ 34`, GRG `AD2600` |
| `branch` | Owner branch, normalised to `… BRANCH`; missing values use the ATM name |
| `status` | `active` |
| `snmp_community` | `--snmp-community` value, default `public`; real polling may fail |
| `ej_log_format` | `NCR_APTRA` / `GRG` |
| `ej_hostname`, `latitude`, `longitude`, `install_date` | Left empty |

Every generated row is checked with the portal’s own `_validate()` before the
CSV is written.

## 2. Generate the CSVs

```bash
# Recommended first pass: RFC 2544 addresses, no production ATM is polled.
python3 scripts/prepare_fleet_import.py --ip-mode synthetic

# Live-IP pass: only with network/security approval.
python3 scripts/prepare_fleet_import.py --ip-mode real

# Small representative pilot (mixed vendors/districts).
python3 scripts/prepare_fleet_import.py --ip-mode synthetic --limit 50

# Match the lab SNMP community instead of the `public` placeholder.
python3 scripts/prepare_fleet_import.py --ip-mode synthetic --snmp-community dashen_sim
```

Outputs:

- `config/postgres/atm_locations_test_synthetic.csv`
- `config/postgres/atm_locations_test_real_ips.csv`
- `config/postgres/atm_locations_test_<mode>_details.csv` — per-row audit of
  every filled field and note
- `docs/atm-import-readiness-<mode>.md` — counts, assumptions and safety notes

The CSVs contain real fleet IPs/TIDs in `real` mode; they are git-ignored and
must not be committed.

## 3. Before the full import

1. **Stop the ticket storm.** Disable the GLPI ticket action or put the
   `TST-` hosts in a Zabbix maintenance window. Otherwise 1,202 unreachable
   hosts can create a ticket per ATM.
2. **Pilot first.** Import the `--limit 50` CSV, watch provisioning and Zabbix
   load, then import the full set.
3. **Know the queue.** The portal enqueues every row in `atm_provision_state`;
   the scheduler creates about 25 hosts per minute. The first 100 are attempted
   immediately. The full 1,202 takes roughly 45 minutes.
4. **Protect the UI.** The first Fleet health sweep over 1,202 hosts is
   expensive. Run it once and let the 15-minute cache serve the page.

## 4. Import

Portal → **ATMs** → **Import CSV** → choose the generated file.

Confirm:

- `imported` equals the prepared row count.
- `import_errors` is empty.
- `Agent setup → Zabbix provisioning` moves from **Retrying/Pending** to
  **Synced** as the outbox drains.

Useful checks on UAT-01:

```bash
docker exec -i report-portal python3 - <<'PY'
from db import get_db
with get_db() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT status, COUNT(*) FROM atm_provision_state
         WHERE atm_id LIKE 'TST-%' GROUP BY status ORDER BY status
    """)
    for row in cur.fetchall():
        print(row)
    cur.close()
PY
```

```bash
docker exec -i report-portal python3 - <<'PY'
from verify import fleet
out = fleet()
print(out['counts'])
print('rows:', len(out['rows']))
PY
```

## 5. Cleanup

All test rows share the `TST-` prefix. In the portal, search `/atms?search=TST-`,
select the rows and use **Delete Selected** page by page. The bulk delete
disables the matching Zabbix hosts and removes the portal rows.

Do **not** delete by vendor or district: those groups also contain real ATMs.
