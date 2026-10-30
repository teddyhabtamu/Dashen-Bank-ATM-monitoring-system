# OpenSearch Dashboards — UAT EJ visualizations

The EJ data is parsed by `config/opensearch/atm_ej_parser.json` into a **nested**
schema. Use the typed index pattern `atm-ej-live-*` (import
`uat_ej_index_pattern.ndjson` via `scripts/load_osd_dashboards.sh`) as the basis
for the UAT dashboards below.

## Structured fields available
| Field | Type | Notes |
|---|---|---|
| `event_time` / `@timestamp` | date | event time |
| `atm.id` | keyword | ATM id, e.g. `ATM-002` |
| `terminal.id` | keyword | terminal id, e.g. `TID002` |
| `event.category` | keyword | `TXN` / `FAULT` / `EVENT` |
| `transaction.type` | keyword | WITHDRAWAL / BALANCE_INQ / TRANSFER / MINI_STATEMENT … |
| `transaction.status` | keyword | APPROVED / DECLINED / TIMEOUT / ERROR (trimmed, no trailing space) |
| `transaction.amount` | float | 0 for non-cash txns |
| `transaction.currency` | keyword | ETB |
| `transaction.auth_code` | keyword | issuer auth reference (APPROVED only) |
| `card.masked` | keyword | `************XXXX` (last-4 only) |
| `vendor` | keyword | NCR / GRG |
| `fault.code` | keyword | e.g. `CU002`, `DI001` |
| `fault.desc` | keyword | human-readable fault text |

## Suggested UAT dashboards (build in OSD, then export here for reuse)
1. **Transaction volume by ATM** — Vertical bar, buckets: Terms on `atm.id`,
   metric: Count. Sub-split by `transaction.type`.
2. **Approval rate trend** — Date histogram on `@timestamp` (e.g. per hour),
   metric: Count; add a filter for `transaction.status = APPROVED` vs others,
   or a sibling pipeline. Quick stat: Terms on `transaction.status` (donut).
3. **Card activity** — Data table: `card.masked`, `atm.id`, `transaction.type`,
   `transaction.status`, `@timestamp` (sorted desc).
4. **Fault breakdown** — Vertical bar / table: Terms on `fault.code`
   (sub-split `fault.desc`), metric: Count, filtered to `event.category = FAULT`.
5. **Hourly heatmap** — Date histogram (`@timestamp`, per hour) × Terms
   (`atm.id`), metric: Count — to spot low-activity windows.

## How to load
```bash
OSD_HOST=<uat-osd-ip>:5601 ./scripts/load_osd_dashboards.sh
```
Only `uat_ej_index_pattern.ndjson` ships today; add the exported dashboard
`.ndjson` files from the OSD UI into this directory and re-run the script.
