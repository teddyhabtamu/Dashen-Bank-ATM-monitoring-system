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
  Output shape (stable contract — Zabbix JSONPath dependents rely on it):
    {"ts":"2026-09-30T12:00:00","parts":{"First Cassette":{"part_status":4,
     "pm_status":0,"replenishment_status":1,"replenishable":true,
     "powered_on":null}, ...}}
.PARAMETER Part
  Optional single-part mode for zabbix_get testing:
    powershell -File atm-wmi-collector.ps1 -Part "First Cassette"
  Prints that part's object only (same shape, single entry).
#>
param([string]$Part = "")

$ErrorActionPreference = 'SilentlyContinue'

function Get-Parts {
  $rows = Get-WmiObject -Class NCR_DevicePart
  $out = @{}
  foreach ($r in $rows) {
    $name = [string]$r.Name
    if ([string]::IsNullOrEmpty($name)) { continue }
    if ($Part -ne "" -and $name -ne $Part) { continue }
    $out[$name] = @{
      part_status          = [int]$r.PartStatus
      pm_status            = [int]$r.PMStatus
      replenishment_status = [int]$r.ReplenishmentStatus
      replenishable        = [bool]$r.Replenishable
      powered_on           = $r.PoweredOn
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

$parts = Get-Parts
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
    ',"powered_on":' + $pon + '}')
}
[void]$sb.Append('}}')
Write-Output $sb.ToString()
