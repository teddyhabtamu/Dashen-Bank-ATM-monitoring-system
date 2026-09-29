# ATM Monitoring System — Deep Review
**Date:** 2026-09-29 · **Branch:** `uat-prep` · **Reviewer role:** senior engineer + end user + tester
**Method:** three parallel evidence dives (portal UX/security, backend/infra, UAT readiness), every
Critical/High below re-verified against the code before publishing. No secrets in this file.

Severity: **Critical** (data loss / security / go-live blocker) → **High** → **Medium** (backlog).
Effort: `S` (<2h) · `M` (half–2 days) · `L` (project-level).

---

## 1. Fix first (ordered)

### C1. SNMP community readable + exportable by any viewer — High/Critical · `S`
`report-portal/blueprints/admin.py:505` (`atm_detail` allows `ROLE_VIEWER`) renders
`{{ atm.snmp_community }}` in plaintext (`templates/admin_atm_detail.html:170`); the same secret
leaks via `atm_get` (`SELECT *` → JSON, viewer-allowed) and `atm_csv_export` (`SELECT *` → CSV).
Any viewer can bulk-export every ATM's community string.
**Fix:** strip `snmp_community` (and `ip_address`) from viewer responses; require operator+ for
detail/export, or mask the community in UI and drop the column from CSV. Then rotate communities
that were visible during UAT.

### C2. Stored-XSS / JS-breakage via `atm_id` — High · `S`
Server `_validate` checks only `len >= 3` (`blueprints/admin.py:1107`); the client assumes
`^[A-Za-z0-9][A-Za-z0-9_-]*$`. A crafted `atm_id` with a quote breaks out of
`onclick="toggleRowDetail('{{ d.atm_id }}')"` / `editATM('{{ d.atm_id }}')`
(`templates/admin_atm.html:240,271`) — import path bypasses client validation entirely.
**Fix:** enforce the charset server-side in `_validate` (reject on mismatch), and prefer
`data-` attributes over string interpolation in `onclick`.

### C3. `javascript:` URLs pass the ticket sanitizer — High · `S`
`_clean_html` allowlists `<a>` but never checks the scheme (`blueprints/incidents.py`,
`handle_starttag`), and output renders `|safe`. A GLPI-side `javascript:` link executes in the
portal. (My own code — should have been caught at build time.)
**Fix:** allow only `http/https/mailto` hrefs; drop the attribute otherwise.

### C4. Simulators ignore `ENABLE_SIMULATORS=false` — Critical · `M`
The flag only gates portal **reads** (`blueprints/admin.py:38,81`). `simulators/txn_engine.py`,
`ej_engine.py`, `sim_engine.py` loop `common.refresh()` unconditionally — nothing in
`simulators/` reads the flag. If sim containers run in UAT they write `SIMULATOR` rows into the
same `atm_transactions` the anomaly detector reads **without a `source` filter**, plus EJ log
files Filebeat ships to the live index. Fake data pollutes real alerts silently.
**Fix:** make each engine exit(0) when the flag is false (one `sys.exit` each), AND add
`source != 'SIMULATOR'` guards to detector/report queries as defense in depth. Verify with
`docker ps` that no sim containers run on UAT-01 today.

### C5. Nightly retention SQL is broken (silently) — High · `S`
`scheduler.py:394,401` builds `INTERVAL '%s days'` with a bound parameter → psycopg2 renders
`INTERVAL ''365' days'` → fails every night, caught as log-only. Retention has likely never run.
**Fix:** `INTERVAL '1 day' * %s`; add a startup self-test log line. Confirm with Risk/Compliance
that 365/365/180 placeholders are acceptable before it starts deleting for real.

### C6. `$ATM_VENDOR` is never provisioned — vendor isolation can't fire — High · `S`
Templates declare it, the webhook consumes it (`vendor || 'Unknown'`), but neither
`scripts/sync_atms_to_zabbix.py:create_host` nor `report-portal/zabbix_sync.py` passes `macros`.
Every auto-ticket arrives with vendor `Unknown`, so the new vendor-override branch never triggers
and hardware tickets keep falling to the shared group.
**Fix:** pass `macros: [{$ATM_VENDOR: <vendor>}]` in both creators (one line each). Then re-test
one NCR + one GRG alert end-to-end.

### C7. GLPI down = alerts lost, no queue — High · `M`
Webhook `initSession` throws on GLPI outage → Zabbix media step fails, action has no retry, and
the recovery→Pending path depends on the `glpi_id` tag that only exists if creation succeeded.
Problems stay visible in Monitoring Center but no ticket ever exists, with nothing paging anyone.
**Fix (short term):** daily reconciler — Zabbix `problem.get` vs GLPI ticket search by event ID,
create missing tickets. **(Long term):** outbox table + retry worker. Also check HTTP status in
`glpi_post/glpi_put` instead of only null-ticket-id.

### C8. Scheduled-report failures look successful — High · `S`
`_send_email` returns `False` on SMTP error and `_generate_report` returns Nones on failure, but
`last_run/next_run` update regardless (`scheduler.py:322-329`) — no retry, no dead-letter, no admin
notice. A dead relay looks like a healthy schedule.
**Fix:** only advance `next_run` on success; on failure keep `last_run` stale + write an
`SCHEDULED_REPORT_FAILED` audit entry the portal surfaces (banner on Schedule Reports page).

### C9. SMTP can hang the whole scheduler — High · `S`
`smtplib.SMTP(HOST, PORT)` with no timeout (`scheduler.py:139`); the 30s poller is single-threaded,
so one hung relay blocks all schedules + retention. No port-465/SSL option either (relevant for
the Outlook cutover if they mandate implicit TLS).
**Fix:** `SMTP(..., timeout=15)`, move sends off the poller thread, add SSL-option env.

### C10. Backup omits tables; restore is destructive — High · `M`
`scripts/backup_db.sh:28-40` skips `cash_replenishments` (explicitly "never auto-delete"
financial evidence), `ticket_seen`, `atm_state_history` (availability-report source),
`fault_type_map`. MariaDB (GLPI tickets/SLAs/groups), Grafana volume, OpenSearch, `.env` are
entirely uncovered, and backups live on the dying VM (`config/postgres/`). `restore_db.sh`
`DROP … CASCADE`s without a pre-restore dump.
**Fix:** extend table list; add `mysqldump glpi`; `scp` off-host on a cron; guard restore with
a timestamped pre-dump. Rehearse once — RTO is currently unproven (handover rows still TBD).

### C11. Portal ATM delete is irreversible (operator-allowed) — High · `M`
`atm_delete`/`atm_bulk_delete` hard-delete 7 tables; `cash_replenishments` goes with the ATM
despite the never-delete policy; single delete needs only OPERATOR. No soft-delete, no archive.
**Fix:** deleted-flag + 30-day purge (keeps dashboards consistent), or at minimum move single
delete to ADMIN and dump the ATM's rows to an archive table first.

### C12. EJ search: viewer-bulk PII + PII in audit log — High · `M`
5000-row CSV export with card/auth/operator/message fields is viewer-allowed
(`blueprints/ej_search.py:320`), raw `message` may carry full PAN, and `EJ_SEARCH`/`EJ_EXPORT_CSV`
audit entries log the card number in plaintext retained 365 days.
**Fix:** mask PAN in export (first6/last4 max), move export to operator+, stop logging card
numbers (log hashed prefix or row counts only).

---

## 2. Backlog by area (Medium unless noted)

**Auth/session.** Empty `ADMIN_PASS` locks out *everyone* incl. valid users (`auth.py:255`); restart
reverts manual admin password/role edits (`auth.py:95-102`) — document or remove the seed-update.
Rate limit is in-memory per-IP, never evicted, no `ProxyFix` (High under proxy/tunnel use).
`PERMANENT_SESSION_LIFETIME=4h` is dead (never sets `session.permanent`); no `Secure` cookie flag.
`X-Internal-Key` bypasses *all* RBAC on *every* endpoint — scope it to `/report/*`.
LDAP login attribute/group DN uninterpolated-escape (`auth.py:138,150`) — escape `*()\\0`.

**ATM Registration.** Import preview `warnRow()` writes undefined `errs` → ReferenceError kills vendor
warnings (`admin_atm.html:1039`). No server-side import size cap (UI claims 10k). Single-save modal
*requires* lat/lng/install-date while server/import treat them optional (parity). `+ Add new
vendor` is always server-rejected (NCR/GRG only). Import never seeds `atm_current_state`
(single-save does). Manual sync silently caps at 200 (`MAX_BATCH`, toast says `checked=200`).

**Operations/KPI.** `cash_today` uses DB date (UTC) vs EAT UI — 3h skew at midnight. Health (txn rate)
and `atm_current_state` can disagree with no legend. KPI fault join lacks vendor
(`kpi.py:60,83`, `admin.py:572`) — shared codes like `CASH_JAM` double-count. KPI ranks deleted
ATMs (no locations join). Fleet/feed/summary render unbounded DOM — paginate before 1202 ATMs.

**Monitoring Center.** Problem `limit:100` + 60s cache: filtered counts mislead past 100 problems.
Sequential Zabbix calls can hold a worker ~24s. Hosts unreachable → silent zeros. Acknowledge is
Zabbix-only (parity gap with Incidents, by design — keep, but say so in the subtitle).

**Incidents (portal).** Board caps at newest 99 (`range 0-99`) yet counts/tabs present as totals.
Detail page is N+1 API calls (10+, 12s timeouts each) — add a 60s detail cache like the board.
Attachment proxy loads whole file into RAM; `Content-Disposition` filename unquoted (header
injection via GLPI filename). `mine` heuristic (`'(Report Portal)'` substring) misclassifies on
parody text — use author-id match instead. `ticket_seen` read fetches all rows, not the page's.

**Schedules/Users/Cash/Audit.** Unvalidated cron/recipients/params fail silently (never runs, UI
says success). Password min-length 4, no complexity; last-admin demote/delete unguarded; login
failures unaudited; cash double-submit creates duplicates with no reversal flow; `int(page)`
500s in three places on malformed input.

**Compose/ops.** `./report-portal:/app:rw` live-mount + unpinned `pip install` + floating tags
(`postgres:15`, `zabbix:*-latest`, `glpi:latest`) = prod runs unreproducible disk state. Zero
healthchecks/log-rotation/resource limits; everything binds `0.0.0.0` (incl. postgres 5432,
filebeat 514); `zabbix-agent2` is `privileged` + host-networked. `FLASK_SECRET_KEY` fallback
invalidates sessions per restart; dev secret ships in `.env`.

**ISO8583 gateway (cutover).** Single `db_conn` shared across accept-threads (`iso8583_gateway.py:397`)
— psycopg2 isn't thread-safe; unauthenticated `0.0.0.0:9876`; no STAN dedup (retransmits double-count
into velocity alerts); parser assumes ASCII/64-bit bitmap/fixed TID length (drops variants);
unknown TIDs stored as `branch=UNKNOWN` junk rows.

**Docs drift (fix while touching docs anyway).** Grafana `:3002` vs real `:3000` (architecture +
REAL_ATM_INTEGRATION_GUIDE); pgAdmin `:5050` documented but service removed; sim_port range
`1161-1260` (docs) vs `1161-2500` (code) vs `:161` (prod reality); Zabbix-agent "not containerized"
(false on UAT); Production v2 §5 builds simulators for a no-sim UAT; UAT guide's own snmpwalk
gate (`:1161`) can't pass on compliant UAT; `filebeat.yml` hardcodes UAT-02 IP; `--pilot` flag
referenced but unimplemented; troubleshooting.md is PoC/WSL-only.

**Tests.** No CI (no workflows), directory/EJ-parser tests skip-green without services, and zero
coverage for sync, excel-import, backup/restore, webhook, auth RBAC, import validation. Add one
workflow running the dependency-free tests + a skipped-if-no-lab policy so green means something.

---

## 3. Strategic (pre-1200-ATM + production)

1. **SNMP wall first.** Everything above is polish while `10.41.30.114` times out (cloud ticket open
   since Sep 22). Escalate with the dated test log, not more code.
2. **1200-ATM math doesn't fit the docs.** sim_port pool (1340) holds 1202 with 138 spare and dies
   toward 5000 despite the guide's "headroom" claim; Zabbix sync is ~100 hosts/min serial (~15–25 min,
   no resume file); `import_atms_from_excel.py` drops snmp/geo columns, never updates existing rows,
   and provides no geocoding although the checklist demands GPS per ATM.
3. **Secrets posture.** `.env` is gitignored (good) but world-readable `644` on a shared jump host,
   with a history of surprise edits (passwords + stray pasted lines). `chmod 600`, sealed off-host
   copy, rotation runbook (PG password rotation currently breaks the stack — first-boot-only volume).
4. **LDAP is correctly parked, not ready.** Missing: CA mount in compose, Grafana `ldap.toml` bind
   values, Zabbix directory re-point, GLPI per-user entity/profile import, OpenSearch security
   (explicitly a production gate — flipping early breaks EJ). Keep `AUTH_MODE=local` until the IAM
   checklist (`AD_Cutover_Runbook §7-16`) is fully TBD-cleared.
5. **Vendor model gap (portal side).** GLPI now isolates NCR/GRG, but portal roles are global —
   an NCR login with viewer role still sees the GRG fleet. If contracts require it, add a vendor
   predicate to fleet/incident queries (LDAP group → vendor attribute is the clean key).
6. **Observability of the monitor.** Nothing watches the watchers: no healthchecks, no dead-man's
   switch, failures are log-only per page. Minimum: scrape `/health` + container-alive into Zabbix
   with one paging trigger before go-live.

---

## Appendix A — already fixed this session (do not re-log)
Import `''→NULL` + per-row savepoints; Zabbix auto-sync/re-enable/error surfacing; import template
route + full 17-col preview/upload/`atm_name` parsing; save-redirect filter preservation; sidebar
transient expand; email CID logo + public/internal URL split; floating kebab menus; ticket
conversation view (dedupe, chat bubbles, reply + attachments + previews, unread badges);
per-vendor GLPI groups + webhook isolation; offline→hybrid fleet map with clustering.

## Appendix B — verified healthy (breadth proof)
All portal routes carry `@login_required + @role_required` (only login/logout/health exempt);
all SQL parameterized (no injection found); no shell execution paths; secrets never logged;
`role_required` correctly 403s viewers on operator/admin POSTs (spot-checked ack/assign/solve,
user admin, bulk delete); BRD matrix is unusually honest about 🟡/❌.
