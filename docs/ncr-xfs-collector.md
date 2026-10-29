# NCR XFS collector

> **Purpose:** collect NCR APTRA CEN/XFS cash and hardware telemetry through
> the Zabbix agent. Read-only: no dispense, reset or configuration write.

## Status

The probe is `scripts/ncr-xfs-probe.ps1`. It is built against the NCR test
core's verified behaviour:

- 32-bit Windows, PowerShell 2.0, APTRA XFS 3.02.
- `msxfs.dll` loaded from `C:\Program Files\Common Files\NCR\msxfs.dll`
  through the system `PATH`.
- `WFSStartUp(0x00030203) -> 0` under `nt authority\system`.
- `CurrencyDispenser1` is the real dispenser: `WFSOpen -> 0`,
  `WFSGetInfo 301 -> 0`, `WFSGetInfo 303 -> 0`, five cash units.
- `WFSGetInfo 303 -> 0` again on `MediaDispenser1`, but it is an alias of the
  same cash units. Bill/Coin/BRM dispensers are absent or subordinate.

The probe emits the same JSON shape as `grg-xfs-probe.ps1 -Json`, so the
existing CEN/XFS template and the portal's `xfs_metrics.normalize()` consume
it without a second contract.

## Preconditions

On the ATM:

- `C:\monitor\ncr-xfs-probe.ps1`
- `C:\Program Files\Common Files\NCR\msxfs.dll` on the system `PATH`
  (confirmed on the test core)
- Zabbix agent `Timeout` at least 30 seconds; the probe opens several XFS
  services and the default 3 seconds is too short
- the agent service account has XFS access (`LocalSystem` was verified)

## Deploy

1. Copy the probe:

   ```powershell
   copy \\path\to\ncr-xfs-probe.ps1 C:\monitor\ncr-xfs-probe.ps1
   ```

2. In `C:\Program Files\Zabbix Agent\zabbix_agentd.conf`, **replace** the GRG
   XFS user parameter with the NCR one (do not enable both; they use the same
   key):

   ```ini
   # GRG hosts:
   # UserParameter=atm.xfs.cdm,powershell ... -File "C:\monitor\grg-xfs-probe.ps1" -Json

   # NCR hosts:
   UserParameter=atm.xfs.cdm,powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\monitor\ncr-xfs-probe.ps1"
   ```

   Also ensure:

   ```ini
   Timeout=30
   ```

3. Restart the Zabbix agent.

## Verify locally

Run the probe in the same bitness the agent uses:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File C:\monitor\ncr-xfs-probe.ps1
```

It must print exactly one JSON line. Parse it:

```powershell
$p = & C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass -File C:\monitor\ncr-xfs-probe.ps1 |
  ConvertFrom-Json

$p.cdm.service                 # CurrencyDispenser1
$p.cdm.status.device           # 0 = online
$p.cdm.summary.total_notes     # 31 on the test core
$p.cdm.summary.total_amount    # 2500 ETB on the test core
$p.cdm.cassettes.Keys          # 0050A 0100A 0100B 0200A Rej01
$p.siu.supervisor_switch       # 0 = normal run
```

On the test core the expected cash mapping is:

| NCR unit | Denomination | Count | Template ID |
|---|---|---|---|
| 1 | ETB 50 | 12 | `0050A` |
| 2 | ETB 100 | 10 | `0100A` |
| 3 | ETB 100 | 9 | `0100B` |
| 4 | ETB 200 | 0 | `0200A` |
| 5 | reject | 3 | `Rej01` |

## Verify from the Zabbix server

```bash
zabbix_get -s <ATM_IP> -t 30 -k atm.xfs.cdm
```

The value is the same single-line JSON. Until the template is linked, the
master item is enough to prove collection.

## Template

For the pilot, link the existing **Dashen Bank ATM Hardware - GRG XFS**
template to the NCR host. Its item keys and JSON paths are the same contract;
only the name is historical. The portal will show the XFS hardware view as
soon as `atm.xfs.cdm` exists.

A separate NCR-named template is cosmetic and can follow after the data is
validated.

## State precedence — do not flip yet

`zabbix_modules/status_rules.interpret_state()` checks `atm.parts` (the NCR
WMI master) first. While that item exists, **WMI remains authoritative** and
the XFS data is parallel telemetry. This is deliberate for the pilot: it lets
us compare the two sources without changing the live state.

XFS is the only source that can produce:

- **Out Of Cash** — all dispense cassettes have zero notes
- **In Supervisor** — the SIU operator switch is in supervisor mode
- **Hardware Faults** — CDM/IDC/PTR/PIN device status

After the pilot, remove or disable the WMI master on the NCR host (or change
the precedence explicitly) to let XFS drive state. Do not do that until the
XFS readings have been compared with the WMI readings for a full shift.

## Known gaps

- **SIU doors and tamper:** only the operator switch is mapped. The probe
  emits the first 40 SIU sensor values as `siu.raw`; cabinet/safe door and
  tamper offsets differ by NCR SIU model and must be identified from that
  array before they are enabled. Until then they are `255` (not queried).
- **Offline:** XFS has no network-link signal. The existing reachability
  layer still decides Offline vs Agent Disconnected.
- **PTR status:** both receipt printers report `device=4` (hardware error) on
  the test core. The probe includes the first eight PTR status words as
  `ptr.raw`. Compare with WMI before treating this as a real printer fault;
  if WMI says OK, the NCR PTR status prefix needs investigation.
- **Cassette IDs:** the probe reuses the template's GRG-style IDs where the
  denomination matches. A site with different denominations gets `CU<n>`
  keys, which the existing template does not chart. A neutral NCR template
  is the follow-up.
- **Per-cassette triggers on 303 failure:** if `WFSGetInfo 303` fails, the
  probe emits no cassettes and the template's dependent items fall back to
  `0`. The main Out Of Cash trigger uses `total_amount=-1` and does not fire,
  but the per-cassette empty triggers may. Treat a 303 failure as a data
  fault to investigate.

## Rollback

Comment out the NCR `atm.xfs.cdm` line, restore the GRG line if this is a GRG
host, and restart the agent. No ATM configuration is changed by the probe.
