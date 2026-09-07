# Mock LDAP lab (stand-in for bank Active Directory)

While IAM access is pending, `mock-ldap` (osixia/openldap) lets us build and
verify every LDAP integration against a controlled directory, then shift to
real AD with a 4-line change.

## Lab directory

- Base DN: `dc=atmmon,dc=local` · admin: `cn=admin,dc=atmmon,dc=local`
- Test users (TEST ONLY): `ops1` / `OpsPass123!` (member: ATM-Operators),
  `admin1` / `AdminPass123!` (member: ATM-Admins)
- Admin password comes from env `LDAP_ADMIN_PASSWORD` (default lab value in
  compose — override in `.env`, never commit a real one)

## Run it (app server; never part of default `up`)

```bash
docker compose --profile ldap up -d mock-ldap
sleep 20
docker exec mock-ldap ldapsearch -x -H ldap://localhost \
  -b dc=atmmon,dc=local -D "cn=admin,dc=atmmon,dc=local" -w "$LDAP_ADMIN_PASSWORD" \
  "(objectClass=groupOfNames)" cn member
```

Tear down: `docker compose --profile ldap down` (add `-v` to wipe lab data).

## Switchover to real AD (when IAM answers)

Only these change per app (Grafana/Zabbix/GLPI/portal/OpenSearch):
1. Host: `172.22.10.45:3890` → bank `ldaps://<host>:636` (+ CA cert for TLS)
2. Base DN: `dc=atmmon,dc=local` → bank base DN
3. Bind DN: `cn=admin,...` → bank service account (`svc-atm-monitor`)
4. Login attribute `uid` → `sAMAccountName`; group match `member={0}` works on
   both (or switch to `memberOf` — one line per app, noted in each config)

Keep a rollback path: `AUTH_MODE=local` on the portal, local users intact
everywhere, until the AD cutover is verified with a leaver test.
