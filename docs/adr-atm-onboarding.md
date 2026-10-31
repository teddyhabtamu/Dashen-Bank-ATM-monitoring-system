# ADR: ATM onboarding — durable provisioning and automatic identity

> **Status:** Accepted, 2026-10-31. Provisioning, `atm.identity` transport and
> the fleet onboarding state are implemented on `uat-prep`.
> **Scope:** Zabbix host creation, `atm.identity` transport, and the per-ATM
> rollup of provisioning / reporting / identity / EJ / images. EJ auto-enable
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

### 2. Identity rides a passive Zabbix item on one shared template

A new item on the shared **Dashen Bank ATM Agent** template. It is not on the
vendor templates: the first implementation put it on both, and Zabbix rejected
the GRG import as soon as the UAT host linked both ("Cannot inherit item with
key atm.identity ... already inherited"). One template owns the common agent
contract; vendor templates own vendor telemetry.

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

### 4. The fleet page answers five stages, not one

`onboarding_state.py` turns the evidence into one verdict per ATM:

| Stage | Ready when |
|---|---|
| Provisioning | the Zabbix host exists and the dry-run plan has no pending change |
| Reporting | the vendor's authoritative telemetry is fresh |
| Identity | `atm.identity` is under 24h old and has no high/medium conflict |
| EJ | an EJ hostname is set and the latest parsed journal is from today or yesterday |
| Images | every reported root is approved **and** the box’s applied profile hash matches the current profile |

The overall status is the worst stage, and the **next action** is the first
stage that is not ready in that order. `unknown` means the sweep could not
evaluate a stage; it never sends a technician to a box. A reporting ATM with
no identity, journal or approved image root is therefore *not* green. This is
the rule that keeps "healthy" honest during a 1,202-ATM rollout.

## Consequences

- A saved ATM can no longer silently lack a Zabbix host.
- The fleet page shows provisioning, reporting, identity, EJ and images per
  ATM, with one next action and a filter for each stage.
- The Agent setup page shows **Synced / Retrying / Needs registry change** and
  a **Retry provisioning now** button.
- Identity appears without a paste; the Agent page shows its source and age.
- An empty EJ hostname is seeded from the journal filename, so the existing
  hourly puller starts collecting without a second portal edit.
- The portal now has a scheduler job that reads Zabbix every five minutes. It
  is one `host.get` + one `item.get` per 250 ATMs, not per ATM.
- `atm_provision_state` is in `config/portal_tables.txt`, the backup/split
  list, and the Stage-3 probe.
- A one-time migration is required after upgrade:
  `python3 scripts/import_agent_template_api.py --link-all`. It creates the
  shared template, removes the legacy item from the WMI and GRG XFS templates,
  and links the shared template to ATM-group hosts that already have an agent
  interface. `zabbix_sync` repairs hosts that do not.

## Next

1. ~~**Onboarding state machine.**~~ Done: `onboarding_state.py` combines the
   five stages into one status and one next action on the fleet page.
2. **EJ completion.** The empty-hostname case is seeded automatically. The
   fleet stage now shows pull/ingest status per ATM. Still needed: a review
   action for a hostname that conflicts with an existing registry value. EJ is
   server-side: once the registry is valid, the existing puller needs no ATM
   revisit.
3. ~~**Images profile freshness.**~~ Done: the identity probe reports the
   applied `profile_hash` from `atm-agent-state.json`, and the Images stage
   stays **Reapply needed** until the box runs the current profile. Approval
   changes the profile, so approve first, then reapply once. Still check the
   bridge/sample read after reapply. Do not build a bespoke push channel yet.
4. **Policy refresh channel.** Only if reapply friction proves real. It needs
   client authentication, TLS, code signing and audit; it is a larger security
   surface than the current manual reapply.
