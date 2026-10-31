# ADR — ATM agent deployment: one signed installer, one profile per ATM

**Status:** accepted for the Phase 2 pilot
**Date:** 2026-10-31

## Context

The bank does not install or manage the Zabbix agent; Dashen does. ATMs are
offline from the public internet, have static addresses, and sit behind district
proxies. The Zabbix host is pre-created from `atm_locations`, so enrolment is
not the problem — applying the right configuration to the right box is.

The first attempt at a per-ATM installer would have produced one signed binary
per ATM. That distributes the code-signing private key to every technician and
every build, and it makes a release impossible to audit. The alternative is one
reusable binary plus a small per-ATM document.

## Decision

1. **One Authenticode-signed `AtmMonitorSetup.exe` per release**, reused for
   every ATM. It bundles the probes, a pinned Zabbix Agent 2 6.4 MSI, and the
   installer payload.
2. **One portal-rendered `<atm_id>.profile.json` per ATM.** The profile carries
   the exact file contents (`atm-agent.conf`, `zabbix_agent2.conf` text, roots
   file), the probe hashes, the agent server and the ATM id. It contains no
   SNMP community, PSK or other secret.
3. **The installer is a reconciler, not a registrar.** It never creates a
   Zabbix host, never auto-registers, and never writes observed identity back
   to `atm_locations`.
4. **The profile HMAC is an integrity aid, not the installer's trust anchor.**
   The HMAC secret must not ship to an ATM, so the installer checks the
   signature's presence and format, not the digest. Authenticity comes from the
   signed EXE plus the portal/download channel. `/EXPECTEDHASH=` pins the exact
   profile bytes when a change ticket needs that guarantee.
5. **Verification runs as the agent's service account.** Testing as the
   installing administrator would not prove that `UserParameter` can run.
   For the default LocalSystem service the installer uses a one-shot SYSTEM
   scheduled task and removes it. A named account is reported as skipped; it is
   never silently treated as passed.
6. **Passive only.** No `ServerActive`, no outbound internet requirement, no
   remote commands. The proxy opens TCP 10050 to the ATM; `Server=` names the
   proxy, not the central server.
7. **Fail closed on identity and images.** The installer writes only approved
   image roots, removes a roots file it previously managed when the registry
   removes every root, and surfaces identity conflicts in the portal instead of
   healing them.

## Consequences

- A release is signed once and can be audited by hash. Per-ATM binaries are not
  produced.
- A technician needs two files, not a build toolchain: the EXE and the profile.
- The profile can be attached to a change ticket; it is safe to store because
  it has no secret.
- A profile from another ATM is refused by the state file (`-Reassign` is an
  explicit override) and by `/ACCEPTATM`.
- The installer cannot prove the HMAC digest offline; it compensates with
  probe hashes, exact-content writes and an optional raw-bytes pin.
- The two-box pilot is the gate for the service-account task, the legacy Agent 1
  handover and the district-proxy DNS assumption.

## Rejected alternatives

- **Per-ATM signed EXE:** key distribution and release explosion.
- **Enrollment token / auto-registration:** the host already exists; a token
  adds a secret to the ATM and a second source of truth.
- **Installer pulls from the portal:** ATMs have no outbound internet.
- **Installer generates the config:** it would become a second policy engine
  and drift from the portal.
- **Verifying as the installing admin:** passes where the service fails.
