# Remaining work — what blocks production, and what can wait

Written 2026-10-19, at `uat-prep` @ `f569a3f`, suite 419 passed / 8 skipped.
Revised the same day to include decisions recorded in ADRs and runbooks, which
the first version missed because it was drawn from conversation rather than from
the documentation set.

Two different kinds of work keep getting mixed together. One is code quality —
migration discipline, splitting a 1,978-line file, chasing a log file. Worth
doing, not urgent. The other is the set of things that actually stand between
this and production. Only three of those are load-bearing:

1. live credentials sitting in git history,
2. a monitoring portal that has three ATMs in it,
3. a scheduler that cannot ever run on more than one host.

But before any of those, there is a cheaper category: **questions that decide
what to build at all.** Section 0 is first because each item costs one email and
two of them can invalidate design work that has already been done.

---

## Where things stand

| Fact | Value |
|---|---|
| Branch / head | `uat-prep` @ `f569a3f` |
| Test suite | 419 passed, 8 skipped |
| Registered ATMs | **3** (2 NCR, 1 GRG), `In Service` 0 |
| Transactions in the portal | ~182k, of which the last day is real EJ + ISO8583 |
| Portal database | separate `portal` DB on UAT-01; six writers and the Grafana reader moved across |
| Zabbix database | `zabbix`, frozen at 181,265 transactions — still the rollback |
| UAT-01 / UAT-02 | `dbtcatmmonappuat` (172.22.10.45) / `dbtcatmmondbuat` (172.22.10.60) |

The database split is done and verified. It is not on this list except where it
touches something else.

---

## 0. Decisions waiting on an answer

Each of these is an external question, not a build. They rank first because the
answer can delete work rather than add it.

| # | Question | Who answers | What it decides |
|---|---|---|---|
| 0.1 | **Can the monitoring host reach every branch on TCP/10050?** | Network | `adr-ej-transport.md` calls this "before rollout — nobody has asked this". If some branches are unreachable, EJ pull dies there *and* WMI polling dies there. The fallback is per-site: Zabbix proxy or active agent for metrics, a file-POST receiver into the `filebeat` directory for EJ. Answer this before sizing anything. |
| 0.2 | **Does the ATM filename encode the transaction or sequence number?** (question 4 of 5) | ATM vendor | **GRG: no** — the 22 Oct filename said `CarNum-8888########8888` while the EJ said `NOCARD`. Time+event is the rule. **NCR entry format still pending.** See `adr-atm-images.md`. |
| 0.3 | Questions 1, 2, 3, 5: camera attached, capture enabled, write path, retention | ATM vendor | Attached, capturing and writing confirmed on the two test ATMs on 2026-10-22. Production paths and retention are still open. |
| 0.4 | **What are the retention periods for EJ evidence and for images?** | Risk / Compliance | Decides the Zabbix-vs-OpenSearch split (days vs years) and whether a purge must exist from day one. Current ISM policy is 90 days; the period is still TBD with Risk. |
| 0.5 | Lawful basis, consent signage, access audit for face images | Compliance | `adr-atm-images.md` states plainly that compliance gates engineering here, not the reverse. |

### A strategic decision that has not been taken

`docs/collection-strategy.md` is still labelled *"not yet committed to a single
path"*. That is honest and it matters, because its own comparison shows the
Zabbix-centric design gives up three things NetXMS provides natively:

| Capability | NetXMS (current) | Built stack |
|---|---|---|
| Data during a link outage | agent caches and replays | gap — nothing buffers |
| ATM behind branch NAT | tunnel mode, agent dials out | needs a reachable port per ATM |
| Screen capture | native | not possible over SNMP/HTTP |

0.1 is the same question wearing different clothes: if branches are NAT'd or
firewalled from the monitoring host, the polling model needs proxies and an
outbound channel. Worth settling as a programme decision before the production
plan is written, not as an implementation detail later.

---

## A. Live risk — do first

The only items whose cost goes *up* with time.

| # | Task | Who can move it |
|---|---|---|
| A1 | **Rotate every secret that was committed.** Not just the two first noticed: `.env.uat` was tracked until `6d40bfe` and carried ~35 real keys, including `FLASK_SECRET_KEY` (a forged session cookie is admin without a password), `ADMIN_PASS`, the DB passwords, `SMTP_PASS` and `GRAFANA_ADMIN_PASSWORD`; the GLPI App token was also hardcoded in `glpi_setup.py` and a tracked Zabbix XML until `711348b`. Untracking the file did not remove any of it — the values are in every clone. Read from the live containers, not from a local `.env`: the `svc-portal` / `svc-scripts` cutover **is** applied and authenticating. Ordered steps, verification and rollback: `docs/credential-rotation.md` | You |
| A2 | Decide on history rewriting. Rotating first makes this optional; if the repo is held by a small group, rotation alone is defensible | You |
| A3 | `report-portal/requirements.lock` exists only on UAT-01 and is untracked. Commit it for reproducible builds, or delete it so it stops looking like a lockfile | Portal work |

---

## B. Blocks the product from being real

| # | Task | Who can move it |
|---|---|---|
| B1 | **Bulk ATM inventory import.** The registry holds three devices. Every number on the dashboards — 5,078 transactions in 24h, 85.9% approval — describes three devices. Onboarding 1,300 ATMs through a one-at-a-time web form is not viable | Portal work; needs your inventory file |
| B2 | **Load test at realistic fleet size.** Nothing has been exercised above 3 ATMs / ~182k rows. The map, fleet pages, dashboard aggregates and CSV export all behave differently at 1,300 nodes and ~100M rows — and the Zabbix side faces ~90 WMI items × 1,300 hosts every 10–30s (4,000–12,000 collections/sec), which is a far larger question than the portal's. Depends on 0.1 | Portal work |
| B3 | Verify the demo window lapsed cleanly. The simulator policy was set to expire and the banner now reads `(0 simulated rows hidden)`, suggesting it did — confirm no dashboard silently emptied and the real-only view is what is on screen | Portal work |
| B4 | **Two of the three test ATMs are not collecting telemetry.** Fleet health reports it precisely: `ATM-TEST-GRG` has 1 of 25 items and no GRG telemetry (needs UDP/161 opened from the collector, or the SNMP community confirmed); `Test1` has 1 of 90 items and no NCR telemetry (needs a Zabbix agent reachable on TCP/10050 at 10.30.41.116 with `atm-wmi-collector.ps1` installed). Only `ATM-TEST-NCR` reports, at 59 of 60. Until these two are wired up, the portal's fleet view is one machine wide — and both remedies are network/desktop tasks, not portal work | You / field |

**B1 is the highest-value item here.** Everything else is engineering around a
fleet we cannot see. If you can supply ATM ID, branch, city, region, vendor,
model and IP in any tabular form, that is enough — it does not need to be clean,
and cleaning it is part of the job.

---

## C. Reliability ceilings

| # | Task | Who can move it |
|---|---|---|
| C1 | Swap the scheduler's `/tmp/portal-scheduler.lock` `fcntl.flock` for a Postgres advisory lock (`pg_try_advisory_lock`). Same singleton design, same "no Redis" property, but it works across hosts instead of within one container | Portal work |
| C2 | Decide production topology: Postgres on its own host, or colocated? Drives backup windows, HA and cutover. Coupled to 0.1 and B2 | You |
| C5 | **Fleet report scaling.** The provisioning dry run read `host.get` once per ATM (`zabbix_sync._build_plans`), so the fleet sweep cost 1,304 serial round-trips at 1,300 ATMs — on top of the verify sweep that had already been batched. Now batched (`_host_states`, 250 per call): 1,300 ATMs is 10 round-trips. The 200-ATM `MAX_BATCH` cap also truncated the read-only report silently; the fleet path now raises it to the fleet size, and the cap stays for writes. **Remaining:** the report is still computed synchronously-ish in the background with no progress indicator beyond a spinner, and `provision_atms` still loads every ATM row before planning — fine at 1,300, worth revisiting above ~10k | Done (measured); revisit at 10k+ |
| C3 | Alert on the portal itself. `/ready` exists and nothing polls it. If the portal is down, the first person to notice is whoever needed a report | Portal work |
| C4 | Reconcile UAT-02: 358 commits behind, gateway work parked on local branch `uat02-local` @ `ec5dc2f`. Decide whether it merges or the host is re-based | Portal work |

C1 is roughly ten lines and is the only remaining change that removes a hard
architectural ceiling rather than tidying up. It should be done whether or not
you ever run two replicas: while the lock is a local file, a rolling restart
either duplicates scheduled emails or misses a poll cycle, and there is no way to
have neither.

---

## D. External dependencies — not ours to unblock

Listed so they stop appearing as open tasks. None can be closed by writing code.

| # | Task | Blocked on | Recorded in |
|---|---|---|---|
| D1 | Switch / Core Banking integration — the simulator is not authoritative | Vendor access | — |
| D2 | EJ parser viability against busy NCR/GRG journals | Real journal samples | `adr-ej-transport.md` |
| D3 | Physical cassette balances — cash forecasting is book-based until these exist | Field access | — |
| D4 | GRG telemetry over UDP/161 | Network + vendor MIB | `atm-data-requirements.md` |
| D5 | Camera images | Live ATM deployment of the `atm.image.*` keys; NCR zip entry format; compliance/retention | `adr-atm-images.md`, `atm-image-on-demand.md` |
| D6 | SMTP and Africa's Talking credentials | You | `BRD_Compliance_Matrix.md` |

D2 deserves the emphasis: it is the one dependency where a negative answer
changes the design rather than merely delaying a feature. If the parser cannot
keep up with a busy journal, ingestion needs rethinking, not tuning.

---

## E. Hygiene — do during slack

- **E1 · Migration discipline.** Numbered SQL files and a `schema_migrations`
  table. Not theoretical: `atm_cassette_levels` and the `atm_transactions_visible`
  view were discovered missing *during* the database split.
- **E2 · The unexplained 34 MB `~/atm-backup_db.log`.** Expected small; nobody
  knows why it is not.
- **E3 · Break down historical EJ log matches** for
  `fetch-failed | error | already running`.
- **E4 · Confirm the expected EJ target count.** Last run reported one NCR target;
  verify against what the inventory should produce.
- **E5 · Split `blueprints/admin.py`** — 1,978 lines holding registry, cash,
  audit, schedules and users. Less urgent since the URL fix removed the
  misleading symptom, but still five unrelated things.
- **E6 · TLS and `SESSION_COOKIE_SECURE`** for production. Off on UAT because UAT
  is plain HTTP.
- **E7 · Historical duplicate transaction rows.** Known, unresolved, does not
  affect new data.
- **E8 · Correct `docs/BRD_Compliance_Matrix.md`.** It still reports BRD 5.3
  cash-depletion forecasting as ❌ "No forecasting exists. Build post-cutover",
  and schedules it post-cutover. `report-portal/cash_forecast.py` implements it,
  the ATM detail page calls it, and the design deliberately refuses to invent a
  runway from no data. Stale compliance evidence is worse than none, because it
  is the document a handover is checked against.

---

## F. Written down, never executed

Runbooks and checklists that exist and are deliberately parked. They are not
forgotten — they have triggers.

| # | Item | State | Reopens when |
|---|---|---|---|
| F1 | Real-AD cutover (`docs/LDAP_Integration.md` §4.3) | Parked 2026-09-24, runbook ready, code and checker maintained in git | >~20 users, a second department, or an audit requirement |
| F2 | OpenSearch security plugin | Production gate; deliberately not done in UAT because it rewires the auth of every client and risks the proven EJ pipeline | Ship with E6 (TLS) |
| F3 | RTO rehearsal | Restore path exists and is dry-run tested; **RTO itself is unproven** | Before cutover — rehearse once |
| F4 | `docs/UAT_Handover.md` | Draft with TBDs open | Sign-off |

---

## Suggested order

1. **A1** — rotate the tokens. Nothing gets cheaper by waiting.
2. **B1** — bulk inventory import, in parallel. This changes what the product *is*.
3. **0.1** — ask Network about TCP/10050. One email; can delete design work.
4. **C1** — advisory lock. Ten lines, removes a ceiling.
5. **B2** — load test, once the fleet is loaded and 0.1 is answered.
6. **C2 / C3** — topology decision, then alerting.
7. **C4, A3, E8**, then section E at leisure.

---

## What is verified, and what is not

- **Verified on UAT** — the database split; the six writers and the Grafana
  reader on `portal`; backup and restore (restore tested, dry-run only for the
  destructive path); the scheduler running as a singleton across three gunicorn
  workers; 419 tests.
- **Not verified** — anything at fleet scale (B2); EJ parsing against real busy
  journals (D2); physical cash levels (D3); whether the SMTP and SMS credentials
  work (D6); why the backup log is 34 MB (E2); whether any branch is reachable on
  TCP/10050 (0.1).
- **Assumed, not confirmed** — that the demo window expired as configured (B3).
- **Known stale** — `BRD_Compliance_Matrix.md` on cash forecasting (E8).
