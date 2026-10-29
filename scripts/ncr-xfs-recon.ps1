#Requires -Version 2.0
<#
.SYNOPSIS
  Read-only check: can this NCR ATM's CEN/XFS layer be used by the collector?

.DESCRIPTION
  Answers three questions in one run:

    1. Is the NCR APTRA XFS stack present (msxfs.dll + service providers)?
    2. Does WFSStartUp succeed from this process and account?
    3. Can we WFSOpen the CDM/IDC/PTR/PIN/SIU logical services and read
       WFSGetInfo status + cash-unit data?

  It performs NO writes: no WFSExecute, no dispense, no reset, no config
  change. Safe to run on a live ATM. The only side effect is a transcript
  file on the Desktop.

  Run it twice, once in each bitness, because NCR's msxfs.dll may be x86 or
  x64:

    C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe ^
      -NoProfile -ExecutionPolicy Bypass -File C:\monitor\ncr-xfs-recon.ps1 -Bit 64

    C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe ^
      -NoProfile -ExecutionPolicy Bypass -File C:\monitor\ncr-xfs-recon.ps1 -Bit 32

  Then run the successful one again as the account the Zabbix agent uses.
  The report prints "User:" so the account is unambiguous.

.PARAMETER Bit
  auto (current process), 32, or 64. 32/64 relaunches the script in the
  requested PowerShell if needed.

.NOTES
  NCR test core was observed with APTRA XFS SP 3.02
  (HKLM\SOFTWARE\XFS\SERVICE_PROVIDERS\CDM). 3.02 uses older object
  definitions than GRG's 3.20/3.30, so this script deliberately does not
  assume the newer cash-unit struct layout. It reports hResults and raw
  buffer bytes for offline analysis.
#>
param(
  [ValidateSet('auto', '32', '64')]
  [string]$Bit = 'auto',
  # Hard limit for the whole run. WFSStartUp has no API-level timeout, and
  # a broken NCR XFS manager can block inside the native call forever. The
  # watchdog kills this process after N seconds and leaves a timeout file.
  [int]$TimeoutSec = 180,
  # Registry/DLL inventory only. Use this to prove the script runs on the
  # ATM before touching the XFS manager.
  [switch]$SkipXfs
)

$ErrorActionPreference = 'SilentlyContinue'

# Printed before anything else so a silent window means PowerShell itself
# did not start the script (or console QuickEdit has paused it).
Write-Host "NCR XFS recon starting... (read-only; Ctrl+C is safe)"

# ── Bitness ────────────────────────────────────────────────────────────────
# [IntPtr]::Size works on PS 2.0/.NET 2.0; [Environment]::Is64BitProcess
# requires .NET 4 and is not available on the NCR test core.
$is64 = ([IntPtr]::Size -eq 8)
$os64 = (($env:PROCESSOR_ARCHITECTURE -match '^(AMD64|ARM64|IA64)$') -or
         ($env:PROCESSOR_ARCHITEW6432 -match '^(AMD64|ARM64|IA64)$'))

if ($Bit -ne 'auto') {
  $want64 = ($Bit -eq '64')

  # On a 32-bit Windows install there is no 64-bit PowerShell at all.
  # Say so and continue in 32-bit instead of relaunching forever.
  if ($want64 -and -not $os64) {
    Write-Output "This is a 32-bit Windows installation; 64-bit PowerShell is unavailable."
    Write-Output "Continuing in 32-bit. Use -Bit 32 to make that explicit."
    $want64 = $false
  }

  if ($env:NCR_XFS_RECON_CHILD -eq '1') {
    # A relaunch already happened. Never relaunch again, even if the child
    # still reports the wrong bitness; that is how the old script looped.
    if ($want64 -ne $is64) {
      Write-Output "WARNING: requested $Bit-bit but this process is $([IntPtr]::Size * 8)-bit; continuing anyway."
    }
  } elseif ($want64 -ne $is64) {
    if ($want64) {
      if ($env:PROCESSOR_ARCHITEW6432) {
        # 32-bit process on 64-bit Windows: System32 is redirected to
        # SysWOW64. Sysnative is the only way to reach the real 64-bit exe.
        $exe = Join-Path $env:WINDIR 'Sysnative\WindowsPowerShell\v1.0\powershell.exe'
      } else {
        $exe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
      }
    } else {
      $exe = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    }
    if (Test-Path $exe) {
      $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $MyInvocation.MyCommand.Path, '-Bit', $Bit,
        '-TimeoutSec', $TimeoutSec)
      if ($SkipXfs) { $childArgs += '-SkipXfs' }
      $env:NCR_XFS_RECON_CHILD = '1'
      & $exe @childArgs
      exit $LASTEXITCODE
    }
    Write-Output "Requested $Bit-bit PowerShell not found: $exe"
  }
}
$is64 = ([IntPtr]::Size -eq 8)

# ── Transcript ─────────────────────────────────────────────────────────────
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop -or -not (Test-Path $desktop)) { $desktop = $env:TEMP }
if (-not $desktop -or -not (Test-Path $desktop)) { $desktop = 'C:\Windows\Temp' }
$report = Join-Path $desktop ("ncr-xfs-recon-{0}-{1}.txt" -f $env:COMPUTERNAME, (Get-Date -Format 'yyyyMMdd-HHmmss'))
try { Stop-Transcript | Out-Null } catch {}
$transcriptOn = $true
try { Start-Transcript -Path $report -Force | Out-Null } catch {
  $transcriptOn = $false
  Write-Output "Transcript unavailable ($report): $($_.Exception.Message)"
  Write-Output "Copy the console output instead."
}

# Timestamped progress. Write-Output feeds the transcript; the explicit
# flush makes the console show each step before a native call can block.
function Write-Step([string]$text) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $text
  Write-Output $line
  try { [Console]::Out.Flush() } catch {}
}

$timeoutFile = [IO.Path]::ChangeExtension($report, '.timeout.txt')
$watchdog = $null
$alive = $null
if ($TimeoutSec -gt 0) {
  $alive = Join-Path $env:TEMP ("ncr-xfs-recon-{0}.alive" -f $PID)
  Set-Content -Path $alive -Value (Get-Date -Format o) -Encoding ASCII
  try {
    $watchdog = Start-Job -ScriptBlock {
      param($parentPid, $alivePath, $timeoutPath, $seconds)
      Start-Sleep -Seconds $seconds
      if (Test-Path $alivePath) {
        $msg = "TIMEOUT after $seconds seconds at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). The script was killed inside a native call; the transcript's last line shows the step."
        Set-Content -Path $timeoutPath -Value $msg -Encoding ASCII
        Stop-Process -Id $parentPid -Force
      }
    } -ArgumentList $PID, $alive, $timeoutFile, $TimeoutSec
    Write-Step "Watchdog: hard timeout ${TimeoutSec}s (timeout file: $timeoutFile)"
  } catch {
    Write-Step "Watchdog unavailable: $($_.Exception.Message)"
  }
}

function Stop-ReconWatchdog {
  if ($alive) { Remove-Item $alive -Force -ErrorAction SilentlyContinue }
  if ($watchdog) {
    Stop-Job $watchdog -ErrorAction SilentlyContinue
    Remove-Job $watchdog -ErrorAction SilentlyContinue
  }
}

Write-Output "NCR XFS ACCESS CHECK - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Output "Host: $env:COMPUTERNAME"
Write-Output "User: $(whoami)"
Write-Output "PowerShell: $($PSVersionTable.PSVersion)   Process64: $is64   OS arch: $env:PROCESSOR_ARCHITECTURE"
Write-Output "Report: $report"
Write-Output ""

# ── Zabbix agent account ───────────────────────────────────────────────────
Write-Step "===== ZABBIX AGENT SERVICE ====="
$zabbixFound = $false
# Get-WmiObject can hang for minutes on an unhealthy ATM WMI repository.
# Get-Service + sc.exe qc returns the same StartName/PathName without WMI.
foreach ($svc in @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)zabbix' })) {
  $zabbixFound = $true
  Write-Output ("Name={0}  State={1}" -f $svc.Name, $svc.Status)
  $qc = @(& sc.exe qc $svc.Name 2>$null)
  foreach ($line in $qc) {
    if ($line -match '\S') { Write-Output ("  " + $line.Trim()) }
  }
}
if (-not $zabbixFound) { Write-Output "No Zabbix service found (agent may be running as a console process)." }
Write-Output ""

# ── PE machine (x86/x64) ───────────────────────────────────────────────────
function Get-PeMachine([string]$path) {
  try {
    $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $br = New-Object IO.BinaryReader($fs)
    $fs.Position = 0x3C
    $peOffset = $br.ReadInt32()
    $fs.Position = $peOffset + 4
    $machine = $br.ReadUInt16()
    $br.Close(); $fs.Close()
    switch ($machine) {
      0x014c { 'x86' }
      0x8664 { 'x64' }
      0xaa64 { 'ARM64' }
      default { '0x{0:X4}' -f $machine }
    }
  } catch { 'unknown' }
}

Write-Step "===== MSXFS.DLL ====="
foreach ($dll in @(
  'C:\Windows\System32\msxfs.dll',
  'C:\Windows\SysWOW64\msxfs.dll'
)) {
  if (Test-Path $dll) {
    $f = Get-Item $dll
    Write-Output ("FOUND {0}  size={1}  machine={2}  version={3}" -f `
      $f.FullName, $f.Length, (Get-PeMachine $f.FullName), $f.VersionInfo.FileVersion)
  } else {
    Write-Output "MISSING $dll"
  }
}
# WFSStartUp can load msxfs.dll from the NCR install directory through
# PATH even when it is absent from System32/SysWOW64. The collector must
# load the same file, so record where the loader actually found it.
$onPath = @(& where.exe msxfs.dll 2>$null)
if ($onPath.Count -gt 0) {
  foreach ($p in $onPath) { Write-Output "PATH $p" }
} else {
  Write-Output "msxfs.dll is not on PATH; the loaded-module line after WFSStartUp is authoritative."
}
Write-Output ""

# ── XFS service providers ──────────────────────────────────────────────────
Write-Step "===== XFS SERVICE PROVIDERS ====="
foreach ($root in @(
  'HKLM:\SOFTWARE\XFS\SERVICE_PROVIDERS',
  'HKLM:\SOFTWARE\WOW6432Node\XFS\SERVICE_PROVIDERS'
)) {
  if (-not (Test-Path $root)) {
    Write-Output "MISSING $root"
    continue
  }
  Write-Output "ROOT $root"
  foreach ($k in @(Get-ChildItem $root -ErrorAction SilentlyContinue)) {
    if ($k.PSChildName -notmatch '(?i)^(CDM|IDC|PTR|PIN|SIU|CIM|TTU)') { continue }
    Write-Output ("  {0}" -f $k.PSChildName)
    $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
    $p.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object {
      Write-Output ("    {0} = {1}" -f $_.Name, $_.Value)
    }
  }
}
Write-Output ""

# ── Logical services (HKLM, HKCU, every loaded HKU hive) ───────────────────
function Get-XfsLogicalServices {
  $roots = @(
    'HKLM:\SOFTWARE\XFS\LOGICAL_SERVICES',
    'HKLM:\SOFTWARE\WOW6432Node\XFS\LOGICAL_SERVICES',
    'HKCU:\XFS\LOGICAL_SERVICES',
    'Registry::HKEY_USERS\.DEFAULT\XFS\LOGICAL_SERVICES'
  )
  foreach ($h in @(Get-ChildItem 'Registry::HKEY_USERS' -ErrorAction SilentlyContinue)) {
    $roots += ("Registry::HKEY_USERS\{0}\XFS\LOGICAL_SERVICES" -f $h.PSChildName)
  }

  $seen = @{}
  foreach ($root in $roots) {
    if (-not (Test-Path $root)) { continue }
    foreach ($k in @(Get-ChildItem $root -ErrorAction SilentlyContinue)) {
      $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
      $name = $k.PSChildName
      $class = [string]$p.Class
      $type = [string]$p.Type
      $provider = [string]$p.Provider
      # The same logical service is mirrored in .DEFAULT and S-1-5-18.
      # One probe per name/class/provider is enough; the first hive wins.
      $key = "$name|$class|$provider"
      if (-not $seen.ContainsKey($key)) {
        $seen[$key] = $true
        New-Object PSObject -Property @{
          Name = $name; Class = $class; Type = $type; Provider = $provider; Hive = $root
        }
      }
    }
  }
}

$services = @(Get-XfsLogicalServices)
Write-Step "===== XFS LOGICAL SERVICES ====="
if ($services.Count -eq 0) {
  Write-Output "No logical services found under HKLM/HKCU/HKU."
} else {
  Write-Output ($services | Sort-Object Class, Name | Format-Table Class, Name, Type, Provider, Hive -AutoSize | Out-String)
}
Write-Output ""

# ── XFS P/Invoke (minimal, version-agnostic) ───────────────────────────────
if ($SkipXfs) {
  Write-Step "SkipXfs requested: stopping before Add-Type/WFSStartUp."
  Stop-ReconWatchdog
  try { Stop-Transcript | Out-Null } catch {}
  exit 0
}

Write-Step "Add-Type C# probe (first run on old .NET can take 10-30s)"
if (-not ('NcrXfsProbe' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential, Pack = 1, CharSet = CharSet.Ansi)]
public struct NcrXfsVersion
{
    public ushort wVersion;
    public ushort wLowVersion;
    public ushort wHighVersion;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)] public string szDescription;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)] public string szSystemStatus;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsSystemTime
{
    public ushort wYear;
    public ushort wMonth;
    public ushort wDayOfWeek;
    public ushort wDay;
    public ushort wHour;
    public ushort wMinute;
    public ushort wSecond;
    public ushort wMilliseconds;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsResult
{
    public uint RequestID;
    public ushort hService;
    public NcrXfsSystemTime tsTimestamp;
    public int hResult;
    public uint u;
    public IntPtr lpBuffer;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCdmCuInfo
{
    public ushort usTellerID;
    public ushort usCount;
    public IntPtr lppList;
}

public static class NcrXfsProbe
{
    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSStartUp(uint dwVersionsRequired, ref NcrXfsVersion lpWFSVersion);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSCreateAppHandle(out IntPtr lphApp);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSDestroyAppHandle(IntPtr hApp);

    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSOpen(string lpszLogicalName, IntPtr hApp, string lpszAppID,
        uint dwTraceLevel, uint dwTimeOut, uint dwSrvcVersionsRequired,
        ref NcrXfsVersion lpSrvcVersion, ref NcrXfsVersion lpSPIVersion, out ushort lphService);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSGetInfo(ushort hService, uint dwCategory, IntPtr lpQueryDetails,
        uint dwTimeOut, out IntPtr lppResult);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSFreeResult(IntPtr lpResult);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSClose(ushort hService);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSCleanUp();
}
'@
}
Write-Step "Add-Type complete"

function New-XfsVersion {
  $v = New-Object NcrXfsVersion
  $v.szDescription = ''
  $v.szSystemStatus = ''
  return $v
}

# ── WFSStartUp ─────────────────────────────────────────────────────────────
Write-Step "===== WFSSTARTUP (no API timeout; watchdog armed) ====="
$startupVersion = $null
foreach ($v in @(0x00030203, 0x00030000, 0x00030003, 0x00031000, 0x00032000, 0x00033000, 0x00010001)) {
  $ver = New-XfsVersion
  $hr = -1
  Write-Step ("Calling WFSStartUp 0x{0:X8}..." -f $v)
  try {
    $hr = [NcrXfsProbe]::WFSStartUp([uint32]$v, [ref]$ver)
  } catch {
    Write-Output ("0x{0:X8} -> THREW: {1}" -f $v, $_.Exception.Message)
    continue
  }
  Write-Output ("0x{0:X8} -> {1} (0x{1:X8})  version=0x{2:X4} low=0x{3:X4} high=0x{4:X4}  {5}" -f `
    $v, $hr, $ver.wVersion, $ver.wLowVersion, $ver.wHighVersion, $ver.szDescription)
  if ($hr -eq 0) { $startupVersion = $v; break }
}
if ($null -eq $startupVersion) {
  Write-Output "RESULT: WFSStartUp did not succeed from this process/account."
  Stop-ReconWatchdog
  try { Stop-Transcript | Out-Null } catch {}
  exit 1
}
Write-Output ""

# Which msxfs.dll did the loader actually use? System32/SysWOW64 may both
# be empty while PATH resolves to the NCR install directory.
Write-Step "Loaded msxfs module(s):"
try {
  foreach ($m in ([System.Diagnostics.Process]::GetCurrentProcess()).Modules) {
    if ($m.ModuleName -match '(?i)^msxfs\.dll$') {
      Write-Output ("  {0}  size={1}" -f $m.FileName, $m.ModuleMemorySize)
    }
  }
} catch {
  Write-Output ("  module list unavailable: {0}" -f $_.Exception.Message)
}
Write-Output ""

# ── App handle ─────────────────────────────────────────────────────────────
[IntPtr]$hApp = [IntPtr]::Zero
$appHr = -1
try { $appHr = [NcrXfsProbe]::WFSCreateAppHandle([ref]$hApp) } catch {
  Write-Output "WFSCreateAppHandle THREW: $($_.Exception.Message)"
}
Write-Output ("WFSCreateAppHandle -> {0} (0x{0:X8})  hApp={1}" -f $appHr, $hApp)
if ($appHr -ne 0) { $hApp = [IntPtr]::Zero }
Write-Output ""

# ── Open services and query read-only information ─────────────────────────
$categories = @{
  'CDM' = @(301, 303)   # status + cash unit info
  'IDC' = @(201)        # card reader status
  'PTR' = @(101)        # printer status
  'PIN' = @(401)        # pin pad status
  'SIU' = @(801)        # sensors / doors / operator switch
}

Write-Step "===== WFSOPEN / WFSGETINFO ====="
foreach ($svc in $services) {
  if ($svc.Class -notmatch '^(CDM|IDC|PTR|PIN|SIU)$') { continue }

  Write-Step ("--- {0}  class={1}  provider={2}" -f $svc.Name, $svc.Class, $svc.Provider)
  $svcVer = New-XfsVersion
  $spiVer = New-XfsVersion
  [uint16]$hService = 0
  $openHr = -1
  Write-Step ("  Calling WFSOpen({0})..." -f $svc.Name)
  try {
    $openHr = [NcrXfsProbe]::WFSOpen($svc.Name, $hApp, 'ZabbixATMRecon', 0, 3000,
      [uint32]$startupVersion, [ref]$svcVer, [ref]$spiVer, [ref]$hService)
  } catch {
    Write-Output ("  WFSOpen THREW: {0}" -f $_.Exception.Message)
    continue
  }
  Write-Output ("  WFSOpen -> {0} (0x{0:X8})  service={1}  provider={2}" -f `
    $openHr, $svcVer.szDescription, $spiVer.szDescription)
  if ($openHr -ne 0) { continue }

  foreach ($cat in $categories[$svc.Class]) {
    $resPtr = [IntPtr]::Zero
    $hr = -1
    Write-Step ("  Calling WFSGetInfo {0}..." -f $cat)
    try {
      $hr = [NcrXfsProbe]::WFSGetInfo($hService, [uint32]$cat, [IntPtr]::Zero, 3000, [ref]$resPtr)
    } catch {
      Write-Output ("  WFSGetInfo {0} THREW: {1}" -f $cat, $_.Exception.Message)
      continue
    }
    if ($hr -ne 0 -or $resPtr -eq [IntPtr]::Zero) {
      Write-Output ("  WFSGetInfo {0} -> {1} (0x{1:X8})" -f $cat, $hr)
      continue
    }

    $res = [Runtime.InteropServices.Marshal]::PtrToStructure($resPtr, [type][NcrXfsResult])
    Write-Output ("  WFSGetInfo {0} -> 0  result.hResult={1} (0x{1:X8})  buffer=0x{2:X8}" -f `
      $cat, $res.hResult, $res.lpBuffer.ToInt64())

    if ($res.hResult -eq 0 -and $res.lpBuffer -ne [IntPtr]::Zero) {
      # Never read past what the manager allocated. PIN status is 4 bytes;
      # the CDM 303 buffer is only its header; cash-unit structs are large
      # enough for 64 bytes.
      $copyLen = 16
      if ($svc.Class -eq 'PIN') { $copyLen = 4 }
      if ($svc.Class -eq 'CDM' -and $cat -eq 303) { $copyLen = 4 + [IntPtr]::Size }
      $bytes = New-Object byte[] $copyLen
      [Runtime.InteropServices.Marshal]::Copy($res.lpBuffer, $bytes, 0, $copyLen)
      Write-Output ("    first{0}: {1}" -f $copyLen, (($bytes | ForEach-Object { $_.ToString('X2') }) -join ' '))

      # CDM cash units: read only the version-stable header and the raw
      # head of each unit. Full struct parsing waits for the negotiated
      # XFS version (3.02 vs 3.20/3.30).
      if ($svc.Class -eq 'CDM' -and $cat -eq 303) {
        $info = [Runtime.InteropServices.Marshal]::PtrToStructure($res.lpBuffer, [type][NcrXfsCdmCuInfo])
        Write-Output ("    cash-unit list: teller={0} count={1} ptr=0x{2:X8}" -f `
          $info.usTellerID, $info.usCount, $info.lppList.ToInt64())
        $limit = [int]$info.usCount
        if ($limit -gt 20) { $limit = 20 }
        for ($i = 0; $i -lt $limit; $i++) {
          $unitPtr = [Runtime.InteropServices.Marshal]::ReadIntPtr($info.lppList, $i * [IntPtr]::Size)
          if ($unitPtr -eq [IntPtr]::Zero) { continue }
          $number = [Runtime.InteropServices.Marshal]::ReadInt16($unitPtr, 0)
          $type = [Runtime.InteropServices.Marshal]::ReadInt16($unitPtr, 2)
          $ub = New-Object byte[] 64
          [Runtime.InteropServices.Marshal]::Copy($unitPtr, $ub, 0, 64)
          Write-Output ("    unit[{0}] ptr=0x{1:X8} number={2} type={3}" -f `
            $i, $unitPtr.ToInt64(), $number, $type)
          Write-Output ("      first64: {0}" -f (($ub | ForEach-Object { $_.ToString('X2') }) -join ' '))
        }
      } else {
        # These USHORTs are stable across XFS versions: device, media/
        # safe-door, paper/dispenser, etc. Raw hex above remains authoritative.
        $u0 = [Runtime.InteropServices.Marshal]::ReadInt16($res.lpBuffer, 0)
        $u1 = [Runtime.InteropServices.Marshal]::ReadInt16($res.lpBuffer, 2)
        Write-Output ("    u16[0..1] = {0}, {1}" -f $u0, $u1)
        if ($copyLen -ge 8) {
          $u2 = [Runtime.InteropServices.Marshal]::ReadInt16($res.lpBuffer, 4)
          $u3 = [Runtime.InteropServices.Marshal]::ReadInt16($res.lpBuffer, 6)
          Write-Output ("    u16[2..3] = {0}, {1}" -f $u2, $u3)
        }
      }
    }
    [void][NcrXfsProbe]::WFSFreeResult($resPtr)
  }
  [void][NcrXfsProbe]::WFSClose($hService)
}

Write-Step "===== CLEANUP ====="
if ($hApp -ne [IntPtr]::Zero) { [void][NcrXfsProbe]::WFSDestroyAppHandle($hApp) }
[void][NcrXfsProbe]::WFSCleanUp()

Write-Output ""
Write-Output "===== HOW TO READ THIS ====="
Write-Output "GOOD:  WFSStartUp -> 0 and CDM WFSOpen -> 0 and WFSGetInfo 301/303 -> 0."
Write-Output "GOOD:  cash-unit count > 0 and unit first64 lines are present."
Write-Output "DENIED: WFSOpen returns a non-zero hResult (often ACCESS_DENIED)."
Write-Output "        Run the same script as the Zabbix agent account."
Write-Output "MISSING: only one bitness finds msxfs.dll. Use that bitness for the collector."
Write-Output "VERSION: report the negotiated version; 3.02 uses older struct layouts"
Write-Output "        than the GRG 3.20/3.30 probe, so parsing must be version-aware."
Write-Output ""

Stop-ReconWatchdog
try { Stop-Transcript | Out-Null } catch {}
