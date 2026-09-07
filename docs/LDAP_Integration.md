# Directory Authentication (LDAP/AD) — Integration Record
### Dashen Bank ATM Monitoring System

| Field | Value |
|-------|-------|
| Version | 1.0 |
| Date | 2026-09-07 |
| Branch | `uat-prep` |
| Status | Lab-proven against mock directory; production cutover pending IAM answers |

## 1. What this is — and is not

**This is one IDENTITY, not one login page.** Every app keeps its own login
screen, but all of them check the same directory: one username, one password,
access managed once in AD groups. Disable a leaver in AD → locked out
everywhere. That is what is built and proven below.

**True single sign-on** (log in once, walk into all five apps with no further
prompts) is a different layer — SAML/OIDC sessions on top of this identity
foundation. Deliberately deferred: it needs TLS everywhere first plus IdP
registrations per app, and it buys nothing for UAT. See §6.

## 2. Architecture

```
                ┌──────────────┐
                │  Bank AD  ───┼── future (ldaps://<host>:636)
                │  (pending)   │
                └──────────────┘
                        ▲  same LDAP protocol, 4 values differ (§5)
                        │
┌──────────┐  ┌────────┴─────────────────────────────┐
│ mock LDAP│  │ App behavior                          │
│ (lab)    │  │ portal: AUTH_MODE=local|ldap          │
│ :3890    │  │ grafana/zabbix: LDAP added alongside  │
└──────────┘  │ local accounts (nothing removed)      │
              │ glpi/opensearch: documented, staged   │
              └──────────────────────────────────────┘
```

Design rules held throughout:
- **Additive only.** No local account removed or weakened anywhere; every
  change is reversible in minutes (documented per app below).
- **Bad directory credentials never fall through** to local auth. Local is
  used only when the directory itself is unreachable (availability), or when
  AUTH_MODE=local (current UAT default).
- **No real secrets in git.** Lab passwords live in server `.env`
  (chmod 600) or clearly-marked TEST fixtures; production values arrive via
  IAM and go straight to server env, never the repo.

## 3. What was built (lab-proven on UAT, 2026-09-07)

| # | Item | Location | Verification |
|---|------|----------|--------------|
| 1 | Mock directory (osixia/openldap, profile-gated, seeded users + ATM-Operators/ATM-Admins) | `docker-compose.yml` (`mock-ldap`), `config/ldap/seed.ldif` | Bind accept/reject correct; group lookups resolve |
| 2 | Portal LDAP bind (ldap3, search-then-bind, group→role, failover semantics) | `report-portal/blueprints/auth.py`, `Dockerfile` (ldap3), `.env.uat` template block | Matrix tested: ops1→operator pages, admin1→admin pages, bad password rejected, local fallback only on directory-down |
| 3 | Portal compose passthrough (`AUTH_MODE`, `LDAP_*`) | `docker-compose.yml` | Container env verified; reverted to `AUTH_MODE=local` after tests |
| 4 | Grafana LDAP (`ldap.toml`, group mappings, local accounts intact) | `config/grafana/ldap.toml`, compose env+mount | Logins verified; roles correct (Admin/Editor); left ENABLED (additive) |
| 5 | Zabbix LDAP (user directory + per-user linkage + global switch) | Live Zabbix DB (documented recipe §4) | ops1/admin1 sessions via directory; local Admin intact |
| 6 | Switchover + per-app status table | `config/ldap/README.md` | — |

Bugs found and fixed during verification (each would have cost a day
against real AD): ldap3 `dn` attribute error; group lookup on the wrong
connection (user-ACL-proof restructure); `.env` missing-newline fusion;
Grafana bind password never passed into container; Grafana group search
needs DN-pattern filter (`%s` = bare username); Zabbix `username`≠`alias`
and the silent global `ldap_auth_enabled` switch.

## 4. Remaining work (runbooked, not yet executed)

### 4.1 GLPI LDAP (~15 min, manual UI)
Setup → Authentication → LDAP directories → Add: host, port 389 (636 +
CA cert for real AD), base DN, bind DN/password, login field (`uid`, later
`sAMAccountName`), group mapping to GLPI profiles (admin/super-admin for
ATM-Admins, technician for ATM-Operators). Verify: directory user logs in;
disable the test user in the directory and confirm lockout.

### 4.2 OpenSearch security (production gate — do NOT do in UAT)
Enabling the security plugin rewires authentication for OpenSearch +
Dashboards + every client (filebeat, portal, grafana datasource). It risks
the proven EJ pipeline for zero UAT benefit. Schedule with the TLS work
(§6): internal users file, roles mapping to directory groups, Dashboards
reconfiguration, full EJ regression after.

### 4.3 Real-AD cutover checklist (needs IAM answers first)
1. Service account (`svc-atm-monitor`, bind+search only), LDAPS endpoint +
   CA cert, base DN, `ATM-Operators` / `ATM-Admins` groups
2. Apply the 4-line change per app (§5 table in `config/ldap/README.md`)
3. Verification: login matrix per app (admin/operator/wrong-password),
   **leaver test** (disable test user in AD → confirm lockout everywhere),
   local break-glass accounts confirmed working
4. Rollback per app: portal `AUTH_MODE=local`; grafana `GF_AUTH_LDAP_ENABLED=false`;
   Zabbix disable directory / unlink user; all instant, all tested paths

## 5. Secrets inventory

| Secret | Lives in | In git? |
|--------|----------|---------|
| Lab LDAP admin/test passwords | Server `.env` + TEST-only seed comments | Test values only, clearly marked |
| Portal/Grafana/Zabbix local admins | Server `.env` | Placeholders only in repo |
| GLPI app/user tokens | Server `.env` | Never in repo |
| Future: AD bind password, CA cert | Server env/files only | Never in repo |

## 6. Future: true SSO (SAML/OIDC)

Requires, in order: (a) TLS on every UI (prerequisite for any SSO flow);
(b) an IdP both sides agree on (bank Entra ID/ADFS or Keycloak);
(c) per-app registration (Zabbix SAML native; Grafana OAuth/SAML native;
GLPI via plugin — weakest link; OpenSearch via security plugin;
portal via OIDC client lib). Track as decision **D7** (matrix §8):
do not start before TLS + AD-directory cutover are done and stable.

## 7. Traceability

- BRD §7.3 / row 17 (RBAC, audit): directory-group mapping + per-system
  audit trails; single-identity join left for SSO phase
- Compliance matrix: row 17 evidence points here
- UAT Test Plan: LDAP verification is engineering acceptance, not bank UAT
