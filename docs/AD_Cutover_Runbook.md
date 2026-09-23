# Real-AD Cutover Runbook — Mock LDAP Lab → Bank Active Directory

> Step-by-step migration executed **once IAM delivers**. Everything was proven
> against `mock-ldap`; this runbook changes *which directory* the apps talk
> to, not *how* they talk. Self-contained: no other doc needed at cutover.

## 0. Ask IAM for this (one email — nothing below starts without it)

- [ ] Service account: username (e.g. `svc-atm-monitor`), password, OWASP-style least privilege (bind + search only, no write)
- [ ] LDAPS endpoint: hostname, port (expect `636`), CA certificate (PEM) that signs it
- [ ] Base DN (e.g. `DC=bank,DC=local`) and users container/OU
- [ ] Login attribute confirmation: `sAMAccountName` (standard; speak now if otherwise)
- [ ] Group DNs for the two roles (or names to search under the groups OU)
- [ ] Two test accounts: one operator, one admin — plus permission to disable one for the leaver test
- [ ] Firewall: UAT-01 (`172.22.10.45`) → AD host on TCP 636

## 1. Portal TLS support (done — verify, don't build)

`report-portal/blueprints/auth.py::_ldap_auth` now honors `LDAP_USE_SSL`
+ `LDAP_CA_CERT_FILE`: `Server(..., use_ssl=True, tls=...)`, fail-closed
when the CA file is configured but missing. Lab default
(`LDAP_USE_SSL` unset) is plain and byte-identical — proven by
`tests/test_directory.py` (6 passed) + fail-closed check
`(False, None, False)` on missing CA.
Cutover-day vars: `LDAP_USE_SSL=true` + `LDAP_CA_CERT_FILE` (container path
of the CA PEM, mounted read-only).
- Review bar: password never logged, TLS errors surface as unreachable (local fallback) only on socket failure — bad credentials still hard-reject.

Grafana, Zabbix, and GLPI need **no code**: all three speak LDAPS natively
via config. Commit, push, deploy to UAT-01 first; lab matrix must stay green.

## 2. Portal cutover (first — flag-gated, instant rollback)

```bash
# on UAT-01, backup then edit .env:
AUTH_MODE=ldap
LDAP_HOST=<ad-host>            # service DNS, not IP (cert hostname must match)
LDAP_PORT=636
LDAP_USE_SSL=true
LDAP_CA_CERT_FILE=/run/secrets/ad-ca.pem   # CA placed here, chmod 600
LDAP_BASE_DN=<bank base DN>
LDAP_BIND_DN=<service account DN>
LDAP_BIND_PASSWORD=<from IAM, server env only>
LDAP_LOGIN_ATTR=sAMAccountName
LDAP_ADMINS_GROUP=<admins group CN>
LDAP_OPERATORS_GROUP=<operators group CN>
docker compose up -d report-portal
```

Verify: AD operator → operator pages; AD admin → admin pages; wrong
password → rejected (no local fallthrough); unknown user → rejected; local
`admin` → still in. Rollback: `AUTH_MODE=local` + recreate (seconds).

## 3. Grafana cutover (second — config file)

Rewrite `config/grafana/ldap.toml` for AD and commit it (DNs are not secrets):

- `host`/`port`/`use_ssl = true` (+ `ssl_cert_path`/`ssl_key_path` only if the bank wants client certs; normally CA trust is enough — set `ssl_skip_verify = false` always).
- `bind_dn`/`bind_password` → service account (password stays `$__env`, never in the file).
- `search_filter = "(sAMAccountName=%s)"`, `search_base_dns` → users OU.
- Group search: prefer `memberOf` — `group_search_filter = "(&(objectClass=user)(sAMAccountName=%s))"` with `member_of = "memberOf"`, base = users OU — then `group_dn` values become the real AD group DNs; keep the `*` → Viewer catch-all.
- Recreate grafana; verify Admin/Editor/Viewer mapping + local `admin` intact.
- Rollback: `GF_AUTH_LDAP_ENABLED=false` + recreate.

## 4. Zabbix cutover (third — DB-resident config, no files)

Administration → Authentication → LDAP: update the existing directory entry
(host, port 636 + TLS/CA per 6.4 directory form, base DN, bind DN/password,
search attribute `sAMAccountName`), or the same fields via API. Then:

- Test login with each AD account; link them to the existing local groups
  (`ATM-Operators` → Read on the ATM host groups, as proven in the lab —
  re-verify hosts are visible, re-grant if a new group was created).
- Lab-linked names (`ops1`/`admin1`) do not exist in AD — decide: keep them as
  disabled lab residue or delete after the leaver test. Real users link by
  their `sAMAccountName` on first login (or pre-create + set directory).
- Local `Admin` stays break-glass. Rollback: disable the directory / unlink user.

## 5. GLPI cutover (fourth — UI, ~15 min)

Setup → Authentication → LDAP directories → Add: host, port 636 (+ CA),
base DN, bind DN/password, login field `sAMAccountName`, group mapping to
GLPI profiles (admins group → super-admin, operators → technician). Verify one
login per role. Rollback: disable the directory (local logins unaffected).

## 6. Acceptance (same sitting, all apps)

Run the checker first (real portal code + toml consistency; browser spot-checks after):

```bash
# Lab defaults target the mock; on AD day export the DIR_* values from §0.
venv/bin/python -m pytest tests/test_directory.py -q
# Leaver test (after disabling the test account in the directory):
DIR_MODE=locked DIR_OPS_USER=<disabled-user> DIR_OPS_PASS=<old-pass> \
  venv/bin/python -m pytest tests/test_directory.py -q -k Locked
```

- [ ] Checker green: admin OK, operator OK, wrong password rejected, unknown user rejected, toml group DNs resolve; Grafana/Zabbix/GLPI login spot-checks in browsers; local break-glass OK everywhere
- [ ] **Leaver test:** disable one AD test account → confirm lockout on portal, Grafana, Zabbix (and GLPI) within one login attempt each; re-enable, confirm access returns
- [ ] `mock-ldap` stopped (`docker compose --profile ldap down`); lab users documented as retired
- [ ] Record committed: real group DNs, role mapping, date, who ran it (append to `docs/LDAP_Integration.md` §3 table)

## 7. Explicitly after (not this runbook)

OpenSearch security enablement and Keycloak-brokered SSO both wait on this
cutover **plus** TLS on every UI (SSO prerequisite). Sequence: AD cutover →
TLS everywhere → Keycloak evaluation → SSO. Do not start either here.

## 8. Secrets rule

AD bind password and CA cert live in server env/files only (`.env` chmod 600,
cert path per §2). They are never committed, emailed, or chatted. Rotate the
lab passwords out of server `.env` once cutover passes.
