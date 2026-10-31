# ADR: ATM onboarding — durable provisioning and automatic identity

> **Status:** Accepted, 2026-10-31. First increment implemented on `uat-prep`.
> **Scope:** Zabbix host creation and `atm.identity` transport. EJ auto-enable
> and image reapply remain follow-ups (see "Next").

## Context

Two onboarding steps were still manual and fragile:

1. **Zabbix host creation.** Registration started a one-shot daemon thread.
   If Zabbix was briefly unavailable, the thread logged and vanished. The ATM
   row existed, the host did not, and nothing retried. The operator found out
   later, from `verify.py` or an empty Fleet health row.
2. **Box identity.** The installer proved `atm.identity` locally as SYSTEM, but
   the only way into the portal was a technician pasting the JSON into the
   Agent page. The probe was already installed and working; the transport was
   missing.

The constraint that shapes both fixes: the agent is **passive-only**. The
installer rejects `ServerActive`, and the fleet has no ATM-to-server channel.
Any solution must use the existing Zabbix server/proxy → agent TCP/10050 path.

## Decision

### 1. Provisioning is an outbox, not a background thread

`atm_provision_state` is a portal-owned table with one row per ATM:

- `pending` — queued, due now
- `syncing` — claimed by a worker
- `synced` — last write landed
- `retry` — transient failure, `next_attempt_at` set
- `blocked` — needs a registry change (no IP, unknown vendor, review)

`provision_atms()` remains the single writer. Every write path records its
outcome; the scheduler worker claims due rows with `FOR UPDATE SKIP LOCKED` and
retries with exponential backoff (1 minute → 1 hour). A stale `syncing` row is
returned to `retry` after 15 minutes, so a killed worker cannot park an ATM.

`blocked` is deliberately not retried. Retrying an ATM with no IP forever
would hide the actual fix, which is a registry edit.

### 2. Identity rides a passive Zabbix item

A new item on the NCR WMI template and the GRG XFS template:

```text
key:        atm.identity
type:       ZABBIX_AGENT
value_type: TEXT
delay:      1h
history:    1d
trends:     0
```

The portal reads the item's `lastvalue` through the Zabbix API every five
minutes (`identity_sync.sync_all`). It validates with `atm_identity.parse`,
stores in `atm_agent_identity` with `received_by='zabbix:auto'`, and shows
conflicts. The manual paste remains as a fallback.

Why not `ServerActive` / active checks: it would add an outbound firewall rule
and break the installer's passive-only contract. Why not a new ATM → portal
HTTP endpoint: it adds authentication, TLS and another network path for no
gain. The Zabbix server already polls TCP/10050.

Why `lastvalue` and not `history.get`: identity is evidence, not a time
series. One current value per ATM is enough. The item keeps one day of history
so a box offline longer than that correctly reports no current value.

`task.create` type 6 ("Execute now") powers the **Refresh from Zabbix** button.
It requires Super-admin; if the portal's API user lacks it, the button says so
and the hourly collection remains the fallback.

### 3. Evidence is still not authority, with one narrow exception

`identity_sync` writes the side table. It never overwrites a registry value.
The one exception is an **empty** `ej_hostname` whose identity source is
`ej-filename` (the `<HOST>` tag in the journal filename): it seeds that field
so the existing hourly puller can pick the ATM up without a second portal
visit. An existing value is never overwritten; a mismatch stays a conflict for
a human.

Terminal ID, vendor and model are never auto-healed. Image roots remain a
human approval. Those are control boundaries, not missing automation.

## Consequences

- A saved ATM can no longer silently lack a Zabbix host.
- The Agent setup page shows **Synced / Retrying / Needs registry change** and
  a **Retry provisioning now** button.
- Identity appears without a paste; the Agent page shows its source and age.
- An empty EJ hostname is seeded from the journal filename, so the existing
  hourly puller starts collecting without a second portal edit.
- The portal now has a scheduler job that reads Zabbix every five minutes. It
  is one `host.get` + one `item.get` per 250 ATMs, not per ATM.
- `atm_provision_state` is in `config/portal_tables.txt`, the backup/split
  list, and the Stage-3 probe.
- A one-time template re-import is required after upgrade so the item exists
  on existing hosts.

## Next

1. **Onboarding state machine.** Combine provisioning, reporting, identity,
   EJ and image readiness into one per-ATM status with one next action. The
   Agent page already has the pieces; the fleet page needs the rollup.
2. **EJ completion.** The empty-hostname case is seeded automatically. Still
   needed: pull/ingest status per ATM, and a review action for a hostname that
   conflicts with an existing registry value. EJ is server-side: once the
   registry is valid, the existing puller needs no ATM revisit.
3. **Images.** Approval changes the agent profile, so a reapply is required.
   Show "profile stale", verify probe hashes after reapply, and check the
   bridge/sample read. Do not build a bespoke push channel yet.
4. **Policy refresh channel.** Only if reapply friction proves real. It needs
   client authentication, TLS, code signing and audit; it is a larger security
   surface than the current manual reapply.
