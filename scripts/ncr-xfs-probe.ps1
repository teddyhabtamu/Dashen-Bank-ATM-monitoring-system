#Requires -Version 2.0
<#
.SYNOPSIS
  Read-only NCR APTRA CEN/XFS probe for the Zabbix agent.

.DESCRIPTION
  Emits one line of JSON in the same shape as grg-xfs-probe.ps1 -Json, so the
  existing CEN/XFS template and the portal's xfs_metrics.normalize() consume
  it without a second data contract.

  The NCR test core is 32-bit Windows with PowerShell 2.0 and APTRA XFS 3.02:
    * msxfs.dll is not in System32/SysWOW64; it is loaded from
      C:\Program Files\Common Files\NCR\msxfs.dll through PATH.
    * WFSStartUp(0x00030203) negotiates the NCR FSG manager.
    * The real dispenser is CurrencyDispenser1 (provider CDM). The
      BillDispenser/CoinDispenser/BRMDispenser services are subordinate or
      absent and must not be selected just because WFSGetInfo 301 works.
    * WFSGetInfo 303 on CurrencyDispenser1 returns five 64-byte cash units.

  No writes: WFSExecute is never called, nothing is dispensed, reset or
  reconfigured. Only WFSStartUp, WFSOpen, WFSGetInfo and their cleanup calls.
#>
param(
  # 32 is the safe default for NCR APTRA 3.02. 64 exists for a future core
  # whose XFS manager is x64; auto uses the current process.
  [ValidateSet('auto', '32', '64')]
  [string]$Bit = '32'
)

$ErrorActionPreference = 'SilentlyContinue'

$is64 = ([IntPtr]::Size -eq 8)
$os64 = (($env:PROCESSOR_ARCHITECTURE -match '^(AMD64|ARM64|IA64)$') -or
         ($env:PROCESSOR_ARCHITEW6432 -match '^(AMD64|ARM64|IA64)$'))

# A 64-bit Zabbix agent on 64-bit Windows would start 64-bit PowerShell. The
# NCR XFS manager on this platform is 32-bit, so relaunch once through
# SysWOW64. The env guard makes a failed relaunch terminate instead of loop.
if ($Bit -ne 'auto') {
  $want64 = ($Bit -eq '64')
  if ($want64 -and -not $os64) {
    $want64 = $false
  }
  if ($env:NCR_XFS_PROBE_CHILD -eq '1') {
    # Already relaunched; continue even if the bitness still looks wrong.
  } elseif ($want64 -ne $is64) {
    if ($want64) {
      if ($env:PROCESSOR_ARCHITEW6432) {
        $exe = Join-Path $env:WINDIR 'Sysnative\WindowsPowerShell\v1.0\powershell.exe'
      } else {
        $exe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
      }
    } else {
      $exe = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    }
    if (Test-Path $exe) {
      $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $MyInvocation.MyCommand.Path, '-Bit', $Bit)
      $env:NCR_XFS_PROBE_CHILD = '1'
      & $exe @childArgs
      exit $LASTEXITCODE
    }
  }
}
$is64 = ([IntPtr]::Size -eq 8)

# ── JSON (PowerShell 2.0 has no ConvertTo-Json) ────────────────────────────
function ConvertTo-JsonString([string]$s) {
  if ($null -eq $s) { return '""' }
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.Append('"')
  for ($i = 0; $i -lt $s.Length; $i++) {
    $ch = $s[$i]
    $code = [int]$ch
    if ($ch -eq '"') { [void]$sb.Append('\"') }
    elseif ($ch -eq '\') { [void]$sb.Append('\\') }
    elseif ($code -eq 8) { [void]$sb.Append('\b') }
    elseif ($code -eq 12) { [void]$sb.Append('\f') }
    elseif ($code -eq 10) { [void]$sb.Append('\n') }
    elseif ($code -eq 13) { [void]$sb.Append('\r') }
    elseif ($code -eq 9) { [void]$sb.Append('\t') }
    elseif ($code -lt 32) { [void]$sb.Append(('\u{0:X4}' -f $code)) }
    else { [void]$sb.Append($ch) }
  }
  [void]$sb.Append('"')
  return $sb.ToString()
}

function ConvertTo-JsonValue($v) {
  if ($null -eq $v) { return 'null' }
  if ($v -is [bool]) { if ($v) { return 'true' } return 'false' }
  if ($v -is [string]) { return (ConvertTo-JsonString $v) }
  if ($v -is [System.Collections.IDictionary]) {
    $parts = @()
    foreach ($key in $v.Keys) {
      $parts += ('{0}:{1}' -f (ConvertTo-JsonString ([string]$key)),
        (ConvertTo-JsonValue $v[$key]))
    }
    return '{' + ($parts -join ',') + '}'
  }
  if ($v -is [System.Collections.IEnumerable]) {
    $parts = @()
    foreach ($item in $v) { $parts += (ConvertTo-JsonValue $item) }
    return '[' + ($parts -join ',') + ']'
  }
  if ($v -is [ValueType]) {
    return [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, '{0}', $v)
  }
  return (ConvertTo-JsonString ([string]$v))
}

# ── P/Invoke (only the calls a read-only probe needs) ──────────────────────
if (-not ('NcrXfsCollector' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential, Pack = 1, CharSet = CharSet.Ansi)]
public struct NcrXfsCollectorVersion
{
    public ushort wVersion;
    public ushort wLowVersion;
    public ushort wHighVersion;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)] public string szDescription;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)] public string szSystemStatus;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorSystemTime
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
public struct NcrXfsCollectorResult
{
    public uint RequestID;
    public ushort hService;
    public NcrXfsCollectorSystemTime tsTimestamp;
    public int hResult;
    public uint u;
    public IntPtr lpBuffer;
}

// Only the first four USHORTs are needed. The NCR 301 buffer matched the
// modern WFS_CDM_STATUS prefix: device, safe door, dispenser, stacker.
[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorCdmStatus
{
    public ushort fwDevice;
    public ushort fwSafeDoor;
    public ushort fwDispenser;
    public ushort fwIntermediateStacker;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorCdmCuInfo
{
    public ushort usTellerID;
    public ushort usCount;
    public IntPtr lppList;
}

// The 64-byte XFS cash-unit layout verified against the NCR 303 raw bytes.
[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorCdmCashUnit
{
    public ushort usNumber;
    public ushort usType;
    public IntPtr lpszCashUnitName;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 5)] public byte[] cUnitID;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 3)] public byte[] cCurrencyID;
    public uint ulValues;
    public uint ulInitialCount;
    public uint ulCount;
    public uint ulRejectCount;
    public uint ulMinimum;
    public uint ulMaximum;
    public int bAppLock;
    public ushort usStatus;
    public ushort usNumPhysicalCUs;
    public IntPtr lppPhysical;
    public uint ulDispensedCount;
    public uint ulPresentedCount;
    public uint ulRetractedCount;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorIdcStatus
{
    public ushort fwDevice;
    public ushort fwMedia;
    public ushort fwRetainBin;
    public ushort fwSecurity;
    public ushort usCards;
    public ushort fwChipPower;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorPtrStatus
{
    public ushort fwDevice;
    public ushort fwMedia;
    public ushort fwPaper;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorPinStatus
{
    public ushort fwDevice;
    public ushort fwEncStat;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct NcrXfsCollectorSiuStatus
{
    public ushort fwDevice;
    public ushort fwOperatorSwitch;
}

public static class NcrXfsCollector
{
    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSStartUp(uint dwVersionsRequired, ref NcrXfsCollectorVersion lpWFSVersion);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSCreateAppHandle(out IntPtr lphApp);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSDestroyAppHandle(IntPtr hApp);

    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSOpen(string lpszLogicalName, IntPtr hApp, string lpszAppID,
        uint dwTraceLevel, uint dwTimeOut, uint dwSrvcVersionsRequired,
        ref NcrXfsCollectorVersion lpSrvcVersion, ref NcrXfsCollectorVersion lpSPIVersion, out ushort lphService);

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

function New-XfsVersion {
  $v = New-Object NcrXfsCollectorVersion
  $v.szDescription = ''
  $v.szSystemStatus = ''
  return $v
}

function Convert-Bytes([byte[]]$b) {
  if ($null -eq $b) { return '' }
  return ([Text.Encoding]::ASCII.GetString($b)).TrimEnd([char]0)
}

# ── Logical services ───────────────────────────────────────────────────────
function Get-LogicalServices {
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
      # The hives mirror the same services; one probe per identity is enough.
      $key = "$name|$class|$provider"
      if (-not $seen.ContainsKey($key)) {
        $seen[$key] = $true
        New-Object PSObject -Property @{
          Name = $name; Class = $class; Type = $type; Provider = $provider
        }
      }
    }
  }
}

# ── XFS helpers ────────────────────────────────────────────────────────────
function Open-XfsService([string]$name, [IntPtr]$hApp, [uint32]$version, [uint32]$timeout) {
  $svcVer = New-XfsVersion
  $spiVer = New-XfsVersion
  [uint16]$hService = 0
  $hr = -1
  try {
    $hr = [NcrXfsCollector]::WFSOpen($name, $hApp, 'ZabbixATMRecon', 0, $timeout,
      $version, [ref]$svcVer, [ref]$spiVer, [ref]$hService)
  } catch {
    return @{ hr = -2; hService = 0; svcVer = $svcVer; spiVer = $spiVer;
              error = $_.Exception.Message }
  }
  return @{ hr = $hr; hService = $hService; svcVer = $svcVer; spiVer = $spiVer;
            error = '' }
}

function Get-XfsInfo([uint16]$hService, [uint32]$category, [uint32]$timeout) {
  $resPtr = [IntPtr]::Zero
  $hr = -1
  try {
    $hr = [NcrXfsCollector]::WFSGetInfo($hService, $category, [IntPtr]::Zero,
      $timeout, [ref]$resPtr)
  } catch {
    return @{ hr = -2; ptr = [IntPtr]::Zero; result = $null;
              error = $_.Exception.Message }
  }
  if ($hr -ne 0 -or $resPtr -eq [IntPtr]::Zero) {
    return @{ hr = $hr; ptr = $resPtr; result = $null; error = '' }
  }
  $res = [Runtime.InteropServices.Marshal]::PtrToStructure(
    $resPtr, [type][NcrXfsCollectorResult])
  return @{ hr = $hr; ptr = $resPtr; result = $res; error = '' }
}

function Close-XfsService([uint16]$hService) {
  if ($hService -ne 0) { [void][NcrXfsCollector]::WFSClose($hService) }
}

# ── Cassette mapping ───────────────────────────────────────────────────────
# Map by physical unit number, not by ulValues. NCR reports the second
# 100 ETB cassette as 101, which a denomination lookup would turn into a CU<n>
# key the template and portal cannot chart. The denomination field keeps the
# vendor's value (101) so the cash total is not silently rewritten.
function New-CassetteSummary([object[]]$units) {
  $totalNotes = 0
  $totalAmount = 0
  $rejectNotes = 0
  $retractNotes = 0
  $hasLow = 0
  $hasEmpty = 0
  $currency = 'ETB'
  $cassettes = @{}

  foreach ($u in @($units | Sort-Object number)) {
    $isReject = ($u.type -eq 2)
    $isRetract = ($u.type -eq 6)
    $isDispense = (-not $isReject -and -not $isRetract)

    if ($isDispense) {
      $totalNotes += $u.count
      $totalAmount += ($u.count * $u.values)
      if ($u.currency -and $u.currency.Trim().Length -gt 0) {
        $currency = $u.currency.Trim()
      }
      if ($u.count -le $u.minimum) { $hasLow = 1 }
      if ($u.count -eq 0) { $hasEmpty = 1 }
    } elseif ($isReject) {
      $rejectNotes += $u.count
    } elseif ($isRetract) {
      $retractNotes += $u.count
    }

    $id = $null
    if ($isReject) {
      $id = 'Rej01'
    } elseif ($isRetract) {
      $id = 'Rct01'
    } else {
      switch ([int]$u.number) {
        1 { $id = '0050A' }
        2 { $id = '0100A' }
        3 { $id = '0100B' }
        4 { $id = '0200A' }
        default { $id = "CU$($u.number)" }
      }
    }
    if ($cassettes.ContainsKey($id)) { $id = "CU$($u.number)" }

    $kind = 'dispense'
    if ($isReject) { $kind = 'reject' } elseif ($isRetract) { $kind = 'retract' }
    $isLow = 0
    if ($isDispense -and $u.count -le $u.minimum) { $isLow = 1 }
    $isEmpty = 0
    if ($isDispense -and $u.count -eq 0) { $isEmpty = 1 }

    $cassettes[$id] = @{
      number = $u.number
      unit_id = $id
      ncr_number = $u.number
      type = $kind
      currency = $u.currency
      denomination = $u.values
      count = $u.count
      reject_count = $u.reject
      minimum = $u.minimum
      maximum = $u.maximum
      status = $u.status
      is_low = $isLow
      is_empty = $isEmpty
    }
  }

  return @{
    cassettes = $cassettes
    summary = @{
      total_amount = $totalAmount
      total_notes = $totalNotes
      reject_count = $rejectNotes
      retract_count = $retractNotes
      currency = $currency
      has_low = $hasLow
      has_empty = $hasEmpty
    }
  }
}

# ── Startup ────────────────────────────────────────────────────────────────
$startupVersion = $null
$startup = $null
foreach ($v in @(0x00030203, 0x00030000, 0x00030003, 0x00031000, 0x00032000, 0x00033000, 0x00010001)) {
  $ver = New-XfsVersion
  $hr = -1
  try { $hr = [NcrXfsCollector]::WFSStartUp([uint32]$v, [ref]$ver) } catch { continue }
  if ($hr -eq 0) {
    $startupVersion = $v
    $startup = @{ requested = [int64]$v; wVersion = [int]$ver.wVersion;
                  description = [string]$ver.szDescription }
    break
  }
}

# ── Poll ───────────────────────────────────────────────────────────────────
$services = @(Get-LogicalServices | Sort-Object Class, Name)
$payload = @{
  ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss')
  vendor = 'NCR'
  probe_version = '2026-10-29.3'
  probe_path = [string]$MyInvocation.MyCommand.Path
  process64 = $is64
  startup = $startup
  cdm = @{
    service = ''
    open_hresult = -1
    status_hresult = -1
    cash_unit_hresult = -1
    status = @{ device = -1; safe_door = -1; dispenser = -1; intermediate_stacker = -1 }
    summary = @{ total_amount = -1; total_notes = -1; reject_count = 0;
                 retract_count = 0; currency = 'ETB'; has_low = 0; has_empty = 0 }
    cassettes = @{}
  }
  idc = @{ class = 'IDC'; service = ''; device = 255; open_hresult = -1; status_hresult = -1;
           error = 'not queried' }
  ptr = @{ class = 'PTR'; service = ''; device = 255; open_hresult = -1; status_hresult = -1;
           error = 'not queried' }
  pin = @{ class = 'PIN'; service = ''; device = 255; open_hresult = -1; status_hresult = -1;
           error = 'not queried' }
  siu = @{ class = 'SIU'; service = ''; device = 255; open_hresult = -1; status_hresult = -1;
           error = 'not queried' }
}

[IntPtr]$hApp = [IntPtr]::Zero
$appHr = -1
if ($null -ne $startupVersion) {
  try { $appHr = [NcrXfsCollector]::WFSCreateAppHandle([ref]$hApp) } catch { $appHr = -1 }
  if ($appHr -ne 0) { $hApp = [IntPtr]::Zero }
}

if ($null -ne $startupVersion -and $hApp -ne [IntPtr]::Zero) {
  # The real dispenser is a CurrencyDispenser/MediaDispenser service. Sort
  # those first so the probe does not waste six seconds opening the absent
  # BillDispenser/CoinDispenser/BRMDispenser services.
  $cdmCandidates = @()
  $cdmCandidates += @($services | Where-Object {
    $_.Class -eq 'CDM' -and $_.Name -match '(?i)CurrencyDispenser|MediaDispenser'
  } | Sort-Object Name)
  $cdmCandidates += @($services | Where-Object {
    $_.Class -eq 'CDM' -and $_.Name -notmatch '(?i)CurrencyDispenser|MediaDispenser'
  } | Sort-Object Name)

  $best = $null
  foreach ($svc in $cdmCandidates) {
    $open = Open-XfsService $svc.Name $hApp ([uint32]$startupVersion) 3000
    if ($open.hr -ne 0) { continue }

    $status = $null
    $statusHr = -1
    $statusInfo = Get-XfsInfo $open.hService 301 3000
    if ($statusInfo.result -ne $null -and $statusInfo.result.hResult -eq 0 -and
        $statusInfo.result.lpBuffer -ne [IntPtr]::Zero) {
      $st = [Runtime.InteropServices.Marshal]::PtrToStructure(
        $statusInfo.result.lpBuffer, [type][NcrXfsCollectorCdmStatus])
      $status = @{ device = [int]$st.fwDevice; safe_door = [int]$st.fwSafeDoor;
                   dispenser = [int]$st.fwDispenser;
                   intermediate_stacker = [int]$st.fwIntermediateStacker }
      $statusHr = $statusInfo.hr
    }
    if ($statusInfo.ptr -ne [IntPtr]::Zero) {
      [void][NcrXfsCollector]::WFSFreeResult($statusInfo.ptr)
    }

    $units = @()
    $cuHr = -1
    $cuInfo = Get-XfsInfo $open.hService 303 3000
    if ($cuInfo.result -ne $null -and $cuInfo.result.hResult -eq 0 -and
        $cuInfo.result.lpBuffer -ne [IntPtr]::Zero) {
      $cuHr = $cuInfo.hr
      $list = [Runtime.InteropServices.Marshal]::PtrToStructure(
        $cuInfo.result.lpBuffer, [type][NcrXfsCollectorCdmCuInfo])
      $count = [int]$list.usCount
      if ($count -gt 20) { $count = 20 }
      for ($i = 0; $i -lt $count; $i++) {
        $unitPtr = [Runtime.InteropServices.Marshal]::ReadIntPtr(
          $list.lppList, $i * [IntPtr]::Size)
        if ($unitPtr -eq [IntPtr]::Zero) { continue }
        $u = [Runtime.InteropServices.Marshal]::PtrToStructure(
          $unitPtr, [type][NcrXfsCollectorCdmCashUnit])
        $name = ''
        if ($u.lpszCashUnitName -ne [IntPtr]::Zero) {
          try {
            $name = [Runtime.InteropServices.Marshal]::PtrToStringAnsi(
              $u.lpszCashUnitName)
          } catch {}
        }
        $units += @{
          number = [int]$u.usNumber
          type = [int]$u.usType
          name = $name
          unit_id = (Convert-Bytes $u.cUnitID)
          currency = (Convert-Bytes $u.cCurrencyID)
          values = [int]$u.ulValues
          count = [int]$u.ulCount
          initial = [int]$u.ulInitialCount
          reject = [int]$u.ulRejectCount
          minimum = [int]$u.ulMinimum
          maximum = [int]$u.ulMaximum
          status = [int]$u.usStatus
          physical_count = [int]$u.usNumPhysicalCUs
        }
      }
    }
    if ($cuInfo.ptr -ne [IntPtr]::Zero) {
      [void][NcrXfsCollector]::WFSFreeResult($cuInfo.ptr)
    }
    Close-XfsService $open.hService

    # Prefer a service with real cash units, then an online device, then a
    # currency dispenser name. A 301-only service is a last resort.
    $score = 0
    if ($cuHr -eq 0) {
      if ($units.Count -gt 0) { $score = 100 } else { $score = 50 }
    } elseif ($statusHr -eq 0) {
      $score = 10
    }
    if ($status -ne $null -and $status.device -eq 0) { $score += 5 }
    if ($svc.Name -match '(?i)CurrencyDispenser') { $score += 5 }

    if ($best -eq $null -or $score -gt $best.score) {
      $best = @{ score = $score; svc = $svc; open = $open;
                 status = $status; statusHr = $statusHr;
                 units = $units; cuHr = $cuHr }
    }
    if ($score -ge 110) { break }
  }

  if ($best -ne $null) {
    $payload.cdm.service = $best.svc.Name
    $payload.cdm.open_hresult = $best.open.hr
    $payload.cdm.status_hresult = $best.statusHr
    $payload.cdm.cash_unit_hresult = $best.cuHr
    if ($best.status -ne $null) { $payload.cdm.status = $best.status }

    if ($best.units.Count -gt 0) {
      $mapped = New-CassetteSummary $best.units
      $payload.cdm.cassettes = $mapped.cassettes
      $payload.cdm.summary = $mapped.summary
    }
  }

  # ── Peripherals ─────────────────────────────────────────────────────────
  function Get-PeripheralStatus([string]$class, [IntPtr]$hApp, [uint32]$version) {
    $prefs = @{
      IDC = @('IDCardUnit1', 'CardReader', 'IDCardUnit2')
      PTR = @('ReceiptPrinter1', 'USBReceiptPrinter1', 'ReceiptPrinter2', 'CheckPrinter')
      PIN = @('Pinpad1', 'Encryptor')
      SIU = @('SIU')
    }
    $candidates = @()
    foreach ($n in $prefs[$class]) {
      $found = @($services | Where-Object {
        $_.Class -eq $class -and $_.Name -eq $n
      } | Select-Object -First 1)
      if ($found.Count -gt 0) { $candidates += $found[0] }
    }
    # Fall back to other services of the class, but never substitute a
    # contactless reader, card dispenser, cheque/journal printer, etc. for
    # the primary device.
    foreach ($svc in @($services | Where-Object { $_.Class -eq $class } | Sort-Object Name)) {
      if ($class -eq 'IDC' -and $svc.Name -match '(?i)Contactless|CRD') { continue }
      if ($class -eq 'PTR' -and $svc.Name -match '(?i)Cheque|Journal|Statement|Passbook|Document|Barcode|Signature') { continue }
      if (-not ($candidates | Where-Object { $_.Name -eq $svc.Name })) {
        $candidates += $svc
      }
    }

    $fallback = $null
    $attempts = @()
    $candidateNames = @($candidates | ForEach-Object { $_.Name })
    foreach ($svc in $candidates) {
      $attempt = @{ service = $svc.Name; open_hresult = -1; status_hresult = -1 }
      $attempts += $attempt
      $open = Open-XfsService $svc.Name $hApp $version 3000
      $attempt.open_hresult = [int]$open.hr
      if ($open.hr -ne 0) {
        if ($fallback -eq $null) {
          # 255 is the template's "not queried" sentinel. Emitting it keeps
          # Zabbix from retaining an old device status and lets the portal
          # fall back to WMI for this peripheral.
          $fallback = @{ class = $class; service = $svc.Name; device = 255;
                         open_hresult = $open.hr; status_hresult = -1;
                         error = 'WFSOpen failed' }
        }
        continue
      }

      $category = @{ IDC = 201; PTR = 101; PIN = 401; SIU = 801 }[$class]
      $info = Get-XfsInfo $open.hService ([uint32]$category) 3000
      $attempt.status_hresult = [int]$info.hr
      $payloadItem = @{ class = $class; service = $svc.Name; device = 255;
                        open_hresult = $open.hr; status_hresult = $info.hr;
                        error = '' }

      if ($info.result -ne $null -and $info.result.hResult -eq 0 -and
          $info.result.lpBuffer -ne [IntPtr]::Zero) {
        $buf = $info.result.lpBuffer
        if ($class -eq 'IDC') {
          $st = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $buf, [type][NcrXfsCollectorIdcStatus])
          $payloadItem.device = [int]$st.fwDevice
          $payloadItem.media = [int]$st.fwMedia
          $payloadItem.retain_bin = [int]$st.fwRetainBin
          $payloadItem.security = [int]$st.fwSecurity
          $payloadItem.cards = [int]$st.usCards
          $payloadItem.chip_power = [int]$st.fwChipPower
        } elseif ($class -eq 'PTR') {
          $st = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $buf, [type][NcrXfsCollectorPtrStatus])
          $payloadItem.device = [int]$st.fwDevice
          $payloadItem.media = [int]$st.fwMedia
          $payloadItem.paper = [int]$st.fwPaper
          # Keep the raw prefix: NCR's PTR status is the one field set that
          # still needs comparison with WMI on the test core. Eight words is
          # the 16-byte prefix the recon proved safe to copy.
          $raw = @()
          try {
            for ($i = 0; $i -lt 8; $i++) {
              $raw += [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, $i * 2)
            }
          } catch {}
          $payloadItem.raw = $raw
        } elseif ($class -eq 'PIN') {
          $st = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $buf, [type][NcrXfsCollectorPinStatus])
          $payloadItem.device = [int]$st.fwDevice
          $payloadItem.enc_stat = [int]$st.fwEncStat
        } elseif ($class -eq 'SIU') {
          $st = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $buf, [type][NcrXfsCollectorSiuStatus])
          $payloadItem.device = [int]$st.fwDevice
          $payloadItem.operator_switch_raw = [int]$st.fwOperatorSwitch
          if ($st.fwOperatorSwitch -eq 1) { $payloadItem.supervisor_switch = 0 }
          elseif ($st.fwOperatorSwitch -eq 2) { $payloadItem.supervisor_switch = 1 }
          else { $payloadItem.supervisor_switch = 255 }
          # Door/tamper offsets differ by NCR SIU model. Keep them unknown
          # and expose the first 40 sensors for a later mapping run.
          $payloadItem.cabinet_door = 255
          $payloadItem.safe_door = 255
          $payloadItem.tamper_sensor = 255
          $raw = @()
          try {
            for ($i = 0; $i -lt 40; $i++) {
              $raw += [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, $i * 2)
            }
          } catch {}
          $payloadItem.raw = $raw
          $payloadItem.error = 'door/tamper sensors not mapped on NCR yet'
        }
      } else {
        $payloadItem.error = 'WFSGetInfo failed'
      }

      if ($info.ptr -ne [IntPtr]::Zero) {
        [void][NcrXfsCollector]::WFSFreeResult($info.ptr)
      }
      Close-XfsService $open.hService

      if ($fallback -eq $null) { $fallback = $payloadItem }
      if ($payloadItem.status_hresult -eq 0 -and $payloadItem.device -ne 3) {
        $payloadItem.candidates = $candidateNames
        $payloadItem.attempts = $attempts
        return $payloadItem
      }
    }
    if ($fallback -ne $null) {
      $fallback.candidates = $candidateNames
      $fallback.attempts = $attempts
      return $fallback
    }
    return @{ class = $class; service = ''; device = 255; open_hresult = -1;
             status_hresult = -1; error = 'service not found';
             candidates = $candidateNames; attempts = $attempts }
  }

  $payload.idc = Get-PeripheralStatus 'IDC' $hApp ([uint32]$startupVersion)
  $payload.ptr = Get-PeripheralStatus 'PTR' $hApp ([uint32]$startupVersion)
  $payload.pin = Get-PeripheralStatus 'PIN' $hApp ([uint32]$startupVersion)
  $payload.siu = Get-PeripheralStatus 'SIU' $hApp ([uint32]$startupVersion)
}

if ($hApp -ne [IntPtr]::Zero) {
  [void][NcrXfsCollector]::WFSDestroyAppHandle($hApp)
}
if ($null -ne $startupVersion) { [void][NcrXfsCollector]::WFSCleanUp() }

[Console]::WriteLine((ConvertTo-JsonValue $payload))
exit 0
