# NCR XFS access check

> **Purpose:** prove whether the NCR test ATM exposes CEN/XFS to our
> collector, before building an NCR XFS probe. Read-only: no writes, no
> dispense, no reset, no configuration change.

## Background

The NCR test core was observed with **APTRA XFS Service Provider 3.02**
(`HKLM\SOFTWARE\XFS\SERVICE_PROVIDERS\CDM`), so the XFS layer exists. What is
not yet proven is whether a non-ATM process (the Zabbix agent account) can
open the logical services and read cash-unit data.

The GRG probe uses the same standard API (`msxfs.dll` → `WFSStartUp` /
`WFSOpen` / `WFSGetInfo`). NCR is not a different XFS universe; it is the same
CEN/XFS API with a different service provider and older object definitions.

## Result so far — NCR test core, 2026-10-29

The test core is **32-bit Windows** (`OS arch: x86`, PowerShell 2.0). The
32-bit run succeeded:

- `WFSStartUp(0x00030203) -> 0` — `XFS Manager, NCR FSG Ltd.`
- `CurrencyDispenser1` (provider `CDM`, described as `XFS MDM Service Provider`)
  `WFSOpen -> 0`, `WFSGetInfo 301 -> 0`, `WFSGetInfo 303 -> 0`
- 5 cash units: ETB 50 × 12, ETB 100 × 10, ETB 100 × 9, ETB 200 × 0,
  and a reject cassette with 3 notes
- IDC, PTR, PIN and SIU also opened and returned status
- `msxfs.dll` is **not** in `System32` or `SysWOW64`; it is loaded from
  `C:\Program Files\Common Files\NCR\msxfs.dll` through the system `PATH`
- the same run under `LocalSystem` (the Zabbix agent account) also reached
  `WFSStartUp -> 0`, opened `CurrencyDispenser1` and returned five cash units
- `BillDispenser1/2`, `CoinDispenser1`, `CurrencyDispenser2` and
  `MediaDispenser2` return `303 -> -13`; they are subordinate or alias
  logical services, not the primary dispenser. `CRDCardUnit1`,
  `ElectronicDocumentProcessor1` and `SignatureScanner1` are not configured
  (`WFSOpen -> -47`).

The raw unit bytes match the 64-byte XFS cash-unit layout used by the GRG
probe (`usNumber`, `usType`, name pointer, 5-byte unit ID, 3-byte currency,
`ulValues`, `ulInitialCount`, `ulCount`, `ulRejectCount`, ...), so the existing
cash-unit struct can be reused. Only service selection and cassette naming need
to be NCR-aware.

## Run it

Copy `scripts/ncr-xfs-recon.ps1` to `C:\monitor\` on the NCR test ATM
(`10.41.30.114` / `172.23.248.4`). On the 32-bit test core, run:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File C:\monitor\ncr-xfs-recon.ps1 -Bit 32
```

On a 64-bit core, try both bitnesses: `System32\WindowsPowerShell` for 64-bit
and `SysWOW64\WindowsPowerShell` for 32-bit. Each run writes
`ncr-xfs-recon-<host>-<timestamp>.txt` to the Desktop (or `%TEMP%` when no
Desktop is available).

### Run as the Zabbix agent account

The agent service on the test core runs as `LocalSystem`
(`SERVICE_START_NAME : LocalSystem`). XFS access is often account/session
sensitive, so test that account before building the collector. A one-off
scheduled task avoids restarting the agent or storing a password:

```cmd
schtasks /Create /TN NCRXfsRecon /SC ONCE /ST 23:59 /RU SYSTEM /RL HIGHEST ^
  /TR "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\monitor\ncr-xfs-recon.ps1 -Bit 32 -TimeoutSec 120" /F
schtasks /Run /TN NCRXfsRecon
```

The task has no console; read the report path it writes. Under `LocalSystem`
the Desktop is the system-profile Desktop when it exists, otherwise
`C:\Windows\Temp`. Find it with:

```cmd
dir /s /b C:\Windows\Temp\ncr-xfs-recon-*.txt
dir /s /b C:\Windows\system32\config\systemprofile\Desktop\ncr-xfs-recon-*.txt
```

Check the outcome with:

```cmd
schtasks /Query /TN NCRXfsRecon /V /FO LIST
type <the report path printed above>
schtasks /Delete /TN NCRXfsRecon /F
```

The report must show `User: nt authority\system`, `WFSStartUp -> 0` and a CDM
`WFSGetInfo 303 -> 0` with `count > 0`. If `WFSStartUp` fails only under
`LocalSystem`, the collector cannot run as the agent service; stop and
investigate before writing it.

## If it looks stuck

The script prints a timestamped line before every risky call and writes the
same line to the transcript. The three calls that can block are:

| Step | Why it can hang | What to do |
|---|---|---|
| Zabbix service lookup | the old WMI query could hang on a sick WMI repository | removed; the script now uses `Get-Service` + `sc.exe qc` |
| `Add-Type` | compiles the C# probe on old .NET; 10–30s is normal | wait; the next line says `Add-Type complete` |
| `WFSStartUp` | the CEN/XFS call has **no timeout**; a stuck NCR service provider can block forever | the watchdog kills the process after `-TimeoutSec` (default 180s) |

First run the inventory only, with no XFS calls:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File C:\monitor\ncr-xfs-recon.ps1 -Bit 32 -SkipXfs
```

If that completes, the PowerShell/registry side is healthy and the hang is in
the XFS call. Then run the real probe with a shorter watchdog:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File C:\monitor\ncr-xfs-recon.ps1 -Bit 32 -TimeoutSec 120
```

If the console prints `NCR XFS recon starting...` several times and then
returns, the old script was relaunching itself: a 32-bit process asking for
64-bit PowerShell through `System32` gets redirected back to 32-bit
PowerShell. The current script uses `Sysnative` for that transition, refuses
to relaunch a child a second time, and falls back to 32-bit on a 32-bit
Windows install. If this ATM is 32-bit, run `-Bit 32` explicitly.

If it times out, two files matter:

- `ncr-xfs-recon-<host>-<timestamp>.txt` — the transcript; its **last line**
  is the call that was running.
- `ncr-xfs-recon-<host>-<timestamp>.timeout.txt` — written by the watchdog.

Closing the PowerShell window or pressing `Ctrl+C` is safe: the probe only
opens services and reads status, it never dispenses, resets or changes
configuration.

One Windows-specific false alarm: if the console title starts with
**Select**, a QuickEdit selection has paused output. Press `Esc` and the
script continues.

## What proves what

| Result | Meaning |
|---|---|
| `msxfs.dll` found in `System32` or `SysWOW64` | XFS manager present; note the bitness |
| `PATH C:\...\msxfs.dll` or a loaded-module line | the manager is outside the system directories; the collector must load it the same way |
| `WFSStartUp -> 0` | the manager is usable from this process/account |
| `WFSOpen -> 0` for CDM | the ATM's cash-dispenser logical service is accessible |
| `WFSGetInfo 301 -> 0` | CDM device status is readable |
| `WFSGetInfo 303 -> 0` with `count > 0` | cash-unit records are readable — cassette counts are available |
| `WFSOpen` non-zero | likely `ACCESS_DENIED` or an unconfigured service; try the Zabbix agent account |
| `WFSStartUp` fails in one bitness only | use the other bitness for the collector |
| `WFSStartUp` fails in both | XFS is present but not usable by our process; stop and investigate |

## What to send back

The full report file from each run. In particular:

- negotiated XFS version (`version=0x...`)
- logical services table (name, class, provider)
- `WFSOpen` and `WFSGetInfo` hResults
- the `cash-unit list` and `unit[n]` lines
- the `PATH`/loaded-module line for `msxfs.dll`

The 2026-10-29 run met the CDM bar under both `ssdbsup` and
`nt authority\system`: `WFSOpen -> 0`, `WFSGetInfo 303 -> 0`, five cash units.
The collector may now run inside the Zabbix agent service; see
`docs/ncr-xfs-collector.md`.

## Version warning

XFS 3.02 predates the 3.20/3.30 object definitions used by the GRG probe. The
recon deliberately dumps raw buffer bytes rather than trusting a struct. On the
NCR test core those bytes matched the 64-byte cash-unit layout exactly, so the
GRG struct can be reused there — but verify the bytes on each new NCR model
before assuming the layout. SIU sensor offsets are less certain: probe them
with the running service before mapping supervisor/door/tamper states.
