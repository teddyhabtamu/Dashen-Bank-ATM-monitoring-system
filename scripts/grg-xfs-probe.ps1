#Requires -Version 3.0
<#
.SYNOPSIS
  Read-only GRG XFS probe for TESTGRG.
.DESCRIPTION
  Runs in 32-bit PowerShell because the GRG XFS manager is x86.
  - lists logical services from HKEY_USERS\.DEFAULT\XFS\LOGICAL_SERVICES
  - negotiates WFSStartUp with the manager
  - opens a CDM service and prints status + cash units
  - opens CardReader, CheckPrinter, Encryptor and SIU in the same read-only
    XFS session; use -Class IDC|PTR|PIN|SIU to isolate one during diagnosis
  No writes, no dispense, no reset.
#>
param(
  [switch]$Json,
  [ValidateSet('IDC','PTR','PIN','SIU')] [string]$Class
)

$ErrorActionPreference = 'SilentlyContinue'

if ([Environment]::Is64BitProcess) {
  $ps32 = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
  $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath)
  if ($Json) { $argList += '-Json' }
  if ($Class) { $argList += @('-Class', $Class) }
  & $ps32 @argList
  exit $LASTEXITCODE
}

if (-not ('XfsNative' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential, Pack = 1, CharSet = CharSet.Ansi)]
public struct XfsVersion
{
    public ushort wVersion;
    public ushort wLowVersion;
    public ushort wHighVersion;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)]
    public string szDescription;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)]
    public string szSystemStatus;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct XfsSystemTime
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
public struct XfsResult
{
    public uint RequestID;
    public ushort hService;
    public XfsSystemTime tsTimestamp;
    public int hResult;
    public uint u;
    public IntPtr lpBuffer;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct XfsCdmStatus
{
    public ushort fwDevice;
    public ushort fwSafeDoor;
    public ushort fwDispenser;
    public ushort fwIntermediateStacker;
    public IntPtr lppPositions;
    public IntPtr lpszExtra;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 32)]
    public uint[] dwGuidLights;
    public ushort wDevicePosition;
    public ushort usPowerSaveRecoveryTime;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct XfsCdmCashUnit
{
    public ushort usNumber;
    public ushort usType;
    public IntPtr lpszCashUnitName;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 5)]
    public byte[] cUnitID;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 3)]
    public byte[] cCurrencyID;
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
public struct XfsCdmCuInfo
{
    public ushort usTellerID;
    public ushort usCount;
    public IntPtr lppList;
}

public static class XfsNative
{
    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSStartUp(uint dwVersionsRequired, ref XfsVersion lpWFSVersion);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSCreateAppHandle(out IntPtr lphApp);

    [DllImport("msxfs.dll", CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSDestroyAppHandle(IntPtr hApp);

    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern int WFSOpen(string lpszLogicalName, IntPtr hApp, string lpszAppID,
        uint dwTraceLevel, uint dwTimeOut, uint dwSrvcVersionsRequired,
        ref XfsVersion lpSrvcVersion, ref XfsVersion lpSPIVersion, out ushort lphService);

    [DllImport("msxfs.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
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

function Get-LogicalServices {
  $roots = @(
    'Registry::HKEY_USERS\.DEFAULT\XFS\LOGICAL_SERVICES',
    'Registry::HKEY_CURRENT_USER\XFS\LOGICAL_SERVICES'
  )
  $seen = @{}
  foreach ($root in $roots) {
    if (-not (Test-Path $root)) { continue }
    foreach ($k in Get-ChildItem $root) {
      if ($seen.ContainsKey($k.PSChildName)) { continue }
      $seen[$k.PSChildName] = $true
      $p = Get-ItemProperty $k.PSPath
      New-Object PSObject -Property @{
        Name = $k.PSChildName
        Class = [string]$p.Class
        Type = [string]$p.Type
        Provider = [string]$p.Provider
      }
    }
  }
}

function Convert-Bytes([byte[]]$b) {
  if ($null -eq $b) { return '' }
  return ([Text.Encoding]::ASCII.GetString($b)).TrimEnd([char]0)
}

function New-XfsVersion {
  $v = New-Object XfsVersion
  $v.szDescription = ''
  $v.szSystemStatus = ''
  return $v
}

function Get-PeripheralService([string]$class) {
  $exact = @{ IDC = 'CardReader'; PTR = 'CheckPrinter'; PIN = 'Encryptor'; SIU = 'SIU' }
  $name = $exact[$class]
  $svc = @($services | Where-Object { $_.Name -eq $name } | Select-Object -First 1)
  if ($svc.Count -gt 0) { return $svc[0] }
  if ($class -eq 'PTR') { return $null }
  $svc = @($services | Where-Object {
    $_.Class -eq $class -and $_.Provider -and $_.Provider -ne 'NODEVICE'
  } | Sort-Object Name | Select-Object -First 1)
  if ($svc.Count -gt 0) { return $svc[0] }
  return $null
}

function Convert-SiuDoor([int]$value) {
  switch ($value) {
    1 { return 3 }
    2 { return 2 }
    0 { return 1 }
    default { return 5 }
  }
}

function Convert-SiuOperator([int]$value) {
  if ($value -eq 1) { return 0 }
  if ($value -eq 2) { return 1 }
  return 255
}

function Convert-SiuTamper([int]$value) {
  if ($value -eq 2) { return 1 }
  if ($value -eq 1) { return 0 }
  return 255
}

function Get-PeripheralStatus([string]$class, [IntPtr]$hApp) {
  $svc = Get-PeripheralService $class
  $payload = @{
    class = $class
    service = if ($svc) { $svc.Name } else { '' }
    open_hresult = -1
    status_hresult = -1
    error = if ($svc) { '' } else { 'service not found' }
  }
  if (-not $svc) { return $payload }

  $svcVer = New-XfsVersion
  $spiVer = New-XfsVersion
  [uint16]$hService = 0
  $openHr = [XfsNative]::WFSOpen($svc.Name, $hApp, 'ZabbixATM', 0, 1500,
    [uint32]$startupVersion, [ref]$svcVer, [ref]$spiVer, [ref]$hService)
  $payload.open_hresult = $openHr
  if ($openHr -ne 0) { return $payload }

  $category = @{ IDC = 201; PTR = 101; PIN = 401; SIU = 801 }[$class]
  $resPtr = [IntPtr]::Zero
  $statusHr = [XfsNative]::WFSGetInfo($hService, [uint32]$category, [IntPtr]::Zero, 1500, [ref]$resPtr)
  $payload.status_hresult = $statusHr
  if ($statusHr -eq 0 -and $resPtr -ne [IntPtr]::Zero) {
    try {
      $res = [Runtime.InteropServices.Marshal]::PtrToStructure($resPtr, [type][XfsResult])
      if ($res.hResult -eq 0 -and $res.lpBuffer -ne [IntPtr]::Zero) {
        $buf = $res.lpBuffer
        if ($class -eq 'IDC') {
          $payload.device = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 0)
          $payload.media = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 2)
          $payload.retain_bin = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 4)
          $payload.security = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 6)
          $payload.cards = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 8)
          $payload.chip_power = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 10)
        } elseif ($class -eq 'PTR') {
          $payload.device = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 0)
          $payload.media = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 2)
          $payload.paper = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 4)
          $payload.paper_levels = @()
          for ($i = 0; $i -lt 16; $i++) {
            $payload.paper_levels += [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 4 + ($i * 2))
          }
          $payload.toner = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 36)
          $payload.ink = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 38)
          $payload.lamp = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 40)
        } elseif ($class -eq 'PIN') {
          $payload.device = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 0)
          $payload.enc_stat = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 2)
        } elseif ($class -eq 'SIU') {
          $payload.device = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 0)
          $payload.operator_switch_raw = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 2)
          $payload.tamper_sensor_raw = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 8)
          $payload.cabinet_door_raw = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 66)
          $payload.safe_door_raw = [int][Runtime.InteropServices.Marshal]::ReadInt16($buf, 68)
          $payload.supervisor_switch = Convert-SiuOperator $payload.operator_switch_raw
          $payload.tamper_sensor = Convert-SiuTamper $payload.tamper_sensor_raw
          $payload.cabinet_door = Convert-SiuDoor $payload.cabinet_door_raw
          $payload.safe_door = Convert-SiuDoor $payload.safe_door_raw
        }
      } else {
        $payload.error = "WFSGetInfo hResult=$($res.hResult)"
      }
    } catch {
      $payload.error = 'status parse failed'
    }
    [void][XfsNative]::WFSFreeResult($resPtr)
  }
  [void][XfsNative]::WFSClose($hService)
  return $payload
}

$services = @(Get-LogicalServices | Sort-Object Class, Name)
$result = New-Object PSObject -Property @{
  ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss')
  process64 = $false
  services = $services
  startup = $null
  cdm = $null
  idc = $null
  ptr = $null
  pin = $null
  siu = $null
}

$startupCandidates = @(3, 0x00030000, 0x00030203, 0x00030003, 0x00010001)
$startupVersion = $null
foreach ($v in $startupCandidates) {
  $ver = New-XfsVersion
  $hr = [XfsNative]::WFSStartUp([uint32]$v, [ref]$ver)
  if ($hr -eq 0) {
    $startupVersion = $v
    $result.startup = New-Object PSObject -Property @{
      requested = $v
      hresult = $hr
      wVersion = $ver.wVersion
      wLowVersion = $ver.wLowVersion
      wHighVersion = $ver.wHighVersion
      description = $ver.szDescription
      status = $ver.szSystemStatus
    }
    break
  }
  $result.startup = New-Object PSObject -Property @{
    requested = $v
    hresult = $hr
    error = 'WFSStartUp failed'
  }
}

if ($null -ne $startupVersion) {
  [IntPtr]$hApp = [IntPtr]::Zero
  $appHr = -1
  try { $appHr = [XfsNative]::WFSCreateAppHandle([ref]$hApp) } catch {}
  if ($appHr -ne 0) { $hApp = [IntPtr]::Zero }

  if ($Class) {
    $payload = Get-PeripheralStatus $Class $hApp
    try { [void][XfsNative]::WFSDestroyAppHandle($hApp) } catch {}
    [void][XfsNative]::WFSCleanUp()
    [Console]::WriteLine((ConvertTo-Json -InputObject $payload -Depth 5 -Compress))
    exit 0
  }

  # Query CDM (Cash Dispenser)
  try {
    $cdmServices = @($services | Where-Object { $_.Class -eq 'CDM' } | Sort-Object Name)
    foreach ($s in $cdmServices) {
      $svcVer = New-XfsVersion
      $spiVer = New-XfsVersion
      [uint16]$hService = 0
      $openHr = [XfsNative]::WFSOpen($s.Name, $hApp, 'ZabbixATM', 0, 1500,
        [uint32]$startupVersion, [ref]$svcVer, [ref]$spiVer, [ref]$hService)
      if ($openHr -ne 0) {
        $result.cdm = New-Object PSObject -Property @{
          service = $s.Name
          open_hresult = $openHr
          error = 'WFSOpen failed'
        }
        continue
      }

      $resPtr = [IntPtr]::Zero
      $statusHr = [XfsNative]::WFSGetInfo($hService, 301, [IntPtr]::Zero, 1500, [ref]$resPtr)
      $statusObj = $null
      if ($statusHr -eq 0 -and $resPtr -ne [IntPtr]::Zero) {
        try {
          $res = [Runtime.InteropServices.Marshal]::PtrToStructure($resPtr, [type][XfsResult])
          if ($res.hResult -eq 0 -and $res.lpBuffer -ne [IntPtr]::Zero) {
            $st = [Runtime.InteropServices.Marshal]::PtrToStructure($res.lpBuffer, [type][XfsCdmStatus])
            $statusObj = New-Object PSObject -Property @{
              device = $st.fwDevice
              safe_door = $st.fwSafeDoor
              dispenser = $st.fwDispenser
              intermediate_stacker = $st.fwIntermediateStacker
              device_position = $st.wDevicePosition
              power_save_recovery_time = $st.usPowerSaveRecoveryTime
            }
          }
        } catch {}
        [void][XfsNative]::WFSFreeResult($resPtr)
      }

      $resPtr = [IntPtr]::Zero
      $cuHr = [XfsNative]::WFSGetInfo($hService, 303, [IntPtr]::Zero, 1500, [ref]$resPtr)
      $units = @()
      if ($cuHr -eq 0 -and $resPtr -ne [IntPtr]::Zero) {
        try {
          $res = [Runtime.InteropServices.Marshal]::PtrToStructure($resPtr, [type][XfsResult])
          if ($res.hResult -eq 0 -and $res.lpBuffer -ne [IntPtr]::Zero) {
            $info = [Runtime.InteropServices.Marshal]::PtrToStructure($res.lpBuffer, [type][XfsCdmCuInfo])
            for ($i = 0; $i -lt $info.usCount; $i++) {
              $unitPtr = [Runtime.InteropServices.Marshal]::ReadIntPtr($info.lppList, $i * [IntPtr]::Size)
              if ($unitPtr -eq [IntPtr]::Zero) { continue }
              $u = [Runtime.InteropServices.Marshal]::PtrToStructure($unitPtr, [type][XfsCdmCashUnit])
              $units += New-Object PSObject -Property @{
                number = $u.usNumber
                type = $u.usType
                name = [Runtime.InteropServices.Marshal]::PtrToStringAnsi($u.lpszCashUnitName)
                unit_id = Convert-Bytes $u.cUnitID
                currency = Convert-Bytes $u.cCurrencyID
                values = $u.ulValues
                count = $u.ulCount
                initial = $u.ulInitialCount
                reject = $u.ulRejectCount
                minimum = $u.ulMinimum
                maximum = $u.ulMaximum
                app_lock = ($u.bAppLock -ne 0)
                status = $u.usStatus
                physical_count = $u.usNumPhysicalCUs
                dispensed = $u.ulDispensedCount
                presented = $u.ulPresentedCount
                retracted = $u.ulRetractedCount
              }
            }
          }
        } catch {}
        [void][XfsNative]::WFSFreeResult($resPtr)
      }

      $result.cdm = New-Object PSObject -Property @{
        service = $s.Name
        service_version = $svcVer.szDescription
        provider_version = $spiVer.szDescription
        open_hresult = $openHr
        status_hresult = $statusHr
        cash_unit_hresult = $cuHr
        status = $statusObj
        cash_units = $units
      }
      [void][XfsNative]::WFSClose($hService)
      if ($statusHr -eq 0 -or $cuHr -eq 0) { break }
    }
  } catch {}

  $result.idc = Get-PeripheralStatus 'IDC' $hApp
  $result.ptr = Get-PeripheralStatus 'PTR' $hApp
  $result.pin = Get-PeripheralStatus 'PIN' $hApp
  $result.siu = Get-PeripheralStatus 'SIU' $hApp

  try { [void][XfsNative]::WFSDestroyAppHandle($hApp) } catch {}
  [void][XfsNative]::WFSCleanUp()
}

if ($Json) {
  $cassettes = @{}
  $totalAmount = 0
  $totalNotes = 0
  $rejectNotes = 0
  $retractNotes = 0
  $currency = 'ETB'
  $hasLow = 0
  $hasEmpty = 0

  if ($null -ne $result.cdm -and $null -ne $result.cdm.cash_units) {
    foreach ($u in $result.cdm.cash_units) {
      $id = $u.unit_id
      if ([string]::IsNullOrEmpty($id)) { $id = "CU$($u.number)" }

      $isReject = ($id -match '(?i)rej' -or $u.type -eq 2)
      $isRetract = ($id -match '(?i)rct' -or $u.type -eq 6)
      $isDispense = (-not $isReject -and -not $isRetract)

      if ($isDispense) {
        $totalNotes += [int]$u.count
        $totalAmount += ([int]$u.count * [int]$u.values)
        if (-not [string]::IsNullOrEmpty($u.currency)) { $currency = $u.currency }
        if ($u.count -le $u.minimum) { $hasLow = 1 }
        if ($u.count -eq 0) { $hasEmpty = 1 }
      } elseif ($isReject) {
        $rejectNotes += [int]$u.count
      } elseif ($isRetract) {
        $retractNotes += [int]$u.count
      }

      $cassettes[$id] = @{
        number = [int]$u.number
        unit_id = $id
        type = if ($isReject) { 'reject' } elseif ($isRetract) { 'retract' } else { 'dispense' }
        currency = [string]$u.currency
        denomination = [int]$u.values
        count = [int]$u.count
        reject_count = [int]$u.reject
        minimum = [int]$u.minimum
        maximum = [int]$u.maximum
        status = [int]$u.status
        is_low = if ($isDispense -and $u.count -le $u.minimum) { 1 } else { 0 }
        is_empty = if ($isDispense -and $u.count -eq 0) { 1 } else { 0 }
      }
    }
  }

  $cdmStatus = @{
    device = if ($result.cdm -and $result.cdm.status) { [int]$result.cdm.status.device } else { -1 }
    safe_door = if ($result.cdm -and $result.cdm.status) { [int]$result.cdm.status.safe_door } else { -1 }
    dispenser = if ($result.cdm -and $result.cdm.status) { [int]$result.cdm.status.dispenser } else { -1 }
    intermediate_stacker = if ($result.cdm -and $result.cdm.status) { [int]$result.cdm.status.intermediate_stacker } else { -1 }
  }

  $summary = @{
    total_amount = $totalAmount
    total_notes = $totalNotes
    reject_count = $rejectNotes
    retract_count = $retractNotes
    currency = $currency
    has_low = $hasLow
    has_empty = $hasEmpty
  }

  $payload = @{
    ts = $result.ts
    vendor = 'GRG'
    cdm = @{
      service = if ($result.cdm) { $result.cdm.service } else { '' }
      open_hresult = if ($result.cdm) { $result.cdm.open_hresult } else { -1 }
      status = $cdmStatus
      summary = $summary
      cassettes = $cassettes
    }
    idc = $result.idc
    ptr = $result.ptr
    pin = $result.pin
    siu = $result.siu
  }

  $jsonString = ConvertTo-Json -InputObject $payload -Depth 5 -Compress
  [System.Console]::WriteLine($jsonString)
  exit 0
}

Write-Output "GRG XFS probe - $($result.ts)"
Write-Output "32-bit process: $(-not [Environment]::Is64BitProcess)"
Write-Output ''
Write-Output 'Logical services:'
if ($services.Count -eq 0) {
  Write-Output '  (none found under HKEY_USERS\.DEFAULT\XFS\LOGICAL_SERVICES)'
} else {
  $services | Format-Table Class, Name, Type, Provider -AutoSize
}
Write-Output ''
Write-Output 'WFSStartUp:'
$result.startup | Format-List
if ($null -ne $result.cdm) {
  Write-Output ''
  Write-Output 'CDM:'
  $result.cdm | Format-List service, service_version, provider_version, open_hresult, status_hresult, cash_unit_hresult
  if ($null -ne $result.cdm.status) {
    $result.cdm.status | Format-List
  }
  if ($result.cdm.cash_units.Count -gt 0) {
    $result.cdm.cash_units | Format-Table number, unit_id, currency, name, values, count, minimum, maximum, status, app_lock -AutoSize
  } else {
    Write-Output '  (no cash units returned)'
  }
}
