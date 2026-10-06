# Zabbix API Service Accounts

One shared `Admin` login used to feed five consumers — one stale password
locked out monitoring, reporting, backups and dashboards together
(Oct 2026 incident). Now each consumer has its own account: a failure is
a single-component warning, never a platform outage.

## Mapping (usernames only — passwords live in `.env` + sealed backup)

| Account | Consumers | Credential source |
|---|---|---|
| `svc-portal` | report-portal (sync, monitoring pages, reconcile) | `.env` → `ZABBIX_USER` / `ZABBIX_PASS` |
| `svc-grafana` | Grafana Zabbix datasource | `.env` → `GF_ZBX_USER` / `ZBX_PASS`* |
| `svc-scripts` | operator scripts, nightly config backups | shell env → `ZBX_USER` / `ZBX_PASS` |
| `Admin` | humans only (break-glass). No machine uses it. | sealed backup, offline |

\* Grafana shares the `ZBX_PASS` slot today (one fewer `.env` key to
rotate); give it its own `GF_ZBX_PASSWORD`-style split only if Grafana
ever needs an independent rotation cadence.

All three are members of `Zabbix administrators` with the `Admin role`
— same effective rights as before, so cutover cannot break anything by
permission. Least-privilege tightening is a separate phase-2 job.

## Cutover (staged, rollback at every step)

Performed once, in this order. Rollback anywhere = put the old value
back + `docker compose up -d --force-recreate <service>`.

1. **Create:** `ZBX_URL=... ZBX_USER=Admin ZBX_PASS=<current> python3
   scripts/setup_zabbix_api_users.py` (from UAT-01 or any host reaching
   the API). Store the three printed passwords in `.env` + sealed backup
   immediately — printed once, never stored by the script.
2. **Scripts first (lowest risk):** set `ZBX_USER=svc-scripts`,
   `ZBX_PASS=<its password>` in the shell (or `.env` + re-login) and run
   `./scripts/backup_configs.sh --dry-run` → expect `zabbix-web API
   reachable`. Real backup next; on any failure the old values still work.
3. **Portal:** set `ZABBIX_USER=svc-portal`, `ZABBIX_PASS=<its password>`
   in `.env`, `docker compose up -d --force-recreate report-portal`.
   Verify: Monitoring Center loads, one ATM save syncs (check Zabbix host
   `last seen`), no `Failed login` rows in Reports → Audit log.
4. **Grafana last (most visible):** set `GF_ZBX_USER=svc-grafana` in
   `.env` (password already correct via `ZBX_PASS` — or split it first),
   `docker compose up -d --force-recreate grafana`. Verify: datasource
   Save & Test + Active ATM Alerts panel fills.
5. **Seal Admin:** change the `Admin` password to a fresh break-glass
   value, store offline, update `.env`'s `ZBX_PASS` slot ONLY if anything
   still references it (nothing should after steps 2–4 — verify with
   `grep -rn 'ZBX_USER.*Admin'`, then leave the slot stale-but-harmless
   or repoint it; never delete blindly).

## Rotating one account (the new normal)

Example — Grafana's password leaks: change `svc-grafana` in Zabbix UI,
update that single `.env` value, recreate Grafana. Portal, scripts,
backups, dashboards-for-others: untouched, unbroken. This is the
property the shared login could never give you.

## History

- 2026-10: created after the rename/lockout cascade (shared `Admin`
  + stale containers → self-renewing account block). Isolation first;
  least-privilege later.
