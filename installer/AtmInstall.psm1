#Requires -Version 2.0
<#
.SYNOPSIS
  Core logic for the Dashen ATM monitoring installer.

.DESCRIPTION
  The installer is a reconciler, not a registrar. It takes the per-ATM profile
  rendered by the portal and makes the box match it:

    * verifies the profile is structurally safe and belongs to the ATM the
      operator accepted;
    * verifies every bundled probe against the SHA-256 in the profile;
    * writes the include, roots file and agent config from the profile's own
      text, never from a second copy of the policy;
    * installs or upgrades Zabbix Agent 2 6.4, replaces a legacy Agent 1
      service only when explicitly allowed;
    * tests the result as the agent's service account, not as the installing
      administrator;
    * records what it wrote so a later run can reconcile rather than guess.

  This module has no side effects on import. `atm-install.ps1` is the entry
  point; the module is dot-sourced by the tests.

  PowerShell 2.0 compatibility: no ConvertFrom-Json, Get-FileHash,
  Get-CimInstance or $PSScriptRoot in the deployment path. Newer hosts use
  their native cmdlets when available.

.NOTES
  Exit codes are part of the contract; see $script:ExitCodes.
#>

$script:InstallerVersion = '2026-10-31.1'
$script:StateFileName = 'atm-agent-state.json'
$script:BackupDirName = 'backup'
$script:AgentServiceName = 'Zabbix Agent 2'
$script:LegacyServiceName = 'Zabbix Agent'
$script:AgentSeries = '6.4'
$script:AgentPort = 10050

$script:ExitCodes = @{
    Ok                  = 0
    InvalidProfile      = 10
    NotReady            = 11
    ProfileHashMismatch = 12
    ProbeMismatch       = 13
    UnsignedProfile     = 14
    AtmIdMismatch       = 15
    WriteFailed         = 20
    AgentInstallFailed  = 21
    LegacyAgentPresent  = 22
    ServiceFailed       = 23
    ConfigTestFailed    = 24
    VerifyFailed        = 25
    Unexpected          = 30
}

function Get-InstallerVersion { return $script:InstallerVersion }

function Get-ExitCode {
    param([string]$Name)
    if ($script:ExitCodes.ContainsKey($Name)) { return [int]$script:ExitCodes[$Name] }
    return [int]$script:ExitCodes.Unexpected
}

# ── JSON ────────────────────────────────────────────────────────────────────
# The portal emits JSON with non-ASCII path text. Keep the reader strict and
# the writer deterministic; the state file is compared by humans and scripts.

function ConvertTo-Hashtable {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $out = @{}
        foreach ($key in $Value.Keys) {
            $out[[string]$key] = ConvertTo-Hashtable $Value[$key]
        }
        return $out
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $out = @{}
        foreach ($prop in $Value.PSObject.Properties) {
            $out[$prop.Name] = ConvertTo-Hashtable $prop.Value
        }
        return $out
    }
    if (($Value -is [System.Collections.IEnumerable]) -and
        -not ($Value -is [string])) {
        $items = @()
        foreach ($item in $Value) { $items += ,(ConvertTo-Hashtable $item) }
        return ,$items
    }
    return $Value
}

function ConvertFrom-JsonCompat {
    param([Parameter(Mandatory=$true)][string]$Json)
    if (Get-Command ConvertFrom-Json -ErrorAction SilentlyContinue) {
        $value = $Json | ConvertFrom-Json
    } else {
        Add-Type -AssemblyName System.Web.Extensions
        $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
        $serializer.MaxJsonLength = [int]::MaxValue
        $value = $serializer.DeserializeObject($Json)
    }
    return (ConvertTo-Hashtable $value)
}

function ConvertTo-JsonString {
    param([string]$Value)
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

function ConvertTo-JsonCompat {
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) {
        if ($Value) { return 'true' }
        return 'false'
    }
    if ($Value -is [string]) { return (ConvertTo-JsonString $Value) }
    if ($Value -is [int] -or $Value -is [long] -or
        $Value -is [uint16] -or $Value -is [uint32] -or
        $Value -is [byte] -or $Value -is [int64]) {
        return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $parts = @()
        foreach ($key in @($Value.Keys | Sort-Object)) {
            $parts += ((ConvertTo-JsonString ([string]$key)) + ':' +
                       (ConvertTo-JsonCompat $Value[$key]))
        }
        return '{' + ($parts -join ',') + '}'
    }
    if (($Value -is [System.Collections.IEnumerable]) -and
        -not ($Value -is [string])) {
        $parts = @()
        foreach ($item in $Value) { $parts += (ConvertTo-JsonCompat $item) }
        return '[' + ($parts -join ',') + ']'
    }
    if ($Value -is [ValueType]) {
        return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    return (ConvertTo-JsonString ([string]$Value))
}

# ── Hashing and paths ──────────────────────────────────────────────────────

function Get-BytesSha256Hex {
    param([byte[]]$Bytes)
    if ($null -eq $Bytes) { return $null }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($Bytes)
    } finally {
        $sha.Clear()
    }
    $sb = New-Object System.Text.StringBuilder
    foreach ($b in $hash) { [void]$sb.Append($b.ToString('x2')) }
    return $sb.ToString()
}

function Get-TextSha256Hex {
    param([string]$Text)
    if ($null -eq $Text) { return $null }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    return (Get-BytesSha256Hex $encoding.GetBytes($Text))
}

function Get-FileSha256Hex {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $sha = $null
    $stream = $null
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $hash = $sha.ComputeHash($stream)
        $sb = New-Object System.Text.StringBuilder
        foreach ($b in $hash) { [void]$sb.Append($b.ToString('x2')) }
        return $sb.ToString()
    } catch {
        return $null
    } finally {
        if ($stream) { $stream.Close() }
        if ($sha) { $sha.Clear() }
    }
}

function Get-NormalizedPath {
    param([string]$Path)
    if (-not $Path) { return $null }
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($root -and $full.Length -gt $root.Length) {
        $separators = [char[]]@([System.IO.Path]::DirectorySeparatorChar,
                                [System.IO.Path]::AltDirectorySeparatorChar)
        $full = $full.TrimEnd($separators)
    }
    return $full
}

function Get-PathComparison {
    if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
        return [System.StringComparison]::OrdinalIgnoreCase
    }
    return [System.StringComparison]::Ordinal
}

function Test-PathUnderRoot {
    param([string]$Path, [string]$Root)
    if (-not $Path -or -not $Root) { return $false }
    try {
        $full = Get-NormalizedPath $Path
        $base = Get-NormalizedPath $Root
    } catch {
        return $false
    }
    $comparison = Get-PathComparison
    if ($full.Equals($base, $comparison)) { return $true }
    $separators = [char[]]@([System.IO.Path]::DirectorySeparatorChar,
                            [System.IO.Path]::AltDirectorySeparatorChar)
    $prefix = $base.TrimEnd($separators) + [System.IO.Path]::DirectorySeparatorChar
    return $full.StartsWith($prefix, $comparison)
}

function Test-SafeAbsolutePath {
    param([string]$Path)
    if (-not $Path) { return $false }
    if ($Path -match '[\x00-\x1F\x7F]') { return $false }
    if (-not [System.IO.Path]::IsPathRooted($Path)) { return $false }
    if ($Path -match '(^|[\\/])\.\.([\\/]|$)') { return $false }
    if ($Path -match '[*?]') { return $false }
    if ($Path.StartsWith('\\')) { return $false }
    if ($Path -match '^[A-Za-z]:[\\/]?$') { return $false }
    # Alternate data stream: a colon after the drive letter.
    if ($Path.Length -gt 2 -and $Path.Substring(2) -match ':') { return $false }
    return $true
}

function Get-LocalPath {
    param([string]$Path)
    if (-not $Path) { return $null }
    if (Test-Path -LiteralPath $Path) { return (Get-NormalizedPath $Path) }
    return $Path
}

# ── Logging ────────────────────────────────────────────────────────────────

function Get-DefaultLogPath {
    $base = $env:ProgramData
    if (-not $base) { $base = [System.IO.Path]::GetTempPath() }
    $dir = Join-Path (Join-Path $base 'AtmMonitor') 'logs'
    return (Join-Path $dir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log'))
}

function Initialize-Log {
    param([string]$LogPath)
    if (-not $LogPath) { return $null }
    $dir = Split-Path -Parent $LogPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    return $LogPath
}

function Write-Log {
    param(
        [string]$Message,
        [string]$Level = 'INFO',
        [string]$LogPath
    )
    $line = ('{0} {1,-5} {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ'), $Level, $Message)
    Write-Host $line
    if ($LogPath) {
        try {
            $encoding = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::AppendAllText($LogPath, $line + "`r`n", $encoding)
        } catch {
            # Logging must never be the reason an install fails.
        }
    }
}

# ── Profile reading and validation ─────────────────────────────────────────

function Get-MapValue {
    param($Map, [string]$Key)
    if ($Map -is [System.Collections.IDictionary] -and $Map.ContainsKey($Key)) {
        return $Map[$Key]
    }
    return $null
}

function Get-ArrayValue {
    param($Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [System.Collections.IDictionary]) { return @($Value) }
    if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        return @($Value)
    }
    return @($Value)
}

function Get-ConfigLines {
    param([string]$Text, [string]$Key)
    $out = @()
    if (-not $Text) { return $out }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match $pattern) { $out += $Matches[1].Trim() }
    }
    return $out
}

function New-CheckResult {
    return @{
        Ok       = $true
        Errors   = @()
        Warnings = @()
    }
}

function Add-CheckError {
    param($Result, [string]$Message)
    $Result.Ok = $false
    $Result.Errors += $Message
}

function Add-CheckWarning {
    param($Result, [string]$Message)
    $Result.Warnings += $Message
}

function Read-ProfileFile {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [string]$ExpectedSha256
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "profile not found: $Path"
    }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $sha = Get-BytesSha256Hex $bytes
    if ($ExpectedSha256) {
        $want = $ExpectedSha256.Trim().ToLowerInvariant()
        if ($want.StartsWith('sha256:')) { $want = $want.Substring(7) }
        if ($sha -ne $want) {
            throw ("profile SHA-256 mismatch: expected {0}, got {1}" -f $want, $sha)
        }
    }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
        $text = $text.Substring(1)
    }
    $document = ConvertFrom-JsonCompat $text
    if (-not ($document -is [System.Collections.IDictionary])) {
        throw 'profile root is not a JSON object'
    }
    return @{
        Path     = (Get-NormalizedPath $Path)
        Sha256   = $sha
        Document = $document
    }
}

function Test-ProfileDocument {
    param(
        $Profile,
        [Parameter(Mandatory=$true)][string]$MonitorRoot,
        [switch]$AllowNotReady,
        [switch]$AllowUnsignedProfile
    )
    $result = New-CheckResult

    if (-not ($Profile -is [System.Collections.IDictionary])) {
        Add-CheckError $result 'profile is not a JSON object'
        return $result
    }

    $schema = Get-MapValue $Profile 'schema'
    if ($null -eq $schema -or [string]$schema -ne '1') {
        Add-CheckError $result "unsupported profile schema: $schema (expected 1)"
    }

    $atmId = [string](Get-MapValue $Profile 'atm_id')
    if ($atmId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$') {
        Add-CheckError $result "ATM id '$atmId' is not a valid Zabbix host name"
    }

    $hostname = [string](Get-MapValue $Profile 'hostname')
    if ($hostname -ne $atmId) {
        Add-CheckError $result "profile hostname '$hostname' does not match ATM id '$atmId'"
    }

    $vendor = [string](Get-MapValue $Profile 'vendor')
    if ($vendor -ne 'NCR' -and $vendor -ne 'GRG') {
        Add-CheckError $result "vendor '$vendor' has no installer contract"
    }

    $ready = Get-MapValue $Profile 'ready'
    if ($ready -ne $true -and -not $AllowNotReady) {
        Add-CheckError $result 'profile is not ready to deploy (pass -AllowNotReady only for the lab)'
    }

    $signature = Get-MapValue $Profile 'signature'
    if (-not $signature -and -not $AllowUnsignedProfile) {
        Add-CheckError $result 'profile is unsigned; pass -AllowUnsignedProfile only for the UAT lab'
    } elseif ($signature -and -not ([string]$signature).StartsWith('hmac-sha256:')) {
        Add-CheckError $result "profile signature '$signature' is not a recognised scheme"
    }

    $profileHash = [string](Get-MapValue $Profile 'profile_hash')
    if ($profileHash -notmatch '^[0-9a-fA-F]{64}$') {
        Add-CheckError $result 'profile_hash is missing or malformed'
    }

    # ── agent block ───────────────────────────────────────────────────────
    $agent = Get-MapValue $Profile 'agent'
    if (-not ($agent -is [System.Collections.IDictionary])) {
        Add-CheckError $result 'profile has no agent block'
        return $result
    }
    $series = [string](Get-MapValue $agent 'series')
    if ($series -ne $script:AgentSeries) {
        Add-CheckError $result "agent series '$series' does not match $($script:AgentSeries)"
    }
    $server = [string](Get-MapValue $agent 'server')
    if (-not $server -or $server -eq 'UNSET') {
        if (-not $AllowNotReady) {
            Add-CheckError $result 'agent server is unset; the profile is not deployable'
        }
    }
    $passiveOnly = Get-MapValue $agent 'passive_only'
    if ($passiveOnly -ne $true) {
        Add-CheckError $result 'profile is not passive-only; this fleet does not allow ServerActive'
    }
    $listenPort = Get-MapValue $agent 'listen_port'
    if ($null -eq $listenPort -or [int]$listenPort -ne $script:AgentPort) {
        Add-CheckError $result "agent listen_port must be $($script:AgentPort) for the opened firewall rule"
    }
    $timeout = Get-MapValue $agent 'timeout'
    if ($null -eq $timeout -or [int]$timeout -lt 3) {
        Add-CheckError $result 'agent timeout must be at least 3 seconds'
    }
    $configText = [string](Get-MapValue $agent 'config_text')
    if (-not $configText) {
        Add-CheckError $result 'profile has no agent config text'
        return $result
    }

    # ── files ─────────────────────────────────────────────────────────────
    $files = @(Get-ArrayValue (Get-MapValue $Profile 'files'))
    if ($files.Count -eq 0) {
        Add-CheckError $result 'profile carries no files to write'
    }
    $includeDest = $null
    $rootsDest = $null
    $seenDest = @{}
    foreach ($file in $files) {
        if (-not ($file -is [System.Collections.IDictionary])) {
            Add-CheckError $result 'profile has a file entry that is not an object'
            continue
        }
        $dest = [string](Get-MapValue $file 'dest')
        $content = Get-MapValue $file 'content'
        if (-not $content -and $content -ne '') {
            Add-CheckError $result "file '$dest' has no content"
        }
        if (-not (Test-SafeAbsolutePath $dest)) {
            Add-CheckError $result "file destination '$dest' is not a safe absolute path"
            continue
        }
        if (-not (Test-PathUnderRoot $dest $MonitorRoot)) {
            Add-CheckError $result "file destination '$dest' is outside $MonitorRoot"
            continue
        }
        if ($seenDest.ContainsKey($dest)) {
            Add-CheckError $result "file destination '$dest' appears twice"
        }
        $seenDest[$dest] = $true
        $leaf = Split-Path -Leaf $dest
        if ($leaf -ieq 'atm-agent.conf') {
            if ($includeDest) { Add-CheckError $result 'profile carries more than one atm-agent.conf' }
            $includeDest = $dest
        } elseif ($leaf -ieq 'atm-image-roots.txt') {
            if ($rootsDest) { Add-CheckError $result 'profile carries more than one roots file' }
            $rootsDest = $dest
        } else {
            Add-CheckError $result "profile writes an unexpected file name '$leaf' under $MonitorRoot"
            continue
        }
    }
    if (-not $includeDest) {
        Add-CheckError $result 'profile does not carry an atm-agent.conf include'
    }

    # The main config is generated from this same profile. Refuse a body
    # that points somewhere else, or that quietly enables outbound checks.
    $serverValues = @(Get-ConfigLines $configText 'Server')
    if ($serverValues.Count -ne 1) {
        Add-CheckError $result 'agent config must contain exactly one Server= line'
    } elseif ($server -and $server -ne 'UNSET' -and $serverValues[0] -ne $server) {
        Add-CheckError $result "agent config Server=$($serverValues[0]) does not match profile server '$server'"
    }
    if (@(Get-ConfigLines $configText 'ServerActive').Count -ne 0) {
        Add-CheckError $result 'agent config contains ServerActive; passive-only is mandatory'
    }
    $hostValues = @(Get-ConfigLines $configText 'Hostname')
    if ($hostValues.Count -ne 1 -or $hostValues[0] -ne $atmId) {
        Add-CheckError $result "agent config Hostname must be exactly '$atmId'"
    }
    $portValues = @(Get-ConfigLines $configText 'ListenPort')
    if ($portValues.Count -ne 1 -or [int]$portValues[0] -ne $script:AgentPort) {
        Add-CheckError $result "agent config ListenPort must be $($script:AgentPort)"
    }
    $timeoutValues = @(Get-ConfigLines $configText 'Timeout')
    if ($timeoutValues.Count -ne 1) {
        Add-CheckError $result 'agent config must contain exactly one Timeout= line'
    } elseif ([int]$timeoutValues[0] -ne [int]$timeout) {
        Add-CheckError $result "agent config Timeout=$($timeoutValues[0]) does not match profile timeout '$timeout'"
    }
    if (@(Get-ConfigLines $configText 'UserParameter').Count -gt 0) {
        Add-CheckError $result 'agent config contains UserParameter; parameters belong in atm-agent.conf'
    }
    if (@(Get-ConfigLines $configText 'LoadModule').Count -gt 0 -or
        @(Get-ConfigLines $configText 'LoadModulePath').Count -gt 0) {
        Add-CheckError $result 'agent config loads a module; the profile contract does not allow that'
    }
    $includeValues = @(Get-ConfigLines $configText 'Include')
    if ($includeValues.Count -ne 1) {
        Add-CheckError $result 'agent config must contain exactly one Include= line'
    } elseif ($includeDest -and $includeValues[0] -ne $includeDest) {
        Add-CheckError $result "agent config Include=$($includeValues[0]) does not match '$includeDest'"
    }
    # Agent 2 has no EnableRemoteCommands switch and no LogRemoteCommands
    # directive. An unknown parameter makes the service exit at startup, and
    # Start-Service only reports "cannot be started", so refuse it here where
    # the reason is visible.
    if (@(Get-ConfigLines $configText 'EnableRemoteCommands').Count -gt 0) {
        Add-CheckError $result 'agent config contains EnableRemoteCommands; Agent 2 uses AllowKey/DenyKey and will not start with that directive'
    }
    if (@(Get-ConfigLines $configText 'LogRemoteCommands').Count -gt 0) {
        Add-CheckError $result 'agent config contains the Agent 1 LogRemoteCommands directive; use Plugins.SystemRun.LogRemoteCommands or omit it'
    }
    foreach ($allow in @(Get-ConfigLines $configText 'AllowKey')) {
        if ($allow -match '(?i)system\.run' -or $allow.Trim() -eq '*') {
            Add-CheckError $result "agent config allows remote commands with AllowKey=$allow"
        }
    }

    # ── probes ────────────────────────────────────────────────────────────
    $probes = @(Get-ArrayValue (Get-MapValue $Profile 'probes'))
    if ($probes.Count -eq 0) {
        Add-CheckError $result 'profile carries no probes'
    }
    $probeNames = @{}
    foreach ($probe in $probes) {
        if (-not ($probe -is [System.Collections.IDictionary])) {
            Add-CheckError $result 'profile has a probe entry that is not an object'
            continue
        }
        $name = [string](Get-MapValue $probe 'name')
        $dest = [string](Get-MapValue $probe 'dest')
        $sha = [string](Get-MapValue $probe 'sha256')
        if ($name -notmatch '^[A-Za-z0-9._-]+\.ps1$') {
            Add-CheckError $result "probe name '$name' is not a safe file name"
            continue
        }
        if (-not (Test-SafeAbsolutePath $dest) -or -not (Test-PathUnderRoot $dest $MonitorRoot)) {
            Add-CheckError $result "probe destination '$dest' is outside $MonitorRoot"
            continue
        }
        if ($sha -notmatch '^[0-9a-fA-F]{64}$') {
            Add-CheckError $result "probe '$name' has no SHA-256"
            continue
        }
        if ($probeNames.ContainsKey($name)) {
            Add-CheckError $result "probe '$name' appears twice"
        }
        $probeNames[$name] = $true
    }
    if ($includeDest -and $probes.Count -gt 0) {
        $includeFile = $files | Where-Object {
            [string](Get-MapValue $_ 'dest') -eq $includeDest
        } | Select-Object -First 1
        if ($includeFile) {
            $includeText = [string](Get-MapValue $includeFile 'content')
            foreach ($name in $probeNames.Keys) {
                if ($includeText -notmatch [regex]::Escape($name)) {
                    Add-CheckWarning $result "probe '$name' is deployed but not referenced by atm-agent.conf"
                }
            }
            $allowedKeys = @(
                'atm.parts', 'atm.part[*]', 'atm.xfs.cdm', 'atm.identity',
                'atm.image.info[*]', 'atm.image.chunk[*]', 'atm.image.dir[*]',
                'atm.image.ziplist[*]', 'atm.image.zipentry[*]'
            )
            foreach ($line in ($includeText -split "`r?`n")) {
                if ($line -match '^\s*UserParameter\s*=\s*([^,]+)\s*,') {
                    $key = $Matches[1].Trim()
                    if ($allowedKeys -notcontains $key) {
                        Add-CheckError $result "atm-agent.conf declares an unknown UserParameter '$key'"
                    }
                }
            }
        }
    }

    # ── image roots ───────────────────────────────────────────────────────
    $imageRoots = @(Get-ArrayValue (Get-MapValue $Profile 'image_roots'))
    foreach ($root in $imageRoots) {
        if (-not (Test-SafeAbsolutePath ([string]$root))) {
            Add-CheckError $result "image root '$root' is not a safe absolute path"
        }
    }
    if ($rootsDest) {
        if ($imageRoots.Count -eq 0) {
            Add-CheckWarning $result 'profile carries a roots file but no approved roots'
        }
        $rootsFile = $files | Where-Object {
            [string](Get-MapValue $_ 'dest') -eq $rootsDest
        } | Select-Object -First 1
        if ($rootsFile) {
            $rootsText = [string](Get-MapValue $rootsFile 'content')
            foreach ($root in $imageRoots) {
                if ($rootsText -notmatch [regex]::Escape([string]$root)) {
                    Add-CheckWarning $result "approved root '$root' is missing from the roots file"
                }
            }
        }
    } elseif ($imageRoots.Count -gt 0) {
        Add-CheckError $result 'profile approves image roots but carries no roots file'
    }

    return $result
}

# ── Deployment plan ────────────────────────────────────────────────────────

function New-PlanItem {
    param(
        [string]$Kind,
        [string]$Path,
        [string]$Action,
        [string]$Reason = '',
        [string]$Source = '',
        [string]$DesiredSha256 = '',
        [string]$CurrentSha256 = '',
        [string]$Name = ''
    )
    return @{
        Kind          = $Kind
        Name          = $Name
        Path          = $Path
        Action        = $Action
        Reason        = $Reason
        Source        = $Source
        DesiredSha256 = $DesiredSha256
        CurrentSha256 = $CurrentSha256
    }
}

function Test-ActionChanged {
    param([string]$Action)
    return ($Action -ne 'unchanged' -and $Action -ne 'skip')
}

function Get-ServiceSafe {
    param([string]$Name)
    if (-not (Get-Command Get-Service -ErrorAction SilentlyContinue)) { return $null }
    try {
        return (Get-Service -Name $Name -ErrorAction SilentlyContinue)
    } catch {
        return $null
    }
}

function Get-Agent2Info {
    $info = @{
        Installed   = $false
        Version     = $null
        Home        = $null
        Exe         = $null
        Conf        = $null
        ServiceName = $script:AgentServiceName
        Status      = $null
        StartName   = $null
    }
    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Zabbix Agent 2')
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Zabbix Agent 2')
    }
    foreach ($home in $candidates) {
        if (-not $home) { continue }
        $exe = Join-Path $home 'zabbix_agent2.exe'
        if (Test-Path -LiteralPath $exe -PathType Leaf) {
            $info.Installed = $true
            $info.Home = $home
            $info.Exe = $exe
            $info.Conf = Join-Path $home 'zabbix_agent2.conf'
            try {
                $info.Version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($exe).FileVersion
            } catch {}
            break
        }
    }
    $service = Get-ServiceSafe $script:AgentServiceName
    if ($service) {
        $info.Installed = $true
        $info.Status = [string]$service.Status
        if (Get-Command Get-WmiObject -ErrorAction SilentlyContinue) {
            try {
                $wmi = Get-WmiObject -Class Win32_Service -Filter ("Name='" + $script:AgentServiceName + "'") -ErrorAction SilentlyContinue
                if ($wmi) { $info.StartName = $wmi.StartName }
            } catch {}
        }
    }
    return $info
}

function Get-LegacyAgentInfo {
    $info = @{ Present = $false; Status = $null }
    $service = Get-ServiceSafe $script:LegacyServiceName
    if ($service) {
        $info.Present = $true
        $info.Status = [string]$service.Status
    }
    return $info
}

function Get-StatePath {
    param([string]$MonitorRoot)
    return (Join-Path $MonitorRoot $script:StateFileName)
}

function Read-InstallerState {
    param([string]$MonitorRoot)
    $path = Get-StatePath $MonitorRoot
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        $raw = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
        $state = ConvertFrom-JsonCompat $raw
        if ($state -is [System.Collections.IDictionary]) { return $state }
    } catch {}
    return $null
}

function Test-VersionNewer {
    param([string]$Current, [string]$Wanted)
    if (-not $Current -or -not $Wanted) { return $false }
    try {
        $currentVersion = [version]($Current -replace '[^0-9.].*$', '')
        $wantedVersion = [version]($Wanted -replace '[^0-9.].*$', '')
    } catch {
        return $false
    }
    if ($currentVersion.Major -gt $wantedVersion.Major) { return $true }
    if ($currentVersion.Major -eq $wantedVersion.Major -and
        $currentVersion.Minor -gt $wantedVersion.Minor) { return $true }
    return $false
}

function Test-IsAddressLiteral {
    param([string]$Value)
    foreach ($part in ($Value -split ',')) {
        $candidate = $part.Trim()
        if (-not $candidate) { continue }
        if ($candidate -match '^[0-9A-Fa-f:.]+/[0-9]{1,3}$') { continue }
        $address = $null
        if (-not [System.Net.IPAddress]::TryParse($candidate, [ref]$address)) { return $false }
    }
    return $true
}

function Test-ServerResolution {
    param([string]$Server)
    if (-not $Server -or $Server -eq 'UNSET') { return $true }
    foreach ($part in ($Server -split ',')) {
        $candidate = $part.Trim()
        if (-not $candidate) { continue }
        if (Test-IsAddressLiteral $candidate) { continue }
        try {
            $addresses = [System.Net.Dns]::GetHostAddresses($candidate)
            if (-not $addresses -or $addresses.Count -eq 0) { return $false }
        } catch {
            return $false
        }
    }
    return $true
}

function Get-InstallPlan {
    param(
        [Parameter(Mandatory=$true)]$Profile,
        [Parameter(Mandatory=$true)][string]$MonitorRoot,
        [Parameter(Mandatory=$true)][string]$AgentConf,
        [Parameter(Mandatory=$true)][string]$ProbeRoot,
        [string]$AgentMsi,
        [string]$AgentVersion = $script:AgentSeries,
        [switch]$NoAgentInstall,
        [switch]$NoService,
        [switch]$ReplaceLegacyAgent,
        [switch]$Reassign
    )
    $items = @()
    $errors = @()
    $warnings = @()

    $state = Read-InstallerState $MonitorRoot
    $stateAtm = [string](Get-MapValue $state 'atm_id')
    $profileAtm = [string](Get-MapValue $Profile 'atm_id')
    if ($stateAtm -and $stateAtm -ne $profileAtm -and -not $Reassign) {
        $errors += "monitor root is already managed for $stateAtm; refusing to apply $profileAtm without -Reassign"
    }

    $server = [string](Get-MapValue (Get-MapValue $Profile 'agent') 'server')
    $serverSource = [string](Get-MapValue (Get-MapValue $Profile 'agent') 'server_source')
    if ($server -and $server -ne 'UNSET' -and $serverSource -and
        $serverSource -ne 'district-proxy') {
        $warnings += "agent Server=$server came from $serverSource, not the district proxy; confirm this is intentional"
    }
    if ($server -and $server -ne 'UNSET' -and -not (Test-ServerResolution $server)) {
        $warnings += "agent Server=$server does not resolve on this box; the agent cannot match the proxy until DNS or a hosts entry exists"
    }

    $managed = @()
    foreach ($probe in @(Get-ArrayValue (Get-MapValue $Profile 'probes'))) {
        $name = [string](Get-MapValue $probe 'name')
        $dest = [string](Get-MapValue $probe 'dest')
        $want = [string](Get-MapValue $probe 'sha256')
        $source = Join-Path $ProbeRoot $name
        $sourceSha = Get-FileSha256Hex $source
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            $errors += "bundled probe is missing: $name"
            continue
        }
        if ($sourceSha -ne $want.ToLowerInvariant()) {
            $errors += "bundled probe $name does not match the profile (bundle $sourceSha, profile $want)"
            continue
        }
        $current = Get-FileSha256Hex $dest
        if (-not $current) {
            $action = 'create'
        } elseif ($current -eq $want.ToLowerInvariant()) {
            $action = 'unchanged'
        } else {
            $action = 'update'
        }
        $items += (New-PlanItem -Kind 'probe' -Name $name -Path $dest -Action $action `
            -Source $source -DesiredSha256 $want.ToLowerInvariant() -CurrentSha256 $current)
        $managed += $dest
    }

    foreach ($file in @(Get-ArrayValue (Get-MapValue $Profile 'files'))) {
        $dest = [string](Get-MapValue $file 'dest')
        $content = [string](Get-MapValue $file 'content')
        $want = Get-TextSha256Hex $content
        $current = Get-FileSha256Hex $dest
        if (-not $current) {
            $action = 'create'
        } elseif ($current -eq $want) {
            $action = 'unchanged'
        } else {
            $action = 'update'
        }
        $managed += $dest
        $items += (New-PlanItem -Kind 'file' -Path $dest -Action $action `
            -DesiredSha256 $want -CurrentSha256 $current)
    }

    # A roots file from a previous run is dangerous left behind after the
    # registry removes every approved root. Remove only a file this installer
    # recorded as managed; never delete a file the bank created.
    $rootsDest = $null
    foreach ($file in @(Get-ArrayValue (Get-MapValue $Profile 'files'))) {
        $dest = [string](Get-MapValue $file 'dest')
        if ((Split-Path -Leaf $dest) -ieq 'atm-image-roots.txt') { $rootsDest = $dest }
    }
    $imageRoots = @(Get-ArrayValue (Get-MapValue $Profile 'image_roots'))
    if (-not $rootsDest -and $imageRoots.Count -eq 0 -and $state) {
        foreach ($old in @(Get-ArrayValue (Get-MapValue $state 'managed_files'))) {
            if ((Split-Path -Leaf ([string]$old)) -ieq 'atm-image-roots.txt' -and
                (Test-Path -LiteralPath ([string]$old) -PathType Leaf)) {
                $items += (New-PlanItem -Kind 'file' -Path ([string]$old) -Action 'remove' `
                    -Reason 'no approved image roots remain')
                $managed += [string]$old
            }
        }
    }

    # The main config lives outside C:\monitor. It is still fully rendered by
    # the profile; the installer only chooses the destination file.
    $confContent = [string](Get-MapValue (Get-MapValue $Profile 'agent') 'config_text')
    $confWant = Get-TextSha256Hex $confContent
    $confCurrent = Get-FileSha256Hex $AgentConf
    if (-not $confCurrent) {
        $confAction = 'create'
    } elseif ($confCurrent -eq $confWant) {
        $confAction = 'unchanged'
    } else {
        $confAction = 'update'
    }
    $items += (New-PlanItem -Kind 'agent_conf' -Path $AgentConf -Action $confAction `
        -DesiredSha256 $confWant -CurrentSha256 $confCurrent)
    $managed += $AgentConf

    $agent = Get-Agent2Info
    $agentChanged = $false
    if (-not $agent.Installed) {
        if ($NoAgentInstall) {
            $warnings += 'Zabbix Agent 2 is not installed; -NoAgentInstall leaves that step to the operator'
            $items += (New-PlanItem -Kind 'agent' -Action 'skip' -Reason 'agent not installed')
        } else {
            $msi = $AgentMsi
            if (-not $msi) {
                $payloadRoot = Split-Path -Parent $ProbeRoot
                if ($payloadRoot) {
                    $agentDir = Join-Path $payloadRoot 'agent'
                    $candidates = @(Get-ChildItem -LiteralPath $agentDir -Filter '*.msi' -ErrorAction SilentlyContinue)
                    if ($candidates.Count -gt 0) { $msi = $candidates[0].FullName }
                }
            }
            if (-not $msi -or -not (Test-Path -LiteralPath $msi -PathType Leaf)) {
                $errors += 'Zabbix Agent 2 is not installed and no MSI is bundled'
            } else {
                $items += (New-PlanItem -Kind 'agent' -Action 'install' -Source $msi `
                    -Reason 'Zabbix Agent 2 is not installed')
                $agentChanged = $true
            }
        }
    } else {
        $currentVersion = [string]$agent.Version
        $wanted = [string]$AgentVersion
        if (Test-VersionNewer $currentVersion $wanted) {
            $errors += "installed Agent 2 $currentVersion is newer than the profile series $wanted; refusing to downgrade"
        } elseif ($currentVersion -and $wanted -and $currentVersion -notlike "$wanted*") {
            if ($NoAgentInstall) {
                $warnings += "agent version $currentVersion differs from $wanted; upgrade skipped"
            } elseif ($AgentMsi -and (Test-Path -LiteralPath $AgentMsi -PathType Leaf)) {
                $items += (New-PlanItem -Kind 'agent' -Action 'upgrade' -Source $AgentMsi `
                    -Reason "agent version $currentVersion differs from $wanted")
                $agentChanged = $true
            } else {
                $warnings += "agent version $currentVersion differs from $wanted; no MSI supplied"
            }
        } else {
            $items += (New-PlanItem -Kind 'agent' -Action 'unchanged' -Reason "agent version $currentVersion")
        }
    }

    $legacy = Get-LegacyAgentInfo
    if ($legacy.Present -and $legacy.Status -eq 'Running') {
        if ($ReplaceLegacyAgent) {
            $items += (New-PlanItem -Kind 'legacy' -Action 'stop' `
                -Reason 'Zabbix Agent 1 must release TCP 10050 for Agent 2')
            $agentChanged = $true
        } else {
            $errors += 'legacy Zabbix Agent 1 is running; pass -ReplaceLegacyAgent to stop and disable it'
        }
    } elseif ($legacy.Present) {
        $warnings += "legacy Zabbix Agent 1 is installed but $($legacy.Status); it will not be started"
    }

    $serviceAction = 'unchanged'
    if (-not $NoService -and -not $NoAgentInstall) {
        if ($agentChanged -or $confAction -ne 'unchanged') {
            $serviceAction = 'restart'
        } elseif ($agent.Installed -and $agent.Status -ne 'Running') {
            $serviceAction = 'start'
        }
        if ($agent.Installed -or $agentChanged) {
            $items += (New-PlanItem -Kind 'service' -Action $serviceAction)
        }
    }

    return @{
        Items    = $items
        Errors   = $errors
        Warnings = $warnings
        Changed  = [bool](@($items | Where-Object { Test-ActionChanged $_.Action }).Count -gt 0)
        Managed  = $managed
        Agent    = $agent
        Legacy   = $legacy
    }
}

# ── Apply ──────────────────────────────────────────────────────────────────

function Write-TextFileAtomic {
    param([string]$Path, [string]$Content)
    $dir = Split-Path -Parent $Path
    if (-not $dir) { throw "destination '$Path' has no parent directory" }
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $tmp = Join-Path $dir ('.atm-install-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($tmp, $Content, $encoding)
        Move-Item -LiteralPath $tmp -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
}

function Copy-ProbeFile {
    param([string]$Source, [string]$Destination, [string]$ExpectedSha256)
    $dir = Split-Path -Parent $Destination
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    $actual = Get-FileSha256Hex $Destination
    if ($ExpectedSha256 -and $actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "probe copy to $Destination failed hash verification"
    }
}

function Backup-PlanTargets {
    param($Plan, [string]$MonitorRoot)
    $targets = @()
    foreach ($item in $Plan.Items) {
        if ((Test-ActionChanged $item.Action) -and $item.Path -and
            (Test-Path -LiteralPath $item.Path -PathType Leaf)) {
            $targets += $item
        }
    }
    if ($targets.Count -eq 0) { return $null }
    $statePath = Get-StatePath $MonitorRoot
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $targets += (New-PlanItem -Kind 'state' -Path $statePath -Action 'update')
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmssfff'
    $root = Join-Path (Join-Path $MonitorRoot $script:BackupDirName) $stamp
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $index = 0
    foreach ($item in $targets) {
        $index++
        $leaf = Split-Path -Leaf $item.Path
        $destination = Join-Path $root ('{0:d3}-{1}' -f $index, $leaf)
        Copy-Item -LiteralPath $item.Path -Destination $destination -Force
    }
    return $root
}

function Install-Agent2Msi {
    param(
        [string]$Msi,
        [string]$Server,
        [string]$Hostname,
        [int]$ListenPort = 10050,
        [int]$Timeout = 30
    )
    if (-not (Test-Path -LiteralPath $Msi -PathType Leaf)) {
        throw "agent MSI not found: $Msi"
    }
    $msiLog = Join-Path ([System.IO.Path]::GetTempPath()) (
        'atm-agent-msi-' + [guid]::NewGuid().ToString('N') + '.log')
    $arguments = @(
        '/i', ('"' + $Msi + '"'), '/qn', '/norestart',
        '/l*v', ('"' + $msiLog + '"')
    )
    # Seed a valid passive-only config during the MSI so the service never
    # starts on the package default (127.0.0.1). The exact profile text is
    # written immediately afterwards.
    if ($Server -and $Server -ne 'UNSET') { $arguments += ('SERVER=' + $Server) }
    if ($Hostname) { $arguments += ('HOSTNAME=' + $Hostname) }
    $arguments += ('LISTENPORT=' + $ListenPort)
    $arguments += ('TIMEOUT=' + $Timeout)
    $arguments += 'ENABLEREMOTECOMMANDS=0'
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments `
        -Wait -PassThru -NoNewWindow
    $code = $process.ExitCode
    if ($code -ne 0 -and $code -ne 3010) {
        throw "msiexec failed with exit code $code; log: $msiLog"
    }
    return @{
        ExitCode       = $code
        Log            = $msiLog
        RebootRequired = ($code -eq 3010)
    }
}

function Stop-LegacyAgent {
    $service = Get-ServiceSafe $script:LegacyServiceName
    if (-not $service) { return }
    if ($service.Status -ne 'Stopped') {
        Stop-Service -Name $script:LegacyServiceName -Force -ErrorAction Stop
        $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    & sc.exe config $script:LegacyServiceName start= disabled | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "could not disable the legacy agent service (sc exit $LASTEXITCODE)"
    }
}

function Set-AgentService {
    param([string]$Action, [string]$LogPath)
    if ($Action -eq 'unchanged' -or $Action -eq 'skip') { return }
    $service = Get-ServiceSafe $script:AgentServiceName
    if (-not $service) {
        throw "the '$($script:AgentServiceName)' service does not exist after install"
    }
    & sc.exe config $script:AgentServiceName start= auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "sc config failed with exit code $LASTEXITCODE"
    }
    if ($LogPath) { Write-Log ("service status before ${Action}: " + $service.Status) -LogPath $LogPath }
    if ($Action -eq 'restart') {
        # msiexec can return while the newly installed service is still
        # StartPending. A stop request in that state fails with 1061
        # ("cannot accept control messages"), so wait for Running first.
        if ($service.Status -eq 'StartPending') {
            $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
        }
        if ($service.Status -eq 'StopPending') {
            $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
        }
        if ($service.Status -ne 'Stopped') {
            # Retry a transient 1061 while the SCM settles after the MSI's
            # own start; give up only after the same 30-second budget.
            $deadline = (Get-Date).AddSeconds(30)
            while ($true) {
                try {
                    Stop-Service -Name $script:AgentServiceName -Force -ErrorAction Stop
                    break
                } catch {
                    $service.Refresh()
                    if ($service.Status -eq 'Stopped') { break }
                    if ((Get-Date) -ge $deadline) { throw }
                    Start-Sleep -Milliseconds 500
                }
            }
            $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
        }
        Start-Service -Name $script:AgentServiceName -ErrorAction Stop
    } elseif ($Action -eq 'start') {
        if ($service.Status -eq 'StopPending') {
            $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
        }
        if ($service.Status -eq 'Stopped') {
            Start-Service -Name $script:AgentServiceName -ErrorAction Stop
        }
    }
    $service = Get-Service -Name $script:AgentServiceName
    $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    if ($LogPath) { Write-Log ("service status after ${Action}: " + $service.Status) -LogPath $LogPath }
}

function Get-AgentTestValue {
    param([string]$Output)
    # zabbix_agent2 -t prints "<key> [<type>|<value>]" (the key is padded to
    # 46 columns), not the bare value. Probe values are one line, so match the
    # result line and take the last "]" in case the value contains brackets.
    $text = ([string]$Output).Trim()
    if ($text -match '(?m)^[^\r\n]*?\[(?<type>[dmstu])\|(?<value>.*)\]$') {
        return $Matches['value']
    }
    return $null
}

function Test-AgentConfig {
    param([string]$Exe, [string]$Conf)
    # The pinned Agent 2 6.4 has no -T (that is the Agent 1 config-test flag).
    # -t parses the configuration and evaluates one built-in item; -p would
    # instead probe
    # every known item, which takes minutes on a real ATM and runs user
    # parameters. A broken config makes either flag exit non-zero.
    $output = & $Exe -c $Conf -t agent.ping 2>&1
    $code = $LASTEXITCODE
    $text = (($output | ForEach-Object { [string]$_ }) -join "`n")
    $value = Get-AgentTestValue $text
    if ($null -ne $value) { $value = $value.Trim() }
    if ($text.Length -gt 2000) { $text = $text.Substring(0, 2000) + '...' }
    return @{
        Ok       = ($code -eq 0 -and $value -eq '1')
        ExitCode = $code
        Output   = $text
    }
}

function Test-TcpPort {
    param(
        [string]$HostName = '127.0.0.1',
        [int]$Port = 10050,
        [int]$TimeoutMs = 3000
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-TcpPort {
    param([int]$Port = 10050, [int]$TimeoutSeconds = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort -Port $Port -TimeoutMs 1000) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-ServiceAccountName {
    if (-not (Get-Command Get-WmiObject -ErrorAction SilentlyContinue)) { return $null }
    try {
        $wmi = Get-WmiObject -Class Win32_Service -Filter ("Name='" + $script:AgentServiceName + "'") -ErrorAction SilentlyContinue
        if ($wmi) { return [string]$wmi.StartName }
    } catch {}
    return $null
}

function Invoke-SelfVerification {
    param(
        $Profile,
        [string]$MonitorRoot,
        [string]$AgentConf,
        [string]$LogPath
    )
    $checks = @()
    $ok = $true

    foreach ($probe in @(Get-ArrayValue (Get-MapValue $Profile 'probes'))) {
        $name = [string](Get-MapValue $probe 'name')
        $dest = [string](Get-MapValue $probe 'dest')
        $want = ([string](Get-MapValue $probe 'sha256')).ToLowerInvariant()
        $actual = Get-FileSha256Hex $dest
        $passed = ($actual -eq $want)
        $checks += @{ name = "probe:$name"; ok = $passed; detail = $(if ($passed) { 'hash matches' } else { "expected $want, got $actual" }) }
        if (-not $passed) { $ok = $false }
    }

    foreach ($file in @(Get-ArrayValue (Get-MapValue $Profile 'files'))) {
        $dest = [string](Get-MapValue $file 'dest')
        $want = Get-TextSha256Hex ([string](Get-MapValue $file 'content'))
        $actual = Get-FileSha256Hex $dest
        $passed = ($actual -eq $want)
        $checks += @{ name = ('file:' + (Split-Path -Leaf $dest)); ok = $passed; detail = $(if ($passed) { 'content matches' } else { 'content differs' }) }
        if (-not $passed) { $ok = $false }
    }

    $confWant = Get-TextSha256Hex ([string](Get-MapValue (Get-MapValue $Profile 'agent') 'config_text'))
    $confActual = Get-FileSha256Hex $AgentConf
    $confPassed = ($confActual -eq $confWant)
    $checks += @{ name = 'agent_config'; ok = $confPassed; detail = $(if ($confPassed) { 'content matches' } else { 'content differs' }) }
    if (-not $confPassed) { $ok = $false }

    if ($LogPath) { Write-Log 'service-account verification: hashes complete' -LogPath $LogPath }

    $agent = Get-Agent2Info
    if ($agent.Exe) {
        $configTest = Test-AgentConfig -Exe $agent.Exe -Conf $AgentConf
        $checks += @{ name = 'agent_config_test'; ok = $configTest.Ok; detail = $configTest.Output }
        if (-not $configTest.Ok) { $ok = $false }

        $includeText = ''
        foreach ($file in @(Get-ArrayValue (Get-MapValue $Profile 'files'))) {
            if ((Split-Path -Leaf ([string](Get-MapValue $file 'dest'))) -ieq 'atm-agent.conf') {
                $includeText = [string](Get-MapValue $file 'content')
            }
        }
        $keys = @()
        if ($includeText -match 'UserParameter=atm\.identity,') { $keys += 'atm.identity' }
        if ($includeText -match 'UserParameter=atm\.parts,') { $keys += 'atm.parts' }
        if ($includeText -match 'UserParameter=atm\.xfs\.cdm,') { $keys += 'atm.xfs.cdm' }
        foreach ($key in $keys) {
            $output = & $agent.Exe -t $key -c $AgentConf 2>&1
            $code = $LASTEXITCODE
            $text = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
            # -t prints "<key> [<type>|<value>]"; checking the raw line for a
            # leading "{" would fail even when the probe returned valid JSON.
            $value = Get-AgentTestValue $text
            $passed = ($code -eq 0 -and $null -ne $value -and
                       $value.Length -gt 1 -and
                       $value -notmatch 'ZBX_NOTSUPPORTED' -and
                       $value.TrimStart().StartsWith('{'))
            $detail = $text
            if ($detail.Length -gt 300) { $detail = $detail.Substring(0, 300) + '...' }
            $checks += @{ name = "key:$key"; ok = $passed; detail = $detail }
            if (-not $passed) { $ok = $false }
        }
    } else {
        $checks += @{ name = 'agent_executable'; ok = $false; detail = 'zabbix_agent2.exe not found' }
        $ok = $false
    }

    $account = $null
    try {
        $account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    } catch {
        $account = [System.Environment]::UserName
    }

    return @{
        schema          = 1
        ok              = $ok
        service_account = $account
        tested_at       = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        checks          = $checks
    }
}

function Get-VerificationFailureMessage {
    param($Result)
    # A failed verification used to surface as an empty message, which hid the
    # one check that actually broke. Name every failing check and keep the
    # detail on one line so the installer log stays readable.
    $failed = @($Result['checks'] | Where-Object { $_.ok -ne $true })
    if ($failed.Count -eq 0) {
        return 'verification returned ok=false without a failed check'
    }
    $parts = @()
    foreach ($check in $failed) {
        $detail = (([string]$check['detail']) -replace '\s+', ' ').Trim()
        if ($detail.Length -gt 300) { $detail = $detail.Substring(0, 300) + '...' }
        $parts += ([string]$check['name'] + ': ' + $detail)
    }
    return ('failed checks: ' + ($parts -join '; '))
}

function Invoke-ServiceAccountVerification {
    param(
        [string]$ScriptPath,
        [string]$ProfilePath,
        [string]$MonitorRoot,
        [string]$AgentConf,
        [string]$LogPath,
        [int]$TimeoutSeconds = 180
    )
    if (-not (Get-Command schtasks.exe -ErrorAction SilentlyContinue)) {
        return @{ Ok = $false; Status = 'unavailable'; Message = 'schtasks is not available' }
    }
    $account = Get-ServiceAccountName
    if (-not $account) {
        return @{ Ok = $false; Status = 'unknown'; Message = 'cannot read the agent service account' }
    }
    if ($account -notmatch '(?i)(LocalSystem|NT AUTHORITY\\SYSTEM|^SYSTEM$)') {
        return @{
            Ok      = $false
            Status  = 'skipped'
            Message = "agent runs as $account; run the probe as that account manually"
        }
    }

    $work = Join-Path $env:ProgramData 'AtmMonitor\verify'
    if (-not $env:ProgramData) {
        $work = Join-Path ([System.IO.Path]::GetTempPath()) 'AtmMonitor\verify'
    }
    if (-not (Test-Path -LiteralPath $work)) {
        New-Item -ItemType Directory -Path $work -Force | Out-Null
    }
    $id = [guid]::NewGuid().ToString('N')
    $taskName = 'AtmMonitorVerify-' + $id
    $outFile = Join-Path $work ('verify-' + $id + '.json')
    $wrapper = Join-Path $work ('verify-' + $id + '.cmd')
    $wrapperLog = Join-Path $work ('verify-' + $id + '.log')

    $command = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        $ScriptPath + '" -VerifyOnly -Profile "' + $ProfilePath +
        '" -MonitorRoot "' + $MonitorRoot + '" -AgentConf "' + $AgentConf +
        '" -Out "' + $outFile + '"'
    if ($LogPath) { $command += ' -LogPath "' + $LogPath + '"' }
    $command += ' >> "' + $wrapperLog + '" 2>&1'
    $batch = "@echo off`r`n" + $command + "`r`nexit /b %ERRORLEVEL%`r`n"
    [System.IO.File]::WriteAllText($wrapper, $batch, [System.Text.Encoding]::Default)

    try {
        & schtasks.exe /Create /TN $taskName /TR ('cmd.exe /c "' + $wrapper + '"') `
            /SC ONCE /ST 00:00 /RU SYSTEM /RL HIGHEST /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            return @{ Ok = $false; Status = 'failed'; Message = "schtasks create failed with $LASTEXITCODE" }
        }
        & schtasks.exe /Run /TN $taskName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            return @{ Ok = $false; Status = 'failed'; Message = "schtasks run failed with $LASTEXITCODE" }
        }
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $outFile -PathType Leaf) { break }
            Start-Sleep -Seconds 2
        }
        if (-not (Test-Path -LiteralPath $outFile -PathType Leaf)) {
            $query = & schtasks.exe /Query /TN $taskName /FO LIST /V 2>&1
            $message = ('service-account verification did not finish: ' +
                        (($query | ForEach-Object { [string]$_ }) -join ' '))
            if (Test-Path -LiteralPath $wrapperLog) {
                $lines = @(Get-Content -LiteralPath $wrapperLog -ErrorAction SilentlyContinue)
                if ($lines.Count -gt 20) {
                    $lines = $lines[($lines.Count - 20)..($lines.Count - 1)]
                }
                if ($lines.Count -gt 0) {
                    $logTail = (($lines | ForEach-Object { [string]$_ }) -join ' ')
                    $message = $message + '; log: ' + $logTail
                }
            }
            return @{ Ok = $false; Status = 'failed'; Message = $message }
        }
        $raw = [System.IO.File]::ReadAllText($outFile, [System.Text.Encoding]::UTF8)
        $result = ConvertFrom-JsonCompat $raw
        if ($result['ok'] -eq $true) {
            $result['Status'] = 'passed'
        } else {
            $result['Status'] = 'failed'
            $result['Message'] = Get-VerificationFailureMessage $result
            if ($LogPath) {
                Write-Log ('service-account verification: ' + $result['Message']) 'ERROR' $LogPath
            }
        }
        return $result
    } finally {
        & schtasks.exe /Delete /TN $taskName /F 2>&1 | Out-Null
        if (Test-Path -LiteralPath $wrapper) { Remove-Item -LiteralPath $wrapper -Force }
    }
}

function Write-InstallerState {
    param(
        $Profile,
        [string]$ProfilePath,
        [string]$ProfileSha256,
        [string]$MonitorRoot,
        [string]$AgentConf,
        [string]$AgentVersion,
        [string]$Backup,
        [string]$ServiceAccountStatus,
        [int]$ChangedCount,
        $Managed
    )
    $state = @{
        schema                = 1
        atm_id                = [string](Get-MapValue $Profile 'atm_id')
        profile_hash          = [string](Get-MapValue $Profile 'profile_hash')
        profile_sha256        = $ProfileSha256
        profile_file          = $ProfilePath
        applied_at            = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        installer_version     = $script:InstallerVersion
        vendor                = [string](Get-MapValue $Profile 'vendor')
        server                = [string](Get-MapValue (Get-MapValue $Profile 'agent') 'server')
        agent_version         = $AgentVersion
        agent_conf            = $AgentConf
        managed_files         = @($Managed)
        service_account_check = $ServiceAccountStatus
        changed_actions       = $ChangedCount
        backup                = $Backup
    }
    Write-TextFileAtomic -Path (Get-StatePath $MonitorRoot) -Content (ConvertTo-JsonCompat $state)
    return $state
}

function Get-ProfileFileContent {
    param($Profile, [string]$Dest)
    foreach ($file in @(Get-ArrayValue (Get-MapValue $Profile 'files'))) {
        if ([string](Get-MapValue $file 'dest') -eq $Dest) {
            return [string](Get-MapValue $file 'content')
        }
    }
    return $null
}

function Invoke-InstallPlan {
    param(
        $Plan,
        $Profile,
        [string]$ScriptPath,
        [string]$ProfilePath,
        [string]$ProfileSha256,
        [string]$MonitorRoot,
        [string]$AgentConf,
        [string]$AgentVersion,
        [string]$LogPath,
        [switch]$NoService,
        [switch]$NoServiceAccountTest,
        [switch]$AllowUnverifiedServiceAccount,
        [switch]$DryRun
    )
    if ($Plan.Errors.Count -gt 0) {
        throw ('preflight failed: ' + ($Plan.Errors -join '; '))
    }

    $summary = @{
        ok                 = $true
        dry_run            = [bool]$DryRun
        atm_id             = [string](Get-MapValue $Profile 'atm_id')
        profile_hash       = [string](Get-MapValue $Profile 'profile_hash')
        changed            = [bool]$Plan.Changed
        actions            = @($Plan.Items | ForEach-Object {
            @{ kind = $_.Kind; action = $_.Action; path = $_.Path; reason = $_.Reason }
        })
        backup             = $null
        verification       = $null
        installer_version  = $script:InstallerVersion
    }
    if ($DryRun) { return $summary }

    $backup = Backup-PlanTargets -Plan $Plan -MonitorRoot $MonitorRoot
    $summary.backup = $backup
    if ($backup -and $LogPath) { Write-Log "backup written to $backup" -LogPath $LogPath }

    # The agent config points its log here. Create it before the service
    # starts; the service account then inherits write access from the root.
    $agentLogDir = Join-Path $MonitorRoot 'logs'
    if (-not (Test-Path -LiteralPath $agentLogDir)) {
        New-Item -ItemType Directory -Path $agentLogDir -Force | Out-Null
    }

    foreach ($item in @($Plan.Items | Where-Object { $_.Kind -eq 'legacy' -and (Test-ActionChanged $_.Action) })) {
        if ($LogPath) { Write-Log 'stopping the legacy Zabbix Agent 1 service' -LogPath $LogPath }
        Stop-LegacyAgent
    }

    $profileAgent = Get-MapValue $Profile 'agent'
    $profileServer = [string](Get-MapValue $profileAgent 'server')
    $profileTimeout = [int](Get-MapValue $profileAgent 'timeout')
    if ($profileTimeout -lt 3) { $profileTimeout = 30 }
    $profileAtmId = [string](Get-MapValue $Profile 'atm_id')
    foreach ($item in @($Plan.Items | Where-Object { $_.Kind -eq 'agent' -and ($_.Action -eq 'install' -or $_.Action -eq 'upgrade') })) {
        if ($LogPath) { Write-Log ("installing Zabbix Agent 2 from " + $item.Source) -LogPath $LogPath }
        $msiResult = Install-Agent2Msi -Msi $item.Source -Server $profileServer `
            -Hostname $profileAtmId -ListenPort $script:AgentPort -Timeout $profileTimeout
        if ($msiResult.RebootRequired -and $LogPath) {
            Write-Log 'msiexec requested a reboot (3010); the agent may need one before it starts' 'WARN' $LogPath
        }
    }

    foreach ($item in @($Plan.Items | Where-Object { $_.Kind -eq 'probe' -and (Test-ActionChanged $_.Action) })) {
        if ($LogPath) { Write-Log ("writing probe " + $item.Name) -LogPath $LogPath }
        Copy-ProbeFile -Source $item.Source -Destination $item.Path -ExpectedSha256 $item.DesiredSha256
    }

    foreach ($item in @($Plan.Items | Where-Object { $_.Kind -eq 'file' -and $_.Action -eq 'remove' })) {
        if ($LogPath) { Write-Log ("removing stale " + $item.Path) -LogPath $LogPath }
        Remove-Item -LiteralPath $item.Path -Force
    }
    foreach ($item in @($Plan.Items | Where-Object { $_.Kind -eq 'file' -and (Test-ActionChanged $_.Action) -and $_.Action -ne 'remove' })) {
        $content = Get-ProfileFileContent -Profile $Profile -Dest $item.Path
        if ($null -eq $content) { throw "profile has no content for $($item.Path)" }
        if ($LogPath) { Write-Log ("writing " + $item.Path) -LogPath $LogPath }
        Write-TextFileAtomic -Path $item.Path -Content $content
        if ((Get-FileSha256Hex $item.Path) -ne $item.DesiredSha256) {
            throw "hash verification failed after writing $($item.Path)"
        }
    }

    foreach ($item in @($Plan.Items | Where-Object { $_.Kind -eq 'agent_conf' -and (Test-ActionChanged $_.Action) })) {
        $content = [string](Get-MapValue (Get-MapValue $Profile 'agent') 'config_text')
        if ($LogPath) { Write-Log ("writing " + $item.Path) -LogPath $LogPath }
        Write-TextFileAtomic -Path $item.Path -Content $content
        if ((Get-FileSha256Hex $item.Path) -ne $item.DesiredSha256) {
            throw "hash verification failed after writing $($item.Path)"
        }
    }

    if (-not $NoService) {
        $agent = Get-Agent2Info
        if (-not $agent.Exe) { throw 'Zabbix Agent 2 is not installed after the apply step' }
        # Test the config before touching the service. A bad directive then
        # reports itself instead of surfacing as a bare Start-Service failure.
        $configTest = Test-AgentConfig -Exe $agent.Exe -Conf $AgentConf
        if (-not $configTest.Ok) {
            throw ("agent config test failed: " + $configTest.Output)
        }
        $serviceItem = $Plan.Items | Where-Object { $_.Kind -eq 'service' } | Select-Object -First 1
        if ($serviceItem) {
            if ($LogPath) { Write-Log ("service action: " + $serviceItem.Action) -LogPath $LogPath }
            Set-AgentService -Action $serviceItem.Action -LogPath $LogPath
        }
        if (-not (Wait-TcpPort -Port $script:AgentPort -TimeoutSeconds 20)) {
            throw "nothing is listening on TCP $($script:AgentPort) after the restart"
        }
        if ($LogPath) { Write-Log "agent config test passed and TCP $($script:AgentPort) is listening" -LogPath $LogPath }
    }

    $verificationStatus = 'skipped'
    if (-not $NoServiceAccountTest) {
        if ($LogPath) { Write-Log 'verifying as the agent service account' -LogPath $LogPath }
        $verification = Invoke-ServiceAccountVerification -ScriptPath $ScriptPath `
            -ProfilePath $ProfilePath -MonitorRoot $MonitorRoot -AgentConf $AgentConf -LogPath $LogPath
        $summary.verification = $verification
        if ($verification.Ok -eq $true) {
            $verificationStatus = 'passed'
        } elseif ($verification.Status -eq 'skipped' -and $AllowUnverifiedServiceAccount) {
            $verificationStatus = 'skipped'
        } else {
            $message = [string]$verification.Message
            if (-not $message) { $message = 'the verification returned no detail' }
            throw ("service-account verification failed: " + $message)
        }
    }

    $changedCount = @($Plan.Items | Where-Object { Test-ActionChanged $_.Action }).Count
    $state = Write-InstallerState -Profile $Profile -ProfilePath $ProfilePath `
        -ProfileSha256 $ProfileSha256 -MonitorRoot $MonitorRoot -AgentConf $AgentConf `
        -AgentVersion $AgentVersion -Backup $backup `
        -ServiceAccountStatus $verificationStatus -ChangedCount $changedCount `
        -Managed $Plan.Managed
    $summary.state = $state
    return $summary
}

Export-ModuleMember -Function @(
    'Get-InstallerVersion', 'Get-ExitCode',
    'Get-MapValue', 'Get-ArrayValue',
    'ConvertTo-Hashtable', 'ConvertFrom-JsonCompat', 'ConvertTo-JsonCompat',
    'Get-BytesSha256Hex', 'Get-TextSha256Hex', 'Get-FileSha256Hex',
    'Get-NormalizedPath', 'Test-PathUnderRoot', 'Test-SafeAbsolutePath',
    'Get-DefaultLogPath', 'Initialize-Log', 'Write-Log',
    'Read-ProfileFile', 'Test-ProfileDocument',
    'Get-Agent2Info', 'Get-LegacyAgentInfo',
    'Read-InstallerState', 'Get-StatePath', 'Get-InstallPlan',
    'Write-TextFileAtomic', 'Copy-ProbeFile', 'Backup-PlanTargets',
    'Install-Agent2Msi', 'Stop-LegacyAgent', 'Set-AgentService',
    'Get-AgentTestValue', 'Test-AgentConfig', 'Test-TcpPort', 'Wait-TcpPort',
    'Get-ServiceAccountName', 'Get-VerificationFailureMessage',
    'Invoke-SelfVerification',
    'Invoke-ServiceAccountVerification', 'Write-InstallerState',
    'Invoke-InstallPlan', 'Test-ServerResolution', 'Test-IsAddressLiteral'
)
