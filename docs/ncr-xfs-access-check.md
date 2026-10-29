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

## Run it

Copy `scripts/ncr-xfs-recon.ps1` to `C:\monitor\` on the NCR test ATM
(`10.41.30.114` / `172.23.248.4`). Run it in both bitnesses:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File C:\monitor\ncr-xfs-recon.ps1 -Bit 64

C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File C:\monitor\ncr-xfs-recon.ps1 -Bit 32
```

Each run writes `ncr-xfs-recon-<host>-<timestamp>.txt` to the Desktop (or
`%TEMP%` when no Desktop is available).

Then repeat the run that found the DLL **as the account the Zabbix agent
uses** (`Get-WmiObject Win32_Service | Where Name -match zabbix`). XFS access
is often account/session sensitive; the report prints `User:` so the result is
unambiguous.

## What proves what

| Result | Meaning |
|---|---|
| `msxfs.dll` found in `System32` or `SysWOW64` | XFS manager present; note the bitness |
| `WFSStartUp -> 0` | the manager is usable from this process/account |
| `WFSOpen -> 0` for CDM | the ATM's cash-dispenser logical service is accessible |
| `WFSGetInfo 301 -> 0` | CDM device status is readable |
| `WFSGetInfo 303 -> 0` with `count > 0` | cash-unit records are readable — cassette counts are available |
| `WFSOpen` non-zero | likely `ACCESS_DENIED`; try the Zabbix agent account |
| `WFSStartUp` fails in one bitness only | use the other bitness for the collector |
| `WFSStartUp` fails in both | XFS is present but not usable by our process; stop and investigate |

## What to send back

The full report file from each run. In particular:

- negotiated XFS version (`version=0x...`)
- logical services table (name, class, provider)
- `WFSOpen` and `WFSGetInfo` hResults
- the `cash-unit list` and `unit[n]` lines

Do not build the NCR collector until the report shows a successful CDM
`WFSOpen` + `WFSGetInfo 303` with a non-zero cash-unit count.

## Version warning

XFS 3.02 predates the 3.20/3.30 object definitions used by the GRG probe. The
recon deliberately dumps raw buffer bytes instead of assuming the newer cash
unit struct layout. Once the negotiated version is known, the collector must
use version-appropriate struct definitions.
