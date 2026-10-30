# GLPI API Service Accounts

Same surgery as Zabbix (docs/zabbix-api-users.md): one shared `glpi`
login feeds the portal, the Zabbix webhook, and the reconcile scripts —
one stale password breaks tickets everywhere at once. Three isolated
accounts from now on:

## Mapping (usernames only — passwords in `.env` + sealed backup)

| Account | Consumers | Credential source |
|---|---|---|
| `glpi-portal` | report-portal Incident Management (tickets, followups, assign, solve) | `.env` → `GLPI_USER` / `GLPI_API_PASSWORD` |
| `glpi-webhook` | Zabbix "GLPI Ticket" media type (auto-create, recovery followups, Pending move) | **baked into the Zabbix media type** (see gotcha) |
| `glpi-scripts` | reconcile + operator scripts | shell env → `GLPI_USER` / `GLPI_API_PASSWORD` |
| `glpi` (old shared) | nothing after cutover — disable, don't delete (audit history) | sealed, then disabled |

All three get the **same profile/rights as today's `glpi` user**
(check Administration → Users → `glpi` → profile first, mirror it —
rights surgery is phase 2, not smuggled in here).

The app token (`GLPI_APP_TOKEN`) belongs to the GLPI **API client**,
not to users — it stays shared for now. Regenerating it breaks all
three consumers at once; per-client tokens are phase 2.

## THE gotcha (read before rotating anything)

The webhook does **not** read `.env`. Its credentials are baked into
the Zabbix media type's message templates at import time
(`config/zabbix/zbx_export_mediatypes.xml`: `glpi_user`,
`glpi_password`, `app_token` substituted by
`scripts/import_zabbix_mediatypes.py`). Consequences:

- Rotating the GLPI password in `.env` alone leaves ticket
  auto-creation on the OLD password (silently failing).
- Changing the `glpi-webhook` password in GLPI alone breaks
  auto-creation until the media type is updated.
- Either change requires the other half. Always do both, back-to-back.

## Cutover (staged, rollback at every step)

1. **Create:** GLPI → Administration → Users → Add `glpi-portal`,
   `glpi-webhook`, `glpi-scripts` (same profile as `glpi`; active;
   password never expires if that policy exists). Store passwords
   sealed immediately.
2. **Portal:** `.env` → `GLPI_USER=glpi-portal`,
   `GLPI_API_PASSWORD=<its password>`,
   `docker compose up -d --force-recreate report-portal`.
   Verify: Incident Management list loads, detail opens, kebab shows
   the Assignee column. Rollback: old values + recreate.
3. **Webhook (surgical UI edit, no re-import):** Zabbix →
   Administration → Media types → `GLPI Ticket` → Message templates →
   PROBLEM/RECOVERY templates → set `glpi_user` = `glpi-webhook`,
   `glpi_password` = its password → Update. (Re-import would also work
   but risks duplicating the media type; the UI edit touches only the
   two parameters.) Verify: Media types → `GLPI Ticket` → Test
   (send to the test host) or wait for the next real alert; confirm
   the ticket appears. Rollback: put the old values back.
4. **Scripts:** shell env `GLPI_USER=glpi-scripts` +
   `GLPI_API_PASSWORD=<its password>` when running reconcile/operator
   scripts. Verify: reconcile completes without auth errors.
5. **Retire the shared account:** set `glpi` to a sealed random
   password, then **disable** it (never delete — tickets, followups
   and audit rows point at it). Confirm the audit log shows the three
   service names succeeding and no `glpi` activity afterward.

## Rotating one account (the new normal)

Same rule as Zabbix: change it in GLPI **and** its single consumer,
back-to-back. Webhook reminder: its half lives in Zabbix, not `.env`.

## History

- 2026-10: created alongside the Zabbix split. Isolation first,
  per-client app tokens + least-privilege profiles later.
