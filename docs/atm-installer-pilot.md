# ATM installer — two-box pilot

This is the gate between "the tests pass" and "roll out to the fleet". Run it
on two real boxes with different shapes:

- **Box A — NCR with a legacy Agent 1.** The hardest migration: TCP 10050 is
  already owned, and the box has live monitoring history.
- **Box B — GRG with no agent.** The clean install, and the first box where
  `atm.identity` proves the GRG journal hostname.

Use a pilot change window. Nothing here registers a host or changes the
registry automatically.

## Preconditions (both boxes)

- [ ] `atm_locations` row exists with the correct `atm_id`, `vendor`, `district`,
      `ip_address`, `terminal_id` and `ej_hostname`.
- [ ] Zabbix host exists (`scripts/sync_atms_to_zabbix.py --apply`).
- [ ] The district proxy can open TCP 10050 to the ATM; the rule is live, not
      just requested.
- [ ] The portal **Agent setup** page shows **ready** and the server is the
      district proxy.
- [ ] The profile downloads and its hash matches the page.
- [ ] `AtmMonitorSetup.exe` is the release artifact, and its SHA-256 matches
      `release.json` / the published `.sha256`.
- [ ] The custodian is present (or remote hands are available) and the ATM is
      not mid-transaction.

## Box A — NCR, legacy Agent 1

1. Record the current service and port:
   ```cmd
   sc query "Zabbix Agent"
   netstat -ano | findstr :10050
   ```
2. Copy the EXE and `ATM-....profile.json` to a local admin folder.
3. Run `AtmMonitorSetup.exe /DRYRUN`. Expect:
   - profile and ATM id printed;
   - all probe hashes match;
   - `agent` install/upgrade or `unchanged`;
   - no legacy error only if `/REPLACELEGACY` is supplied.
4. Run with `/REPLACELEGACY`. Expect the legacy service stopped and disabled,
   Agent 2 installed, config written, service running, TCP 10050 listening, and
   the service-account check passing.
5. From the proxy:
   ```bash
   zabbix_get -s <ATM_IP> -k "atm.identity"
   zabbix_get -s <ATM_IP> -k "atm.parts"
   ```
   The first is JSON with the NCR terminal id and EJ hostname; the second is
   the parts JSON.
6. Open **Latest data** for the host: NCR parts and XFS rows resume. Confirm
   the host did not get a duplicate or a new interface.
7. Paste the `atm.identity` output into the portal and confirm the terminal id
   and EJ hostname agree with the registry. Do not "fix" a mismatch; record it.
8. Roll back if needed: stop Agent 2, re-enable `Zabbix Agent`, restore the
   files from `C:\monitor\backup\<timestamp>`, and restart the legacy service.

## Box B — GRG, clean install

1. Confirm the box has no `Zabbix Agent 2` service and TCP 10050 is closed.
2. Dry run, then apply without `/REPLACELEGACY`. Expect the agent installed,
   the GRG `atm.xfs.cdm` parameter present, and `atm.parts` absent (that is the
   NCR collector and must not run on GRG).
3. From the proxy:
   ```bash
   zabbix_get -s <ATM_IP> -k "atm.identity"
   zabbix_get -s <ATM_IP> -k "atm.xfs.cdm"
   ```
   The identity JSON should name the GRG EJ hostname (the computer name) and a
   journal root. The XFS JSON should carry CDM/cassette data.
4. Confirm `ej_pull_targets.py --vendor GRG --explain` no longer prints
   `no EJ hostname recorded` for this ATM.
5. If the registry approved image roots, open the image viewer once and confirm
   a read; if it did not, confirm the roots file is absent and image keys are
   not in `atm-agent.conf`.

## Failure drills (one box, then restore)

- [ ] Run a profile for the other pilot ATM: expect refusal (`already managed`).
- [ ] Remove `/REPLACELEGACY` on Box A: expect exit 22, not a partial install.
- [ ] Corrupt one bundled probe in a scratch copy: expect exit 13 before any
      write.
- [ ] Point `Server=` at a name the box cannot resolve: expect a warning in the
      plan, and confirm whether the agent still starts. Record the DNS gap; do
      not edit the profile to hide it.
- [ ] Set the agent service to a named account: expect `skipped`, not passed.

## Acceptance

- Both boxes report `atm.identity` and their vendor key from the proxy.
- The portal shows no identity conflict that is actually a registry error.
- Box A kept its host and history; Box B created no duplicate.
- The installer log and state file exist and name the same profile hash the
  portal served.
- A second run reports `changed=false` and no new backup.

## Record

| Field | Box A (NCR) | Box B (GRG) |
|---|---|---|
| ATM id | | |
| Installer version / SHA-256 | | |
| Profile hash | | |
| Dry-run result | | |
| Apply result | | |
| `atm.identity` result | | |
| `atm.parts` / `atm.xfs.cdm` | | |
| Service account check | | |
| Registry conflicts | | |
| EJ hostname confirmed | | |
| Rollback needed? | | |
| Notes | | |
