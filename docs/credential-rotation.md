# Credential Rotation Runbook

Secrets were committed to this repository before the current `.gitignore`
rules existed. Rotating them is the only real fix: anyone with a clone can
read them. This document lists what was exposed, in the order it must be
rotated, with a verification step and a rollback for each.

Owner: IT + the on-call engineer.
Environment: UAT-01 (`dbtcatmmonappuat`) first, then production.

---

## 1. What was exposed

`.env` itself was **never** tracked (`git rev-list --all -- .env` is empty).
Two other things were:

| Source | Commit that stopped it | What it contained |
|---|---|---|
| `.env.uat` (tracked file) | `6d40bfe` "`.env.uat` was tracked and held real UAT credentials" | ~35 real keys: `ADMIN_PASS`, `FLASK_SECRET_KEY`, `DB_PASS`, `POSTGRES_PASSWORD`, `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `GLPI_API_PASSWORD`, `GLPI_APP_TOKEN`, `SMTP_PASS`, `GRAFANA_ADMIN_PASSWORD`, `ZBX_PASS`, `ZABBIX_PASS`, `ATM_SNMP_COMMUNITY`, `AT_API_KEY` |
| `glpi_setup.py`, `config/zabbix/zbx_export_mediatypes.xml` | `711348b` "remove all hardcoded secrets from repo" | the GLPI **App token**, hardcoded |

Treat every value above as public. Do not try to work out which ones are
"still fine" — rotate them.

---

## 2. Rules for every step

1. **Back up `.env` sealed before you start** (`umask 077; cp .env .env.bak-$(date +%F)`), and keep it off the repo. `.env*` is already gitignored — confirm with `git check-ignore .env`.
2. **`docker compose restart` does NOT apply `.env` changes.** The container keeps its old environment. Use:
   `docker compose up -d --force-recreate <service>`
3. Rotate **one secret at a time**, verify, then move on. A batch rotation that breaks something tells you nothing about which change did it.
4. Rollback everywhere = put the old value back + `up -d --force-recreate <service>`.
5. Never paste a secret into a ticket, chat, or commit. Use the sealed store.

---

## 3. Priority 1 — rotate first

### 3.1 `FLASK_SECRET_KEY` — the highest risk

It signs the Flask session cookie, and that cookie carries `session['role']`
(`blueprints/auth.py`). Anyone holding the leaked key can forge a cookie with
`role=admin` and **log in without a password**.

```bash
# [UAT-01]
python3 -c 'import secrets; print(secrets.token_hex(32))'   # new value
# put it in .env as FLASK_SECRET_KEY=...
docker compose up -d --force-recreate report-portal
```

Verify: every existing session is now invalid (everyone is logged out — this
is expected and is the proof it took effect). Log in again, open a page that
requires a role, confirm it works.

Rollback: restore the old value and recreate.

### 3.2 Admin password — change it in the portal, not in `.env`

`ADMIN_PASS` only seeds the account when it does not exist yet
(`init_admin_user()`); it **never overwrites an existing password**. Changing
the env var alone gives a false sense of safety.

- Change the password through **User Management** (or `docs/password-reset-sop.md` for a lockout).
- Then set `ADMIN_PASS` in `.env` to the same new value, so a fresh database is not seeded with the old one.
- Verify: log out, log in with the new password; the old one must fail.

### 3.3 GLPI App token

The token belongs to the GLPI **API client**, not to a user, and it is shared
by the portal, the Zabbix webhook, and the reconcile scripts.

1. Regenerate the App token in GLPI (the API configuration screen — confirm
   the exact menu path on your GLPI version).
2. `.env` → `GLPI_APP_TOKEN=<new>`.
3. **Also update the Zabbix media type.** Its credentials are baked into
   `config/zabbix/zbx_export_mediatypes.xml` at import time and the webhook
   does **not** read `.env` (see `docs/glpi-api-users.md`, "THE gotcha").
   Surgical UI edit: Zabbix → Administration → Media types → `GLPI Ticket` →
   message templates → replace the `app_token` value → Update.
4. `docker compose up -d --force-recreate report-portal`
5. Verify both halves:
   - Portal → Incident Management loads and a ticket detail opens.
   - Raise a test alert in Zabbix and confirm a ticket appears.

Doing only step 2 leaves auto-created tickets failing silently. Doing only
step 3 breaks the portal. They must move together.

### 3.4 GLPI account passwords

Rotate `glpi-portal` (portal), `glpi-webhook` (Zabbix media type), and
`glpi-scripts` (operator scripts). Same paired-update rule as 3.3: the
webhook password lives in the media type, not in `.env`.

Verify: portal ticket operations work; one test alert creates a ticket.

### 3.5 Grafana admin password

Change it in the Grafana UI, then set `GRAFANA_ADMIN_PASSWORD` in `.env` to
match (the env value is what a fresh container would use).

Verify: log out and back in; dashboards still load.

### 3.6 `SMTP_PASS`

Rotate the provider (bank relay or Gmail app password), update `.env`,
recreate `report-portal` and the scheduler.

Verify: run the daily outage report on demand and confirm delivery.

---

## 4. Priority 2 — database passwords (maintenance window)

`POSTGRES_PASSWORD` / `DB_PASS` and `MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD`
were all in the leaked file, and every service uses them. Do this in a window
because it requires recreating everything.

1. Change the password in the database first:
   - PostgreSQL: `ALTER ROLE <user> WITH PASSWORD '<new>';`
   - MariaDB: `ALTER USER '<user>'@'%' IDENTIFIED BY '<new>'; FLUSH PRIVILEGES;`
2. Update `.env`.
3. Recreate every consumer: `docker compose up -d --force-recreate`.
4. Verify: portal loads data, Zabbix is up, scheduled reports run, backups run.

Rollback: set the old password back in the database and in `.env`.

Do **not** rotate `MYSQL_ROOT_PASSWORD` and `MYSQL_PASSWORD` blindly at the
same time as a Zabbix upgrade; keep the window clean.

---

## 5. Priority 3 — operational, lower value

- **`ATM_SNMP_COMMUNITY`** — changing it means editing the community on every
  ATM *and* on every Zabbix host interface. Plan it with the network team; do
  not attempt it during a monitoring outage.
- **`AT_API_KEY`** — currently an empty placeholder (SMS is not built). When
  SMS is implemented, generate a fresh key; never reuse the leaked one.

---

## 6. After rotation — verification checklist

- [ ] Portal login works with the new admin password; the old one fails
- [ ] An old session cookie is rejected (secret rotation took effect)
- [ ] Incident Management loads and a ticket detail opens (GLPI read path)
- [ ] A test Zabbix alert creates a GLPI ticket (webhook path)
- [ ] Dashboard loads (Grafana)
- [ ] Daily report is delivered by email (SMTP)
- [ ] Backups still run (DB credentials)
- [ ] `git check-ignore .env` prints `.env` (it is still ignored)

---

## 7. What this does not fix

The old values stay in the git history and in every clone. After rotation they
are useless, so a history rewrite is **optional** and disruptive (every
developer must re-clone). It is tracked as A2 in `docs/remaining-work.md`;
decide it separately, after rotation is complete.

Rotation alone **is** sufficient to remove the risk.
