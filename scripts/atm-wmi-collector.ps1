#Requires -Version 2.0
<#
.SYNOPSIS
  Dashen Bank ATM — WMI part-state collector (read-only).
.DESCRIPTION
  Queries the proven NCR WMI classes on NCR APTRA Windows cores and prints
  one compact JSON line for Zabbix agent polling (UserParameter atm.parts).

  Read-only: pure Get-WmiObject queries. No writes, no restarts, no dialogs.
  Safe to run by hand first:  powershell -File atm-wmi-collector.ps1

  Classes (all verified present on SelfServ test core, 2026-09-30):
    NCR_DevicePart  - per-part state (reader, shutter, printers, cassettes,
                      purge/reject/retract, PinPad, presenter, ...)
    NCR_Device      - per-device operational state, added 2026-10-12
                      (14 instances on TESTMACHINE; DeviceStatus 0 OK,
                      1 Warning, 2 Unavailable)
  Output shape (stable contract — Zabbix dependents rely on it):
    {"ts":"2026-09-30T12:00:00","parts":{"First Cassette":{"part_status":4,
     "pm_status":0,"replenishment_status":1,"replenishable":true,
     "powered_on":null,"device_instance":65722,"part_id":8}, ...},
     "devices":{"CASH-01-UsbMediaDispenser":{"device_status":1,...}, ...}}
  Duplicate part names are " (2)"-suffixed, bare name binds lowest
  (DeviceInstance,PartId) — see Get-Parts. Master key stays atm.parts.
.PARAMETER Part
  Optional single-part mode for zabbix_get testing:
    powershell -File atm-wmi-collector.ps1 -Part "First Cassette"
  Prints that part's object only (same shape, single entry).
#>
param([string]$Part = "")

$ErrorActionPreference = 'SilentlyContinue'

function Get-Parts {
  $rows = Get-WmiObject -Class NCR_DevicePart
  # NOTE 2026-10-09 (proven on TESTMACHINE): names are NOT unique —
  # Control Board x2 (dev 65674/65722), Transport x2 (dev 65673/65674).
  # A name-keyed hashtable collapses each pair last-write-wins in RANDOM
  # order, so Zabbix flaps between the instances (live case: Transport
  # #2=OK vs #5=Error while "Transport" read 0). Disambiguate
  # deterministically: sort by name,DeviceInstance,PartId; the first keeps
  # the bare name (history continuity), the rest get " (2)", " (3)".
  # Template PARTS and trigger lists must carry the same suffixed names.
  $list = @()
  foreach ($r in $rows) {
    $name = [string]$r.Name
    if ([string]::IsNullOrEmpty($name)) { continue }
    if ($Part -ne "" -and $name -ne $Part) { continue }
    $list += New-Object PSObject -Property @{
      Name = $name
      Dev  = [uint32]$r.DeviceInstance
      Pid  = [uint16]$r.PartId
      Row  = $r
    }
  }
  $list = $list | Sort-Object -Property Name, Dev, Pid
  $out = @{}
  $prev = ""
  $dup = 0
  foreach ($e in $list) {
    $r = $e.Row
    if ($e.Name -eq $prev) { $dup += 1 } else { $prev = $e.Name; $dup = 0 }
    $key = $e.Name
    if ($dup -gt 0) { $key = "$($e.Name) ($($dup + 1))" }
    $out[$key] = @{
      part_status          = [int]$r.PartStatus
      pm_status            = [int]$r.PMStatus
      replenishment_status = [int]$r.ReplenishmentStatus
      replenishable        = [bool]$r.Replenishable
      powered_on           = $r.PoweredOn
      device_instance      = [uint32]$r.DeviceInstance
      part_id              = [uint16]$r.PartId
    }
  }
  return $out
}

# Minimal JSON writer (works on PowerShell 2.0 — no ConvertTo-Json needed).
function Escape-JsonString([string]$s) {
  $s = $s -replace '\\', '\\'
  $s = $s -replace '"', '\"'
  $s = $s -replace "`n", '\n'
  $s = $s -replace "`r", '\r'
  $s = $s -replace "`t", '\t'
  return $s
}

# NCR_Device sweep: device-level operational states. Discovered 2026-10-12
# (14 instances on TESTMACHINE; DeviceStatus legend 0 OK, 1 Warning,
# 2 Unavailable — same qualifier pattern as PartStatus). Same duplicate
# guard as parts (names assumed unique, suffixed if ever not).
function Get-Devices {
  $rows = Get-WmiObject -Class NCR_Device
  $list = @()
  foreach ($r in $rows) {
    $name = [string]$r.Name
    if ([string]::IsNullOrEmpty($name)) { continue }
    $list += New-Object PSObject -Property @{
      Name = $name
      Dev  = [uint32]$r.DeviceInstance
      Row  = $r
    }
  }
  $list = $list | Sort-Object -Property Name, Dev
  $out = @{}
  $prev = ""
  $dup = 0
  foreach ($e in $list) {
    $r = $e.Row
    if ($e.Name -eq $prev) { $dup += 1 } else { $prev = $e.Name; $dup = 0 }
    $key = $e.Name
    if ($dup -gt 0) { $key = "$($e.Name) ($($dup + 1))" }
    $out[$key] = @{
      device_status   = [int]$r.DeviceStatus
      pm_status       = [int]$r.PMStatus
      device_instance = [uint32]$r.DeviceInstance
    }
  }
  return $out
}

$parts = Get-Parts
$devices = Get-Devices
$ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss')
$sb = New-Object System.Text.StringBuilder
[void]$sb.Append('{"ts":"' + $ts + '","parts":{')
$first = $true
foreach ($k in ($parts.Keys | Sort-Object)) {
  $v = $parts[$k]
  if (-not $first) { [void]$sb.Append(',') }
  $first = $false
  $pon = if ($null -eq $v['powered_on']) { 'null' } else { ([string]$v['powered_on']).ToLower() }
  [void]$sb.Append('"' + (Escape-JsonString $k) + '":{"part_status":' + $v['part_status'] +
    ',"pm_status":' + $v['pm_status'] +
    ',"replenishment_status":' + $v['replenishment_status'] +
    ',"replenishable":' + ($v['replenishable'].ToString().ToLower()) +
    ',"powered_on":' + $pon +
    ',"device_instance":' + $v['device_instance'] +
    ',"part_id":' + $v['part_id'] + '}')
}
[void]$sb.Append('},"devices":{')
$first = $true
foreach ($k in ($devices.Keys | Sort-Object)) {
  $v = $devices[$k]
  if (-not $first) { [void]$sb.Append(',') }
  $first = $false
  [void]$sb.Append('"' + (Escape-JsonString $k) + '":{"device_status":' + $v['device_status'] +
    ',"pm_status":' + $v['pm_status'] +
    ',"device_instance":' + $v['device_instance'] + '}')
}
[void]$sb.Append('}}')
# NOTE: [Console]::WriteLine, NOT Write-Output. Write-Output sends the long
# JSON line through PowerShell's formatting pipeline, which hard-wraps it at
# console width (~80 cols) with literal CRLF — splitting key names mid-token
# ("Card Captu<CRLF>re Bin") and producing invalid JSON. Proven live 2026-10-09:
# zabbix_get payload failed json.tool at char 159 with exactly such a split,
# and all 41 dependents died (JSONPath control-character + Duktape offset-161
# errors are both downstream symptoms of the same broken document).
# Console.WriteLine bypasses formatting: one clean line, single trailing CRLF.
[System.Console]::WriteLine($sb.ToString())
