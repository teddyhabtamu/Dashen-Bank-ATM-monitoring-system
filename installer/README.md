# ATM agent installer

`AtmMonitorSetup.exe` applies one portal-rendered `<atm_id>.profile.json` to one
ATM. It is a reconciler, not a registrar:

- The Zabbix host already exists (created from `atm_locations`).
- The profile is the desired state. The installer writes it, never re-invents
  the policy.
- `atm.identity` is evidence. The installer never writes box-reported identity
  back to the registry.
- Passive checks only. There is no `ServerActive`, no auto-registration and no
  outbound internet requirement on the ATM.

One signed EXE per release is built once and reused for every ATM. Only the
profile differs per box. Per-ATM signed binaries are deliberately rejected:
they would put the signing key on every technician's laptop.

## What the EXE contains

| Part | Purpose |
|---|---|
| `atm-install.ps1`, `AtmInstall.psm1` | the reconciler |
| `probes/*.ps1` | the exact probe files named in the portal manifest |
| `agent/zabbix_agent2-6.4.x-windows-amd64-openssl.msi` | pinned Agent 2 package |
| `VERSION.txt` | installer, agent and manifest provenance |

The profile is **not** bundled. It is downloaded from the portal for that ATM
and placed next to the EXE, or passed with `/PROFILE=<path>`.

## Build and sign

Requirements on the build host: Windows, PowerShell 5.1+, Inno Setup 6,
Windows SDK (`signtool`), Python 3 (manifest check).

```powershell
py -3 scripts\build_agent_manifest.py --check
.\installer\build_agent_setup.ps1 -Version 0.1.0 `
    -SignPfx C:\certs\dashen.pfx -SignPassword $env:PFX_PASSWORD `
    -RequireSignature
```

Without `-SignPfx` the build still produces an EXE, but says **UNSIGNED** and
`-RequireSignature` turns that into an error. CI (`.github/workflows/agent-installer.yml`)
builds on `windows-latest` and signs when `WINDOWS_PFX_BASE64` and
`WINDOWS_PFX_PASSWORD` are present. A tag `agent-v*` is refused unless the
artifact is signed.

The build refuses to run when a probe's SHA-256 differs from
`report-portal/agent_manifest.json`, or when the Agent 2 MSI differs from the
pinned hash in `build_agent_setup.ps1`. Add a new agent version by adding its
hash in the same commit that changes the version.

Artifacts land in `installer/dist/`:

- `AtmMonitorSetup-<version>.exe`
- `AtmMonitorSetup-<version>.sha256`
- `release.json` (agent version/hash, commit, EXE hash, signed flag)

## Operator runbook

1. In the portal open **ATM → Agent setup**, confirm the profile is **ready**
   and the server is the district proxy (not the central server).
2. Download `<atm_id>.profile.json`. Record the hash shown on the page if you
   want to pin it with `/EXPECTEDHASH=`.
3. Put the profile next to `AtmMonitorSetup.exe`.
4. Run as administrator:

   ```cmd
   AtmMonitorSetup.exe /DRYRUN
   ```

   The dry run checks the profile, the bundled probe hashes and the MSI, prints
   every planned action, and writes nothing.
5. Run again without `/DRYRUN`, confirm the ATM id in the prompt, and apply.
   In a change-managed run pass `/ACCEPTATM=<atm_id>` as well: the wrapper
   checks the filename and the payload checks the document's own `atm_id`, so a
   renamed profile cannot be applied to the wrong box.
6. Verify from the proxy/server:

   ```bash
   zabbix_get -s <ATM_IP> -k "atm.identity"
   zabbix_get -s <ATM_IP> -k "atm.parts"
   ```

   The log is `C:\ProgramData\AtmMonitor\logs\install.log`. The state file is
   `C:\monitor\atm-agent-state.json`.
7. In the portal open **ATM → Agent setup** and confirm **Box identity** is
   **Reported** within five minutes. The portal reads the passive
   `atm.identity` item from Zabbix automatically; **Refresh from Zabbix**
   requests a check immediately. Manual paste is only the fallback when the
   Zabbix API user cannot run `task.create` or the item is not linked yet.

### Useful flags

| Flag | Meaning |
|---|---|
| `/PROFILE=<path>` | explicit profile (otherwise exactly one `*.profile.json` beside the EXE) |
| `/DRYRUN` | plan only, no writes |
| `/EXPECTEDHASH=<sha256>` | pin the profile bytes; mismatch stops the run |
| `/ACCEPTATM=<atm_id>` | require the profile's internal ATM id to match |
| `/ALLOWNOTREADY` | lab only: apply a profile with no server |
| `/ALLOWUNSIGNED` | lab only: apply a profile without an HMAC |
| `/REPLACELEGACY` | stop and disable a running Zabbix Agent 1 before Agent 2 |
| `/REASSIGN` | allow a profile whose ATM id differs from the state file |
| `/NOAGENTINSTALL` | do not install/upgrade Agent 2 (config-only recovery) |
| `/NOSERVICE` | do not start/restart the service (staging) |
| `/NOSERVICEACCOUNTTEST` | skip the scheduled-task verification |
| `/ALLOWUNVERIFIED` | allow a named (non-SYSTEM) agent account we cannot test |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success (or a clean dry run) |
| 10 | invalid profile / unsafe path / not passive-only |
| 11 | profile is not ready (no server) |
| 12 | profile SHA-256 mismatch (`/EXPECTEDHASH`) |
| 13 | bundled probe does not match the profile |
| 14 | unsigned profile |
| 15 | `/ACCEPTATM` or filename ATM id mismatch |
| 20 | write failure |
| 21 | Agent 2 MSI install/upgrade failure |
| 22 | legacy Agent 1 is running (pass `/REPLACELEGACY`) |
| 23 | service control failure |
| 24 | agent config test or TCP 10050 listen failure |
| 25 | service-account verification failed |
| 30 | unexpected error |

## What it changes

- `C:\monitor\atm-agent.conf` — `UserParameter` include, generated by the portal.
- `C:\monitor\atm-image-roots.txt` — only when the registry has approved roots.
  If every root is removed, the installer removes the file it previously
  managed; it never deletes a file it did not record.
- `C:\monitor\*.ps1` — the probes named in the profile, hash-checked.
- `C:\Program Files\Zabbix Agent 2\zabbix_agent2.conf` — the full passive-only
  config from the profile (`Server=<proxy>`, `Hostname=<atm_id>`,
  `Timeout=30`, `DenyKey=system.run[*]`, one `Include`).
- Zabbix Agent 2 6.4 service, startup type automatic.

Every file it replaces is copied to `C:\monitor\backup\<timestamp>\` first.
A failed run leaves the backup and does not update the state file. Copy the
files back and restart the service to roll back.

## Service-account verification

Zabbix runs `UserParameter` commands as the agent service account. Testing as
the installing administrator would prove nothing. When the service runs as
`LocalSystem` (the default), the installer creates a one-shot scheduled task as
`SYSTEM`, runs the probes through the agent, and deletes the task. The result
includes the actual `atm.identity`/`atm.parts` output.

If the service runs as a named account, the installer cannot obtain that
account's password safely and reports `skipped`. In that case run the same
probe manually under that account, or pass `/ALLOWUNVERIFIED` and record why.

## Legacy Agent 1

A running `Zabbix Agent` service owns TCP 10050 and blocks Agent 2. The default
is to stop with exit 22. With `/REPLACELEGACY` the installer stops and disables
the legacy service (it does not uninstall it), installs Agent 2, and writes the
new config. Re-enable the old service only if you are deliberately rolling
back.

## Network assumptions

- The district proxy opens TCP 10050 to the ATM. The agent listens on 10050.
- The Agent 2 MSI adds a local Windows Firewall exception for 10050. The
  installer does not create or change firewall rules. If a bank GPO or endpoint
  policy strips that exception, the proxy path is open but the box still
  refuses the connection; check `netsh advfirewall firewall show rule
  name=all` for a Zabbix rule.
- `Server=` names the proxy. If the ATM cannot resolve that DNS name, the agent
  cannot match the proxy; the installer warns but does not rewrite the profile.
  Fix DNS or add a hosts entry at the proxy/DNS layer, not in the profile.
- The ATM needs no outbound internet. The EXE and MSI are local.

## Tests

- `tests/test_agent_installer.py` renders real profiles and exercises dry run,
  apply, idempotency, roots removal, wrong-ATM refusal, path traversal,
  `ServerActive`, probe tampering and verification. It runs on Windows CI.
- `tests/test_agent_installer_contract.py` pins the wrapper flags, exit codes,
  MSI hash and signing hook; it runs in the dependency-free Linux suite.

## Known limits

- The profile's HMAC is checked for presence, not recomputed: the secret must
  never ship to an ATM. Authenticity comes from the signed EXE and the
  portal/download channel. Use `/EXPECTEDHASH=` to pin the exact profile.
- Only x64 Windows is built. A 32-bit ATM needs a separate i386 artifact.
- The installer does not open the network firewall, register the host, or
  uninstall Agent 1. Those are deliberate separate actions.
- Zabbix Agent 2 6.4.21 has a known random startup crash on Windows
  ([ZBX-25530](https://support.zabbix.com/browse/ZBX-25530)): the agent exits
  with `cannot load os dependent items ... buffer`. The installer retries the
  SCM start up to three times, which usually clears it. The upstream fix is in
  6.0.39 / 7.0.10+; 6.4 is end-of-life and has no fix. If all three attempts
  fail, the agent log is the evidence — this is not a profile or probe error.
