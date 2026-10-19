# Remaining work — what blocks production, and what can wait

Written 2026-10-19, at `uat-prep` @ `e8a5dda`, suite 419 passed / 8 skipped.

Two different kinds of work have been getting mixed together in conversation, and
they deserve separating. One is code quality — migration discipline, splitting a
1,978-line file, chasing a log file. It is worth doing and it is not urgent. The
other is the set of things that actually stand between this and production, and
only three of those are load-bearing:

1. live credentials sitting in git history,
2. a monitoring portal that has three ATMs in it,
3. a scheduler that cannot ever run on more than one host.

Everything in section E can wait indefinitely without a single consequence. Read
the groups in order; the ordering is the argument.

---

## Where things stand

| Fact | Value |
|---|---|
| Branch / head | `uat-prep` @ `e8a5dda` |
| Test suite | 419 passed, 8 skipped |
| Registered ATMs | **3** (2 NCR, 1 GRG), `In Service` 0 |
| Transactions in the portal | ~182k, of which the last day is real EJ + ISO8583 |
| Portal database | separate `portal` DB on UAT-01; six writers and the Grafana reader moved across |
| Zabbix database | `zabbix`, frozen at 181,265 transactions — still the rollback |
| UAT-01 / UAT-02 | `dbtcatmmonappuat` (172.22.10.45) / `dbtcatmmondbuat` (172.22.10.60) |

The database split is done and verified. It is not on this list except where it
touches something else.

---

## A. Live risk — do first

The only items whose cost goes *up* with time.

| # | Task | Who can move it |
|---|---|---|
| A1 | Rotate `GLPI_APP_TOKEN` and `ZABBIX_PASS`. Untracking `.env.uat` did not remove them — they are still in the history of every clone, and a token that has been in a repository should be treated as disclosed | You |
| A2 | Decide on history rewriting. Rotating first makes this optional; if the repo is held by a small group, rotation alone is defensible | You |
| A3 | `report-portal/requirements.lock` exists only on UAT-01 and is untracked. Either commit it (reproducible builds) or delete it (stop pretending it is a lockfile) | Portal work |

Rotation is a vendor/console job: GLPI API user token, and the Zabbix password.
Neither requires downtime, which is why there is no excuse for A1 outliving this
document by much.

---

## B. Blocks the product from being real

| # | Task | Who can move it |
|---|---|---|
| B1 | **Bulk ATM inventory import.** The registry holds three devices. Every number on the dashboards — 5,078 transactions in 24h, 85.9% approval — describes three devices. Onboarding 1,300 ATMs through a one-at-a-time web form is not viable | Portal work; needs your inventory file |
| B2 | **Load test at realistic fleet size.** Nothing has been exercised above 3 ATMs / ~182k rows. The map, fleet pages, dashboard aggregates and CSV export all behave differently at 1,300 nodes and ~100M rows. This is the difference between "works" and "works at scale", and it is currently unknown | Portal work |
| B3 | Verify the demo window lapsed cleanly. The simulator policy was set to expire and the banner now reads `(0 simulated rows hidden)`, which suggests it did — confirm no dashboard silently emptied and the real-only view is what is on screen | Portal work |

**B1 is the highest-value item here.** Everything else in this document is
engineering around a fleet we cannot see. If you can supply ATM ID, branch, city,
region, vendor, model and IP in any tabular form, that is enough to build the
importer against — it does not need to be clean, and cleaning it is part of the
job.

---

## C. Reliability ceilings

| # | Task | Who can move it |
|---|---|---|
| C1 | Swap the scheduler's `/tmp/portal-scheduler.lock` `fcntl.flock` for a Postgres advisory lock (`pg_try_advisory_lock`). Same singleton design, same "no Redis needed" property, but it works across hosts instead of within one container | Portal work |
| C2 | Decide the production topology: Postgres on its own host, or everything colocated? This drives backup windows, HA options and the cutover plan, so it is a decision before it is a task | You |
| C3 | Alert on the portal itself. `/ready` exists and nothing polls it. If the portal is down, the first person to notice is whoever needed a report | Portal work |
| C4 | Reconcile UAT-02: it is 358 commits behind, with gateway work parked on a local branch `uat02-local` @ `ec5dc2f`. Decide whether that work merges or the host is re-based | Portal work |

C1 is roughly ten lines and is the only remaining change that removes a hard
architectural ceiling rather than tidying up. It should be done regardless of
whether you ever run two replicas: while the lock is a local file, a rolling
restart either duplicates scheduled emails or misses a poll cycle, and there is
no way to have neither.

---

## D. External dependencies — not ours to unblock

Listed so they stop appearing as open tasks. None of these can be closed by
writing code.

| # | Task | Blocked on |
|---|---|---|
| D1 | Switch / Core Banking integration. The simulator is still not an authoritative transaction source | Vendor access |
| D2 | EJ parser viability against busy NCR/GRG journals — unproven against real traffic, only against what we have | Real journal samples |
| D3 | Physical cassette balances. Cash forecasting remains book-based until these exist | Field/hardware access |
| D4 | GRG telemetry over UDP/161 | Network access and vendor MIB |
| D5 | Camera images — discovery, storage, linkage, and compliance sign-off | Physical, legal |
| D6 | SMTP and Africa's Talking credentials | You |

D2 is worth calling out: the EJ path is the one dependency where a negative
answer changes the design rather than just delaying a feature. If the parser
cannot keep up with a busy journal, the ingestion strategy needs rethinking, not
tuning.

---

## E. Hygiene — do during slack

Real, but nothing here blocks anyone.

- **E1 · Migration discipline.** Numbered SQL files and a `schema_migrations`
  table. This is not theoretical: `atm_cassette_levels` and the
  `atm_transactions_visible` view were discovered missing *during* the database
  split. A registry file caught it, but only because someone compared lists.
- **E2 · The unexplained 34 MB `~/atm-backup_db.log`.** Expected to be small;
  nobody knows why it is not.
- **E3 · Break down the historical EJ log matches** for
  `fetch-failed | error | already running`.
- **E4 · Confirm the expected EJ target count.** The last run reported one NCR
  target; verify that against what the inventory should have produced.
- **E5 · Split `blueprints/admin.py`.** Now 1,978 lines holding the registry, cash,
  audit, schedules and users. Less urgent than it was: the URL-prefix fix removed
  the misleading symptom, but the file is still five unrelated things.
- **E6 · TLS and `SESSION_COOKIE_SECURE`** for production. Off on UAT because UAT
  is plain HTTP; must be on in production.
- **E7 · Historical duplicate transaction rows.** Known, unresolved, and does not
  affect new data.

---

## Suggested order

1. **A1** — rotate the tokens. Nothing else on this list gets cheaper by waiting.
2. **B1** — bulk inventory import, in parallel. This is the one that changes what
   the product *is*.
3. **C1** — advisory lock. Ten lines, removes a ceiling.
4. **B2** — load test once the fleet is loaded, because that is the first moment
   it is possible to do meaningfully.
5. **C2 / C3** — topology decision, then alerting.
6. **C4, A3**, then section E at leisure.

---

## What is verified, and what is not

Stated plainly so nobody mistakes the two:

- **Verified on UAT** — the database split, the six writers and the Grafana
  reader on the `portal` database, backup and restore (restore tested, dry-run
  only for the destructive path), the scheduler running as a singleton across
  three gunicorn workers, and 419 tests.
- **Not verified** — anything at fleet scale (B2), EJ parsing against real busy
  journals (D2), physical cash levels (D3), whether the SMTP and SMS credentials
  work (D6), and why the backup log is 34 MB (E2).
- **Assumed, not confirmed** — that the demo window expired as configured (B3).
