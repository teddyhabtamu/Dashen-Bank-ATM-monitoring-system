# Portal Actions Review — what to add, what to refuse

Review date: 2026-10-20. Scope: write actions on the Operations portal against
Zabbix 6.4.21 and GLPI. Method: read the current blueprints/templates, then
checked every proposed call against the Zabbix 6.4 API reference and the GLPI
legacy REST API as used in `report-portal/blueprints/incidents.py`. Nothing
below is assumed — the API facts are cited.

## 1. What the portal can write today

| Page | Reads | Writes |
|---|---|---|
| Monitoring Center | Zabbix problems + hosts | acknowledge / un-acknowledge |
| Alert Management (`/anomalies`) | portal anomaly table | acknowledge / un-ack / bulk ack |
| Incidents (`/incidents`) | GLPI tickets, timeline, attachments | acknowledge (self-assign), note + up to 3 files, assign, solve |
| Operations | ATM state, KPI | none |
| ATM Registry | `atm_master`, Zabbix status | save, delete, bulk delete, CSV import, Zabbix sync, cash log |
| Schedules / Users | PostgreSQL | CRUD, send-now |
| Onboarding | fleet plan | save, provision, verify, stage |

The two operational surfaces — Monitoring Center and Incidents — are exactly the
two where the lifecycle stops early.

## 2. Why this is a real gap, not a wish list

- The BRD compliance matrix claims alert management includes **ack/suppress**
  (`docs/BRD_Compliance_Matrix.md:145`). Suppress does not exist in the portal.
- `docs/BRD-traceability.md:230` already names the risk: *"Alert storms. A
  flapping host can flood every channel. Maintenance windows, suppression and
  hysteresis must be configured before go-live."* Two of those three are absent.
- Incidents stop at **Solved**. GLPI's lifecycle continues to **Closed**, and
  Solved can be **Reopened**. Today the portal can do neither, so either someone
  logs into GLPI or solved tickets accumulate.
- There is no link from a Zabbix problem to the GLPI ticket the webhook created,
  even though the event carries a `glpi_id` tag
  (`config/zabbix/zbx_export_mediatypes.xml:320`). The operator sees the same
  fault twice and has to find the ticket by hand.

## 3. Zabbix actions

### 3.1 Suppress / unsuppress a problem — P0

`event.acknowledge`, action bitmask `32` (suppress), `64` (unsuppress), `4`
(add message). `suppress_until` is **required** with 32; `0` means indefinite
(Zabbix 6.4 `event.acknowledge` reference).

Suppression is per-event: it covers the problem already open. The operator
needs it for a known fault that will keep firing until someone physically
reaches the ATM.

```
action 38 = 32 suppress | 4 message | 2 acknowledge
{
  "eventids": ["20427"],
  "action": 38,
  "message": "Dispenser jam — vendor visit booked, ref VN-1042",
  "suppress_until": 1761000000
}
```

UI: duration preset (30 min / 2 h / 8 h / 24 h), reason mandatory, badge on the
row. Read state costs **no extra call**: the problem object already returns
`suppressed` (0/1), and `problem.get` with `selectSuppressionData` returns
`suppression_data` (`suppress_until`, `maintenanceid`).

Important distinction: if `suppression_data.maintenanceid` is set, the problem
is suppressed by a maintenance window, not by a person. The row must say
"In maintenance" and must not offer "Unsuppress" — the manual action would be
skipped by Zabbix.

### 3.2 Maintenance window for an ATM — P0

`maintenance.create`. This is a different tool from suppression: it is per-host,
prevents *future* problems from being displayed, and can be scheduled before
the work starts. Maintenance is what you want for a planned cash replenishment,
a technician visit, or a network change.

```
{
  "name": "ATM ATM-... — planned service",
  "active_since": 1760990000,
  "active_till": 1761000000,
  "hosts": [{"hostid": "10492"}],
  "timeperiods": [{"timeperiod_type": 0, "period": 7200}],
  "maintenance_type": 0
}
```

- 6.4 uses `hosts` (array of `{hostid}`); `hostids` is deprecated in this
  version. Same for `groups` vs `groupids`.
- `maintenance_type` 0 = **with data collection** (data still collected,
  problems hidden); 1 = without data collection. Default to 0: you want the
  history of the service visit.
- `tags` can scope which problems are hidden, but only with type 0. Start
  without tags — hiding the whole host is the intent.
- End early with `maintenance.delete`; list with `maintenance.get`.

Placement: ATM detail page ("Service mode") and the Monitoring Center row. The
portal already knows branch/terminal from `atm_master`, so branch-wide
maintenance is a later, cheap extension (use `groups`).

### 3.3 Bulk acknowledge / suppress — P1

`eventids` accepts an array, so bulk is the same call with more ids. This is
the alert-storm tool the BRD asks for: one network cut and 100 ATMs go
unreachable. Cap at 100 ids (the problem page already limits to 100) and
require one shared reason.

### 3.4 Show and link the GLPI ticket — P1 (read, not a write)

`problem.get` with `selectTags` exposes the `glpi_id` tag the webhook sets.
Render `GLPI #123` on the problem row linking to
`{GLPI_UI_URL}/front/ticket.form.php?id=123`. Costs one select flag, closes the
loop between the two screens, and makes the "which ticket is this?" question
disappear. Only present when creation succeeded — show nothing otherwise.

### 3.5 Enable / disable monitoring for a host — P2

`host.update` with `status: 1` (disabled) / `0` (enabled). The code already
exists as `zabbix_sync.disable_hosts()` (`report-portal/zabbix_sync.py:540`),
used by ATM delete. Expose it on the ATM detail page for ADMIN, labelled
"monitoring off", never as the first choice for temporary work — that is what
maintenance is for. Disabling keeps history but stops collection.

### 3.6 What **not** to add

- **Close problem** (`action 1`). The Zabbix reference is explicit: *"To close
  an event, manual close should be allowed in the trigger."* None of the 39
  trigger definitions in `config/zabbix/triggers.json` set `manual_close`, and
  none of the rendered templates contain it — the call would be refused for
  every ATM trigger. Even if enabled, closing a still-firing problem hides it
  until recovery. Acknowledge + suppress covers the false-positive case without
  losing the signal.
- **Change severity** (`action 8`). The trigger is the source of truth; editing
  one event's severity corrupts the KPI and report history.
- **Cause/symptom rank** (`128`/`256`). Only meaningful with cause-symptom
  correlation, which this deployment does not use.
- **Remote commands / Zabbix scripts.** Execution on 1,300 endpoints from a web
  button is a privilege-escalation surface with no operational need here.
- **Trigger/OID editing from the portal.** Configuration drift with no review
  path; belongs in the template repo.
- **Host delete.** Loses history; disable is sufficient and reversible.

## 4. GLPI actions

### 4.1 Close a ticket — P0

`PUT Ticket/{id}` with `{"input": {"status": 6}}`. Guard: only from Solved (5),
confirmation, audit. This is the missing end of the lifecycle.

Operational nuance: do not make Close the automatic next step after Solve. The
requester needs a window to say "it is still broken". Either GLPI auto-closes
solved tickets after N days, or the NOC lead closes them. **Decision needed:
is GLPI auto-close enabled?** If it is, this action is nice-to-have; if it is
not, solved tickets never leave the board.

### 4.2 Reopen a ticket — P0

`PUT Ticket/{id}` with `{"input": {"status": 2}}` plus a followup carrying the
reason (the current `_add_followup` path). Allowed from Solved/Closed. Without
this, a fault that returns after a solve forces a new ticket — splitting the
history and inflating the count.

### 4.3 Move to Pending — P0

`PUT Ticket/{id}` with `{"input": {"status": 4}}` plus a reason followup:
"waiting for vendor", "waiting for cash", "waiting for network". GLPI has no
structured pending-reason field on the ticket, so the followup is the record.
The board already counts status 4 separately
(`report-portal/blueprints/incidents.py:596`), so this makes the existing
counts honest. Without it, waiting tickets masquerade as open work.

### 4.4 Create a ticket manually — P1

For faults reported by phone or by the branch, with no Zabbix problem:
`POST Ticket/` with `name`, `content`, `type`, `itilcategories_id`, `urgency`,
`impact`, `_users_id_requester`. The open question is linking: today the
webhook puts the ATM in the ticket title/tag. Decide whether manual tickets
should also carry the technical host name in a tag; KPIs by ATM depend on it.

If the webhook already creates a ticket for **every** problem (verify against
the Zabbix action config), manual creation is only for unmonitored faults and
is correctly P1, not P0.

### 4.5 Change priority / urgency — P2

`PUT Ticket/{id}` with `priority`. Useful, but GLPI does it well and the SLA
impact is a policy decision. Add only if the NOC actually re-prioritises from
the portal; otherwise it is a button nobody presses.

### 4.6 Already there, keep

Acknowledge self-assigns, note with attachments (3 × 10 MB, blocked
executables), reassignment releases the old technician, solve writes an
`ITILSolution`. Those are the right shapes.

## 5. Guardrails that apply to every new write

| Guardrail | Rule |
|---|---|
| RBAC | VIEWER read-only. OPERATOR: ack, suppress ≤ 24 h, maintenance ≤ 8 h, ticket close/reopen/pending. ADMIN: indefinite suppression, maintenance without data collection, host disable. |
| Reason | Required for suppress, maintenance, reopen and pending (min 5 chars). Close is a confirmation plus the actor recorded in the followup. |
| Audit | `log_action()` with action, target, reason and expiry — same pattern as `PROBLEM_ACKNOWLEDGE`. |
| Idempotency | Pre-check `suppressed` / ticket `status`; return "already suppressed" instead of surfacing a Zabbix error. |
| Cache | Monitoring: `_cache['ts'] = 0.0`. GLPI: `_invalidate_cache()` + `_invalidate_detail(id)` (already wired in `_mutate`). |
| CSRF | Global `CSRFProtect` already covers every POST; new forms inherit it. |
| Bulk | Cap at 100 ids, one reason, one audit row per batch plus per id. |
| Tests | Pure payload builders in `problem_actions.py` / a GLPI helper, plus static wiring tests — the venv has no Flask or psycopg2. |

## 6. Recommended order

| # | Item | Effort | Value |
|---|---|---|---|
| 1 | Suppress / unsuppress + bulk ack | 1.5 d | Closes the BRD gap; storm control |
| 2 | `glpi_id` link + suppressed/maintenance badges | 0.5 d | Removes duplicate hunting; nearly free |
| 3 | Maintenance window per ATM | 2 d | Planned work stops generating alarms |
| 4 | GLPI close + reopen | 1 d | Stops solved-ticket accumulation |
| 5 | GLPI pending + priority | 1 d | Board counts become honest |
| 6 | Manual ticket creation | 2–3 d | Phone/branch faults |

Items 1 and 2 are the immediate sprint: both are small, both reuse patterns
already proven by the acknowledge work, and together they turn the Monitoring
Center from a viewer into an operator console.

## 7. Decisions needed before building

1. Who may suppress, and what is the maximum duration? (Proposed: OPERATOR
   ≤ 24 h, ADMIN unlimited.)
2. Maintenance scope: single host first, or branch/group from day one?
   (Proposed: single host; `groups` is a small follow-up.)
3. Is GLPI auto-close enabled? This decides whether "Close" is P0 or P2.
4. Does the NOC use "Pending" today, and for what reasons?
5. Do all 68 alert rules create a GLPI ticket, or only a subset? This decides
   how urgent manual ticket creation is.
6. Confirm we never close a Zabbix problem from the portal (recommendation:
   yes, never — `manual_close` is off on every trigger and closing hides a live
   fault).
