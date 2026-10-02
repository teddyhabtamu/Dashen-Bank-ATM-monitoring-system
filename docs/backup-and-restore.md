# Backup & Restore Runbook

Two scripts, two cron jobs, one laptop copy. That is the entire backup
strategy — this page exists so nobody has to rediscover it after an OS crash.

## What is backed up

| Script | Covers | Output location |
|---|---|---|
| `scripts/backup_db.sh` | Postgres custom tables (`atm_custom_tables.sql` + CSVs), full Zabbix DB dump, GLPI DB dump | `config/postgres/` |
| `scripts/backup_configs.sh` | Zabbix hosts / 315 templates (one file each — bulk export 500s) / media types / trigger actions (raw JSON), Grafana datasources + dashboards, `.env`, compose files, `filebeat.yml`, `config/`, `glpi-plugins/` | `config/backups/configs_<date>.tar.gz` |

Trigger actions have no XML export: the bundle stores raw `actions.json`
and `scripts/setup_zabbix_actions.py` recreates them on restore.

## Schedule (UAT-01, user `Tewodros`)

```
0 2 * * * /data/atm-monitoring/scripts/backup_db.sh >> $HOME/atm-backup.log 2>&1
30 2 * * * /data/atm-monitoring/scripts/backup_configs.sh >> $HOME/atm-backup.log 2>&1
```

Notes:

- Log goes to `$HOME/atm-backup.log` because the user cannot write to
  `/var/log` (verified 2026-10-02 — redirecting there would silently
  prevent the jobs from running at all).
- Config bundles auto-prune to the latest 7. DB dumps accumulate —
  clean `config/postgres/` occasionally.
- Bundles are `chmod 600` and git-ignored (`config/backups/`). **They
  contain `.env` secrets — never commit, never email.**
- `BACKUP_DEST` (scp target) is intentionally unset: there is no second
  server. The laptop below is the off-site copy.
- Smoke tests (write nothing): `./scripts/backup_configs.sh --dry-run`.

## Off-site copy (laptop, Windows)

Weekly, or after any config change. In PowerShell, using `scp.exe`
(bare `scp` collides with a PowerShell alias):

```powershell
mkdir C:\ATM-Backups -Force
scp.exe -P 1006 Tewodros@196.189.126.63:/data/atm-monitoring/config/backups/configs_*.tar.gz C:\ATM-Backups\
scp.exe -P 1006 Tewodros@196.189.126.63:/data/atm-monitoring/config/postgres/glpi_*.sql.gz C:\ATM-Backups\
scp.exe -P 1006 Tewodros@196.189.126.63:/data/atm-monitoring/config/postgres/zabbix_full_*.sql.gz C:\ATM-Backups\
scp.exe -P 1006 Tewodros@196.189.126.63:/data/atm-monitoring/config/postgres/atm_custom_tables.sql C:\ATM-Backups\
```

Keep only the newest of each file. Keep the folder BitLocker-encrypted.

## Restore order (total loss, fresh RHEL9 + Docker)

1. `git clone <repo>` + copy sealed `.env` back (from laptop bundle `files/.env`).
2. `docker compose up -d postgres` (+ mariadb/glpi on their hosts).
3. `scripts/restore_db.sh --confirm` (Postgres custom tables) +
   `zcat glpi_*.sql.gz | docker exec -i glpi-db mysql glpi`.
4. Zabbix UI: Configuration → Import `zabbix/hosts.json`, then each
   `zabbix/templates/*.json` (small batches — bulk import may 500 like
   bulk export does) + `python3 scripts/setup_zabbix_actions.py`.
5. Grafana API: POST `grafana/datasources.json` + `dashboards/*.json`.
6. Copy back `filebeat.yml` (**`chown root:root`**, filebeat 7.x exits
   otherwise), compose files, `glpi-plugins/`.
7. Verify: portal login, Monitoring Center data, one GLPI ticket flow.

## History

- 2026-10-01: `backup_configs.sh` created (AE batch); dry-run probe fixed
  to POST (GET on `api_jsonrpc.php` returns 412); per-template exports
  after bulk-export HTTP 500 on ~300 stock templates.
- 2026-10-02: cron scheduled; log redirected to `$HOME`; first laptop pull.
