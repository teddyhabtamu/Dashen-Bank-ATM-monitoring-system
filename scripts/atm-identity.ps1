#Requires -Version 2.0
<#
.SYNOPSIS
  Dashen Bank ATM — read-only identity and evidence probe.

.DESCRIPTION
  Reports what this ATM can prove about itself to the portal's
  `atm.identity` UserParameter. The portal treats the result as evidence:
  it is compared with the registry and shown as a conflict, never written
  back automatically.

  The probe is deliberately conservative. Every field it cannot determine is
  null, and the portal's `unknown` rules keep silence from becoming a false
  conflict. It never writes, installs, starts, stops or reconfigures anything.

  What it reports:
    * vendor        — NCR or GRG, from the installed program/registry markers
    * terminal_id   — from the newest local EJ receipt/header when readable
    * ej.hostname   — NCR: the <HOST> in <date>_<HOST>_EJDATA.log;
                      GRG: the computer name
    * ej.roots      — directories where an EJ log was actually found
    * images.roots  — existing directories from C:\monitor\atm-image-roots.txt
    * probes        — SHA-256 and probe_version of each deployed probe
    * agent         — Zabbix agent file version and config hash
    * agent.profile_hash — canonical hash of the whole profile the installer applied
    * agent.deploy_hash  — hash of the files/config actually written to the box
    * serial        — BIOS serial number (asset evidence; never reconciled)

  Run it by hand first:
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\monitor\atm-identity.ps1

  Output is one compact JSON line, written with [Console]::WriteLine so the
  PowerShell formatting pipeline cannot wrap it. PowerShell 2.0 compatible:
  no ConvertTo-Json, Get-FileHash, Get-ChildItem -File or $PSScriptRoot.
#>

$ErrorActionPreference = 'SilentlyContinue'

# The manifest builder reads this exact assignment. Bump it whenever the
# output contract changes; the portal compares it with the deployed copy.
$probe_version = '2026-10-31.2'

# PowerShell 2.0 has no $PSScriptRoot. Resolve the script folder once so the
# roots and probes are read from beside this script regardless of the caller.
$Script:MonitorRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Script:MonitorRoot) {
    $Script:MonitorRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if (-not $Script:MonitorRoot) { $Script:MonitorRoot = 'C:\monitor' }

# ── JSON (PowerShell 2.0 has no ConvertTo-Json) ────────────────────────────
function ConvertTo-JsonString([string]$Value) {
    if ($null -eq $Value) { return '""' }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int]$ch
        if ($code -eq 34) { [void]$sb.Append('\"') }
        elseif ($code -eq 92) { [void]$sb.Append('\\') }
        elseif ($code -eq 8) { [void]$sb.Append('\b') }
        elseif ($code -eq 9) { [void]$sb.Append('\t') }
        elseif ($code -eq 10) { [void]$sb.Append('\n') }
        elseif ($code -eq 12) { [void]$sb.Append('\f') }
        elseif ($code -eq 13) { [void]$sb.Append('\r') }
        elseif ($code -lt 32) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function ConvertTo-JsonValue($Value) {
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) {
        if ($Value) { return 'true' }
        return 'false'
    }
    if ($Value -is [string]) { return (ConvertTo-JsonString $Value) }
    if ($Value -is [int] -or $Value -is [long] -or
        $Value -is [uint16] -or $Value -is [uint32] -or
        $Value -is [byte]) {
        return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $parts = @()
        foreach ($key in @($Value.Keys | Sort-Object)) {
            $parts += ((ConvertTo-JsonString ([string]$key)) + ':' +
                       (ConvertTo-JsonValue $Value[$key]))
        }
        return '{' + ($parts -join ',') + '}'
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $parts = @()
        foreach ($item in $Value) { $parts += (ConvertTo-JsonValue $item) }
        return '[' + ($parts -join ',') + ']'
    }
    if ($Value -is [ValueType]) {
        return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    return (ConvertTo-JsonString ([string]$Value))
}

# ── Read-only helpers ──────────────────────────────────────────────────────
function Get-Sha256([string]$Path) {
    $sha = $null
    $stream = $null
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open,
            [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $bytes = $sha.ComputeHash($stream)
        $sb = New-Object System.Text.StringBuilder
        foreach ($b in $bytes) { [void]$sb.Append($b.ToString('x2')) }
        return $sb.ToString()
    } catch {
        return $null
    } finally {
        if ($stream) { $stream.Close() }
        if ($sha) { $sha.Clear() }
    }
}

function Read-TextPrefix([string]$Path, [int]$MaxBytes) {
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open,
            [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $length = [int][Math]::Min([long]$MaxBytes, $stream.Length)
        if ($length -le 0) { return '' }
        $buffer = New-Object 'byte[]' $length
        $read = $stream.Read($buffer, 0, $length)
        if ($read -le 0) { return '' }
        if ($read -lt $buffer.Length) {
            $trimmed = New-Object 'byte[]' $read
            [Array]::Copy($buffer, 0, $trimmed, 0, $read)
            $buffer = $trimmed
        }
        $offset = 0
        $encoding = [Text.Encoding]::Default
        if ($read -ge 2 -and $buffer[0] -eq 0xFF -and $buffer[1] -eq 0xFE) {
            $encoding = [Text.Encoding]::Unicode
            $offset = 2
        } elseif ($read -ge 2 -and $buffer[0] -eq 0xFE -and $buffer[1] -eq 0xFF) {
            $encoding = [Text.Encoding]::BigEndianUnicode
            $offset = 2
        } elseif ($read -ge 3 -and $buffer[0] -eq 0xEF -and
                $buffer[1] -eq 0xBB -and $buffer[2] -eq 0xBF) {
            $encoding = [Text.Encoding]::UTF8
            $offset = 3
        }
        return $encoding.GetString($buffer, $offset, $read - $offset)
    } catch {
        return ''
    } finally {
        if ($stream) { $stream.Close() }
    }
}

function ConvertTo-CleanRoot([string]$Value) {
    if ($null -eq $Value) { return $null }
    $path = $Value.Trim().Replace('/', '\')
    while ($path.EndsWith('\') -and $path.Length -gt 3) {
        $path = $path.Substring(0, $path.Length - 1)
    }
    if ($path.Length -lt 4) { return $null }
    if ($path -match '[\x00-\x1F\x7F]') { return $null }
    if (-not [IO.Path]::IsPathRooted($path)) { return $null }
    if ($path -match '^[A-Za-z]:([^\\]|$)') { return $null }
    if ($path -match '^[A-Za-z]:\\?$') { return $null }
    if ($path.StartsWith('\\')) { return $null }
    if ($path -match '(^|[\\])\.\.([\\]|$)') { return $null }
    if ($path -match '[*?]') { return $null }
    if ($path.Substring(2) -match ':') { return $null }
    return $path.Substring(0, 1).ToUpperInvariant() + $path.Substring(1)
}

function Get-RootFile([string]$Name) {
    $path = Join-Path $Script:MonitorRoot $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return $path
}

function Get-ApprovedImageRoots {
    $out = @()
    $file = Get-RootFile 'atm-image-roots.txt'
    if (-not $file) { return $out }
    foreach ($line in @(Get-Content -LiteralPath $file -ErrorAction SilentlyContinue)) {
        $value = $line.Trim()
        if (-not $value) { continue }
        if ($value.StartsWith('#')) { continue }
        $root = ConvertTo-CleanRoot $value
        if (-not $root) { continue }
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        if ($out -notcontains $root) { $out += $root }
    }
    return $out
}

function Test-RootRelated([string]$Left, [string]$Right) {
    $a = $Left.TrimEnd([char]'\').ToLowerInvariant()
    $b = $Right.TrimEnd([char]'\').ToLowerInvariant()
    return ($a -eq $b -or $a.StartsWith($b + '\') -or $b.StartsWith($a + '\'))
}

function Get-ImageRoots {
    $approved = @(Get-ApprovedImageRoots)
    $out = @()
    foreach ($root in $approved) {
        if ($out -notcontains $root) { $out += $root }
    }
    # The probe may propose a root the registry has not approved yet. The
    # portal shows it for review and never writes it automatically; a human
    # approves it through the Agent setup page. Only existing directories are
    # proposed, only from this bounded list, and never a parent/child of an
    # approved root: approving a parent would silently widen access.
    foreach ($candidate in @(
        'E:\pic', 'D:\pic', 'C:\pic',
        'E:\ATM\images', 'D:\ATM\images', 'C:\ATM\images',
        'E:\ATM\Images', 'D:\ATM\Images', 'C:\ATM\Images',
        'E:\ATM\captures', 'D:\ATM\captures', 'C:\ATM\captures',
        'E:\images', 'D:\images', 'C:\images'
    )) {
        $root = ConvertTo-CleanRoot $candidate
        if (-not $root) { continue }
        $covered = $false
        foreach ($existing in $approved) {
            if (Test-RootRelated $root $existing) { $covered = $true; break }
        }
        if ($covered) { continue }
        if ($out -contains $root) { continue }
        if (Test-Path -LiteralPath $root -PathType Container) { $out += $root }
    }
    return $out
}

function Get-EjRoots {
    $out = @()
    $config = Get-RootFile 'atm-ej-roots.txt'
    if ($config) {
        foreach ($line in @(Get-Content -LiteralPath $config -ErrorAction SilentlyContinue)) {
            $value = $line.Trim()
            if (-not $value) { continue }
            if ($value.StartsWith('#')) { continue }
            $root = ConvertTo-CleanRoot $value
            if ($root -and (Test-Path -LiteralPath $root -PathType Container) -and
                $out -notcontains $root) {
                $out += $root
            }
        }
    }
    foreach ($candidate in @(
        'E:\EJLogs', 'D:\EJLogs', 'C:\EJLogs',
        'D:\GRG\EJ', 'C:\GRG\EJ',
        'D:\Program Files\DTATMW\EJ'
    )) {
        if ((Test-Path -LiteralPath $candidate -PathType Container) -and
            $out -notcontains $candidate) {
            $out += $candidate
        }
    }
    return $out
}

function Get-NewestLog([string]$Root) {
    $files = @(Get-ChildItem -LiteralPath $Root -Filter '*.log' -ErrorAction SilentlyContinue |
        Where-Object { -not $_.PSIsContainer })
    if ($files.Count -eq 0) { return $null }
    return ($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}

function Get-NcrTerminal([string]$Text) {
    # Receipt date line: "09/09/26  23:32 PATML056". Require a digit in the
    # token so a date line followed by "WITHDRAWAL" is not mistaken for a TID.
    $match = [regex]::Match($Text,
        '\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}\s+([A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)')
    if ($match.Success) { return $match.Groups[1].Value }
    $match = [regex]::Match($Text, '\b(TID[0-9A-Za-z._-]{1,17})\b')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

function Get-GrgTerminal([string]$Text) {
    # Compact GRG journal: "... | GRG-001 | TID006 | VENDOR=GRG |"
    $match = [regex]::Match($Text,
        '\|\s*([A-Za-z0-9][A-Za-z0-9._-]{0,19})\s*\|\s*VENDOR\s*=\s*GRG')
    if ($match.Success) { return $match.Groups[1].Value }
    $match = [regex]::Match($Text, '\|\s*(TID[0-9A-Za-z._-]{1,17})\s*\|')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

function Get-EjInfo([string]$Vendor) {
    $roots = @()
    $newest = $null
    foreach ($root in @(Get-EjRoots)) {
        $candidate = Get-NewestLog $root
        if (-not $candidate) { continue }
        if ($roots -notcontains $root) { $roots += $root }
        if (-not $newest -or
            $candidate.LastWriteTime -gt $newest.LastWriteTime) {
            $newest = $candidate
        }
    }

    $format = $null
    if ($Vendor -eq 'GRG') { $format = 'grg' }
    elseif ($Vendor -eq 'NCR') { $format = 'ncr' }
    elseif ($newest) {
        if ($newest.Name -match '_EJDATA\.log$') { $format = 'ncr' }
        elseif ($newest.Name -match '^EJ.*\.log$') { $format = 'grg' }
    }

    $hostname = $null
    $hostnameSource = 'unknown'
    $terminal = $null
    if ($newest) {
        if ($format -eq 'ncr' -and
            $newest.Name -match '^[^_]+_(.+)_EJDATA\.log$') {
            $hostname = $matches[1]
            $hostnameSource = 'ej-filename'
        } elseif ($format -eq 'grg' -and $env:COMPUTERNAME) {
            $hostname = $env:COMPUTERNAME
            $hostnameSource = 'computer-name'
        }
        $text = Read-TextPrefix $newest.FullName 262144
        if ($format -eq 'ncr') { $terminal = Get-NcrTerminal $text }
        elseif ($format -eq 'grg') { $terminal = Get-GrgTerminal $text }
    }

    return @{
        vendor = $(if ($format -eq 'grg') { 'GRG' }
                   elseif ($format -eq 'ncr') { 'NCR' }
                   else { $null })
        hostname = $hostname
        hostname_source = $hostnameSource
        roots = $roots
        newest_file = $(if ($newest) { $newest.Name } else { $null })
        format = $format
        terminal_id = $terminal
    }
}

function Get-Vendor {
    foreach ($path in @(
        'C:\Program Files (x86)\GrgBanking',
        'C:\Program Files\GrgBanking',
        'D:\Program Files\DTATMW'
    )) {
        if (Test-Path -LiteralPath $path) {
            return @{ value = 'GRG'; source = 'program-files' }
        }
    }
    foreach ($path in @(
        'C:\Program Files\Common Files\NCR',
        'C:\Program Files (x86)\NCR',
        'C:\Program Files\NCR'
    )) {
        if (Test-Path -LiteralPath $path) {
            return @{ value = 'NCR'; source = 'program-files' }
        }
    }
    foreach ($key in @(
        'HKLM:\SOFTWARE\WOW6432Node\GRGFD',
        'HKLM:\SOFTWARE\WOW6432Node\GRGAV',
        'HKLM:\SOFTWARE\GRGFD',
        'HKLM:\SOFTWARE\GRGAV'
    )) {
        if (Test-Path -LiteralPath $key) {
            return @{ value = 'GRG'; source = 'registry' }
        }
    }
    foreach ($key in @(
        'HKLM:\SOFTWARE\XFS\SERVICE_PROVIDERS',
        'HKLM:\SOFTWARE\WOW6432Node\XFS\SERVICE_PROVIDERS'
    )) {
        foreach ($child in @(Get-ChildItem -Path $key -ErrorAction SilentlyContinue)) {
            if ($child.PSChildName -match '(?i)ncr|aptra') {
                return @{ value = 'NCR'; source = 'registry' }
            }
            if ($child.PSChildName -match '(?i)grg') {
                return @{ value = 'GRG'; source = 'registry' }
            }
        }
    }
    return @{ value = $null; source = 'unknown' }
}

function Get-ProbeInfo {
    $out = @{}
    foreach ($name in @(
        'atm-wmi-collector.ps1', 'ncr-xfs-probe.ps1', 'grg-xfs-probe.ps1',
        'atm-image-chunk.ps1', 'atm-identity.ps1'
    )) {
        $path = Join-Path $Script:MonitorRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $version = $null
        $text = ''
        try { $text = [IO.File]::ReadAllText($path) } catch {}
        $match = [regex]::Match($text, "probe_version\s*=\s*'([^']+)'")
        if ($match.Success) { $version = $match.Groups[1].Value }
        $hash = Get-Sha256 $path
        # Do not report a probe we could not read/hash: a null hash would
        # look like a present-but-unverified copy and hide the drift.
        if (-not $hash) { continue }
        $out[$name] = @{
            sha256 = $hash
            version = $version
        }
    }
    return $out
}

function Get-AgentInfo {
    foreach ($entry in @(
        @{ exe = 'C:\Program Files\Zabbix Agent 2\zabbix_agent2.exe';
           conf = 'C:\Program Files\Zabbix Agent 2\zabbix_agent2.conf' },
        @{ exe = 'C:\Program Files\Zabbix Agent\zabbix_agentd.exe';
           conf = 'C:\Program Files\Zabbix Agent\zabbix_agentd.conf' },
        @{ exe = 'C:\Program Files (x86)\Zabbix Agent 2\zabbix_agent2.exe';
           conf = 'C:\Program Files (x86)\Zabbix Agent 2\zabbix_agent2.conf' },
        @{ exe = 'C:\Program Files (x86)\Zabbix Agent\zabbix_agentd.exe';
           conf = 'C:\Program Files (x86)\Zabbix Agent\zabbix_agentd.conf' }
    )) {
        if (-not (Test-Path -LiteralPath $entry.exe -PathType Leaf)) { continue }
        $version = $null
        try {
            $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo(
                $entry.exe).FileVersion
        } catch {}
        $hash = $null
        if (Test-Path -LiteralPath $entry.conf -PathType Leaf) {
            $hash = Get-Sha256 $entry.conf
        }
        return @{ version = $version; config_hash = $hash }
    }
    return @{ version = $null; config_hash = $null }
}

function Get-AppliedProfile {
    # The installer records the canonical hash of the profile it applied in
    # atm-agent-state.json. PowerShell 2.0 has no ConvertFrom-Json, so
    # read the three scalars with anchored regexes. A missing or malformed
    # state file is null evidence, never an invented hash: the portal then
    # says "reapply the profile" instead of claiming the box is current.
    $out = @{ profile_hash = $null; deploy_hash = $null; applied_at = $null }
    $path = Join-Path $Script:MonitorRoot 'atm-agent-state.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $out }
    $text = ''
    try { $text = [IO.File]::ReadAllText($path) } catch { return $out }
    $hash = [regex]::Match($text, '"profile_hash"\s*:\s*"([0-9a-fA-F]{64})"')
    if ($hash.Success) {
        $value = [string]$hash.Groups[1].Value
        $out['profile_hash'] = $value.ToLowerInvariant()
    }
    $deploy = [regex]::Match($text, '"deploy_hash"\s*:\s*"([0-9a-fA-F]{64})"')
    if ($deploy.Success) {
        $value = [string]$deploy.Groups[1].Value
        $out['deploy_hash'] = $value.ToLowerInvariant()
    }
    $when = [regex]::Match($text, '"applied_at"\s*:\s*"([^"]{1,40})"')
    if ($when.Success) { $out['applied_at'] = $when.Groups[1].Value }
    return $out
}

function Get-IpAddresses {
    $out = @()
    try {
        foreach ($address in [System.Net.Dns]::GetHostAddresses($env:COMPUTERNAME)) {
            $value = $address.ToString()
            if (-not $value) { continue }
            if ($value -eq '127.0.0.1' -or $value -eq '::1') { continue }
            if ($out -notcontains $value) { $out += $value }
        }
    } catch {}
    if ($out.Count -gt 16) { $out = $out[0..15] }
    return $out
}

# ── Build and emit one compact JSON line ───────────────────────────────────
$vendorInfo = Get-Vendor
$vendor = $vendorInfo.value
$vendorSource = $vendorInfo.source
$ej = Get-EjInfo $vendor
if (-not $vendor -and $ej.vendor) {
    $vendor = $ej.vendor
    $vendorSource = 'ej-filename'
}

$serial = $null
$bios = Get-ItemProperty -Path 'HKLM:\HARDWARE\DESCRIPTION\System\BIOS' -ErrorAction SilentlyContinue
if ($bios) { $serial = [string]$bios.SystemSerialNumber }

$appliedProfile = Get-AppliedProfile
$agentInfo = Get-AgentInfo
$agentInfo['profile_hash'] = $appliedProfile['profile_hash']
$agentInfo['deploy_hash'] = $appliedProfile['deploy_hash']
$agentInfo['applied_at'] = $appliedProfile['applied_at']

$document = @{
    schema = 1
    collected_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    computer = $env:COMPUTERNAME
    vendor = $vendor
    vendor_source = $vendorSource
    model = $null
    serial = $serial
    terminal_id = $ej.terminal_id
    terminal_source = $(if ($ej.terminal_id) { 'ej-header' } else { 'unknown' })
    ej = @{
        hostname = $ej.hostname
        hostname_source = $ej.hostname_source
        roots = @($ej.roots)
        newest_file = $ej.newest_file
        format = $ej.format
    }
    images = @{
        roots = @(Get-ImageRoots)
        candidates = @()
    }
    ip_addresses = @(Get-IpAddresses)
    probes = (Get-ProbeInfo)
    agent = $agentInfo
}

# Console.WriteLine, not Write-Output: the formatting pipeline wraps long
# lines at console width, which turns valid JSON into a multi-line document
# Zabbix cannot parse. Same trap documented in atm-wmi-collector.ps1.
[System.Console]::WriteLine((ConvertTo-JsonValue $document))
