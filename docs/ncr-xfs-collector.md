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

## Service selection

- **CDM:** `CurrencyDispenser1` is selected by a successful `303` with cash
  units, not by the first service with a readable `301`.
- **IDC:** `IDCardUnit1` is tried first, then `IDCardUnit2`. On the test core
  unit 1 can return `-48` while the ATM application is running. Unit 2 opens
  and returns status but reports `fwDevice=3` (no device) — confirmed on
  2026-10-29 — so the probe treats it as unusable and the portal falls back
  to WMI.
- **PIN:** `Pinpad1` is the only configured PIN service on the test core; if
  it returns `-48`, there is no XFS alternate and the portal uses WMI.
- **PTR / SIU:** `ReceiptPrinter1` and `SIU` are the verified services.

Each peripheral object also carries `candidates` (the services considered)
and `attempts` (the `WFSOpen`/`WFSGetInfo` result for each). These are
diagnostic fields; the template ignores them. They answer whether the probe
tried an alternate such as `IDCardUnit2` and what it returned.

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

It must print exactly one JSON line. The JSON carries `probe_version` and
`probe_path`; if `probe_path` is not `C:\monitor\ncr-xfs-probe.ps1`, the
UserParameter is pointing at a different file. Fix that before debugging
anything else.

PowerShell 2.0 has no `ConvertFrom-Json`, so check the expected fields with
`-match`:

```powershell
$out = & C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass -File C:\monitor\ncr-xfs-probe.ps1

$out
$out -match '"service":"CurrencyDispenser1"'
$out -match '"total_notes":31'
$out -match '"total_amount":2509'
$out -match '"0050A"'
$out -match '"Rej01"'
```

On the test core the expected cash mapping is:

| NCR unit | Denomination | Count | Template ID |
|---|---|---|---|
| 1 | ETB 50 | 12 | `0050A` |
| 2 | ETB 100 | 10 | `0100A` |
| 3 | ETB 100 (vendor reports 101) | 9 | `0100B` |
| 4 | ETB 200 | 0 | `0200A` |
| 5 | reject | 3 | `Rej01` |

The counts change as the ATM dispenses; the structure and service name are
the stable checks. NCR reports unit 3 as `ulValues=101`; the probe maps it to
`0100B` by physical unit number, not by denomination, and keeps the vendor's
101 in the cash value.

The test core had 2,509 ETB across 31 notes, so the template's
`< 50,000 ETB` low-cash trigger and the `0200A` cassette-empty trigger will
fire during the pilot. Both are correct readings, not collector faults.

## Verify from the Zabbix server

```bash
zabbix_get -s <ATM_IP> -t 30 -k atm.xfs.cdm
```

The value is the same single-line JSON. Until the template is linked, the
master item is enough to prove collection.

Check `probe_version` and `probe_path` first. If they are missing or show an
old path, the ATM is running a stale copy and no amount of probe debugging
will explain the result.

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
- **Cassette status codes:** NCR's `usStatus` values do not follow the GRG
  value map (unit 2 reports 10 notes with status 6, which the template labels
  "Missing"). Use the counts; the portal and cash forecast use counts.
- **PTR status:** both receipt printers report `device=4` (hardware error) and
  `paper=2` (paper out) on the test core. The probe includes the first eight
  PTR status words as `ptr.raw`. Compare with WMI before treating this as a
  real printer fault; if WMI says OK, the NCR PTR status prefix needs
  investigation.
- **IDC/PIN `-48`:** `WFS_ERR_INVALID_SERVPROV` means the service provider is
  unavailable, often because the ATM application holds the card reader or PIN
  pad open. The probe reports `open_hresult=-48` and `device=255` (not
  queried) so Zabbix does not keep a stale status; the portal falls back to
  the WMI card-reader/PIN rows for those two chips. The CDM and SIU data are
  unaffected.
  On the 2026-10-29 15:25 recon, `IDCardUnit1` returned `-48` but
  `IDCardUnit2` opened and returned status. The live probe then showed
  `IDCardUnit2` with `device=3` (no device), so the real reader is unit 1
  and the ATM application owns it. The probe intentionally keeps the WMI
  fallback rather than showing a false red "No device" chip. There is no
  working alternate for `Pinpad1`, so the PIN chip stays on WMI.
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
