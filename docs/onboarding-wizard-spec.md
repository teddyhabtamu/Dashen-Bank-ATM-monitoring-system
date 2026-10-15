# ATM Onboarding Wizard — Behavior Spec (for team review)
Status: SPEC (no code yet) · Date: 2026-10-14
Goal: trained non-technical staff onboard known-model ATMs solo; everything
else stays engineer-owned. Reuses: Registration form + CSV import,
`zabbix_sync` provisioner, `pull_ej_logs.sh` ATMS list, Latest-Data checks.

## Roles (RBAC additions)
- `onboarder`: May open the wizard, enter network facts, run pre-flight,
  complete verified onboardings. CANNOT see/type secrets, touch Zabbix UI,
  shell, other vendors' models, or edit templates. Sees only own audit trail.
- `admin` (existing): approves new-vendor/model additions, manages
  credential escrow, owns troubleshooting. Unchanged powers.
- Secrets rule: the wizard NEVER displays a community string, password, or
  key. Credentials resolve server-side from escrow by reference
  (`credential_ref`), selected from a dropdown of sealed entries.

## Screen 1 — Facts (all validated inline, nothing submits yet)
Fields: ATM ID (format `ATM-XXXX`, uniqueness checked live), name, branch,
district (closed dropdown — zone codes + names map shown), city, region,
GPS lat/lng (map picker, required: geo-map has no fallback), Terminal ID
(exact-match format check), vendor (closed: NCR/GRG), model (closed list
per vendor — anything else routes to Review Queue, see below), IP address
(IPv4 syntax + pingability hint text), monitored-toggle (default on).
Every field: inline error sentence on blur, e.g. "That IP doesn't answer
ping — check the address or ask the network team for the route." Never
error codes, never stack traces.

## Screen 2 — Pre-flight (automatic, one click, ~60 seconds)
Runs in order, stops at first red with the fix written beside it:
1. DB uniqueness (ATM ID / IP / TID not already registered).
2. Network reachability (ping where policy allows, else TCP port per
   vendor path: NCR 10050, GRG 161/UDP).
3. Credential check against escrow reference (NCR: agent key via Zabbix
   API test-auth; GRG: SNMP community via single snmpget — never echoed).
4. Model known? (vendor+model in template map; unknown → Review Queue,
   wizard ends gracefully with a ticket number, NOT an error).
5. Journal path preview (expected filename pattern for this host).
Each check: green check / red cross + one human sentence + owner
("you fix" vs "network team" vs "escalate to engineer").

## Screen 3 — Provision + verify + sign
On user confirm: create DB row → run provisioner (existing sync path,
dry-run first internally; aborts cleanly on any failure with the step
named) → poll Latest Data until values arrive (max ~3 intervals, live
progress bar, plain words: "Waiting for the machine's first answer…") →
EJ pull-list entry staged (DB-driven later; explicit list edit today,
shown for confirmation, not typed) → sign-off screen (summary card +
checkbox "I verified values are flowing" + name/date auto-stamped to
audit log).
Failure anywhere: state what completed, what didn't, and the exact next
human step. No partial silent success — the audit entry records
COMPLETE or ABANDONED-at-step-N.

## Review Queue (unknown models, failed pre-flights)
Separate admin-visible list: submitted facts + failed check + suggested
owner (engineer). Resolution actions: add model mapping (admin), fix
network/credential (requester), or reject with reason. Nothing auto-
provisions from this queue, ever.

## Guardrails (non-negotiable)
- Closed vocabularies everywhere (vendors, models, districts); free text
  only for names/branches.
- Unknown vendor/model/district: graceful stop + queue, never silent skip
  (today's `skipped` counter becomes a human sentence).
- No secrets on screen, in logs, or in audit entries (presence markers
  only — same standard as C12 PAN handling).
- Every onboarding writes ONE audit entry with outcome + actor + timestamp.
- Bulk CSV import gets the same pre-flight per row with a row-level
  pass/fail report (no partial silent imports).

## Metrics (acceptance for the build)
- Median time-to-onboard (target: under 30 min unassisted after training).
- First-try success rate (target: 8/10 with zero engineer calls).
- Escalations per 10 onboardings (target: ≤2, all to Review Queue).
- Trial gate: 3 supervised onboardings per operator before solo rights.

## Explicitly out of scope (stays engineer CLI)
Agent installation, firewall rules, first-of-model template work, EJ
pull-list automation (until DB-driven), all troubleshooting, all secret
lifecycles, proxy/LDAP/compose changes.
