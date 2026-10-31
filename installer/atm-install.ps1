#Requires -Version 2.0
<#
.SYNOPSIS
  Apply one ATM monitoring profile to this box.

.DESCRIPTION
  This is the payload inside AtmMonitorSetup.exe. It is also runnable by hand
  for a pilot:

    powershell -NoProfile -ExecutionPolicy Bypass -File atm-install.ps1 `
        -Profile .\ATM-0107.profile.json -DryRun

  It never registers an ATM with Zabbix and never writes identity back to the
  registry. The Zabbix host is created separately; this script only makes the
  box match the profile the portal rendered for that host.

  Exit codes are the contract used by the Inno wrapper and CI. See
  installer/README.md for the table.
#>
[CmdletBinding()]
param(
    [string]$Profile,
    [string]$MonitorRoot = 'C:\monitor',
    [string]$ProbeRoot,
    [string]$AgentHome,
    [string]$AgentConf,
    [string]$AgentMsi,
    [string]$AgentVersion = '',
    [string]$ExpectedProfileSha256,
    [string]$AcceptAtmId,
    [string]$LogPath,
    [string]$Out,
    [switch]$DryRun,
    [switch]$AllowNotReady,
    [switch]$AllowUnsignedProfile,
    [switch]$NoAgentInstall,
    [switch]$NoService,
    [switch]$NoServiceAccountTest,
    [switch]$AllowUnverifiedServiceAccount,
    [switch]$ReplaceLegacyAgent,
    [switch]$Reassign,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'

$scriptPath = $MyInvocation.MyCommand.Path
if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Definition }
$scriptDir = Split-Path -Parent $scriptPath
if (-not $scriptDir) { $scriptDir = (Get-Location).Path }

Import-Module (Join-Path $scriptDir 'AtmInstall.psm1') -Force

function Read-VersionFile {
    param([string]$Directory)
    $values = @{}
    $path = Join-Path $Directory 'VERSION.txt'
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        foreach ($line in @(Get-Content -LiteralPath $path -ErrorAction SilentlyContinue)) {
            if ($line -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$') {
                $values[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $values
}

function Find-Profile {
    param([string]$Directory, [string]$Explicit)
    if ($Explicit) { return $Explicit }
    $roots = @($Directory, (Split-Path -Parent $Directory), (Get-Location).Path)
    $found = @()
    foreach ($root in $roots) {
        if (-not $root) { continue }
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $found += @(Get-ChildItem -LiteralPath $root -Filter '*.profile.json' -ErrorAction SilentlyContinue |
            Where-Object { -not $_.PSIsContainer })
    }
    $found = @($found | Sort-Object FullName -Unique)
    if ($found.Count -eq 1) { return $found[0].FullName }
    if ($found.Count -eq 0) {
        throw 'no profile found; place <atm_id>.profile.json next to the installer or pass -Profile'
    }
    throw ('multiple profiles found: ' + (($found | ForEach-Object { $_.Name }) -join ', '))
}

function Get-ExitCodeForError {
    param([string]$Message)
    if ($Message -match 'legacy Zabbix Agent 1') { return (Get-ExitCode LegacyAgentPresent) }
    if ($Message -match 'bundled probe|does not match the profile') { return (Get-ExitCode ProbeMismatch) }
    if ($Message -match 'msiexec|agent MSI|Zabbix Agent 2 is not installed|refusing to downgrade') { return (Get-ExitCode AgentInstallFailed) }
    if ($Message -match 'service-account verification') { return (Get-ExitCode VerifyFailed) }
    if ($Message -match 'agent config test|listening on TCP') { return (Get-ExitCode ConfigTestFailed) }
    if ($Message -match 'sc config|service does not exist|schtasks|service action') { return (Get-ExitCode ServiceFailed) }
    if ($Message -match 'preflight failed|not a safe|outside|unsigned|not ready|schema|hostname|vendor|ServerActive|Include=|Hostname must|ListenPort|Timeout=|profile SHA-256|already managed') {
        return (Get-ExitCode InvalidProfile)
    }
    return (Get-ExitCode WriteFailed)
}

function Write-Result {
    param($Object)
    [System.Console]::WriteLine('RESULT ' + (ConvertTo-JsonCompat $Object))
}

function Show-Plan {
    param($Plan)
    foreach ($item in $Plan.Items) {
        $target = $item.Path
        if (-not $target) { $target = '-' }
        $reason = $item.Reason
        if ($reason) { $reason = '  # ' + $reason }
        Write-Host ('PLAN {0,-9} {1,-8} {2}{3}' -f $item.Kind, $item.Action, $target, $reason)
    }
}

# ── Resolve the payload's own files ────────────────────────────────────────
$versions = Read-VersionFile $scriptDir
if (-not $AgentVersion) {
    if ($versions.ContainsKey('agent_version')) { $AgentVersion = $versions['agent_version'] }
    else { $AgentVersion = '6.4' }
}
if (-not $ProbeRoot) { $ProbeRoot = Join-Path $scriptDir 'probes' }
if (-not $AgentMsi) {
    $agentDir = Join-Path $scriptDir 'agent'
    $msis = @(Get-ChildItem -LiteralPath $agentDir -Filter '*.msi' -ErrorAction SilentlyContinue)
    if ($msis.Count -eq 1) { $AgentMsi = $msis[0].FullName }
}
if (-not $LogPath) { $LogPath = Get-DefaultLogPath }
$LogPath = Initialize-Log $LogPath

if ($AgentMsi -and $versions.ContainsKey('agent_msi_sha256')) {
    $msiActual = Get-FileSha256Hex $AgentMsi
    if ($msiActual -ne ([string]$versions['agent_msi_sha256']).ToLowerInvariant()) {
        $message = "bundled agent MSI does not match VERSION.txt (expected $($versions['agent_msi_sha256']), got $msiActual)"
        Write-Log $message 'ERROR' $LogPath
        Write-Result @{ ok = $false; error = $message; atm_id = $null }
        exit (Get-ExitCode AgentInstallFailed)
    }
}

try {
    if (-not $Profile) { $Profile = Find-Profile -Directory $scriptDir -Explicit '' }
    $profileInfo = Read-ProfileFile -Path $Profile -ExpectedSha256 $ExpectedProfileSha256
    $document = $profileInfo.Document
} catch {
    $message = $_.Exception.Message
    $code = Get-ExitCode InvalidProfile
    if ($message -match 'SHA-256 mismatch') { $code = Get-ExitCode ProfileHashMismatch }
    Write-Log $message 'ERROR' $LogPath
    Write-Result @{ ok = $false; error = $message; atm_id = $null }
    exit $code
}

$atmId = [string](Get-MapValue $document 'atm_id')
$vendor = [string](Get-MapValue $document 'vendor')
$agentBlock = Get-MapValue $document 'agent'
$server = [string](Get-MapValue $agentBlock 'server')

if ($AcceptAtmId -and $AcceptAtmId -ne $atmId) {
    $message = "profile is for '$atmId', not the accepted ATM '$AcceptAtmId'"
    Write-Log $message 'ERROR' $LogPath
    Write-Result @{ ok = $false; error = $message; atm_id = $atmId }
    exit (Get-ExitCode AtmIdMismatch)
}

if ($VerifyOnly) {
    $result = Invoke-SelfVerification -Profile $document -MonitorRoot $MonitorRoot `
        -AgentConf $AgentConf -LogPath $LogPath
    if ($Out) {
        $outDir = Split-Path -Parent $Out
        if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
            New-Item -ItemType Directory -Path $outDir -Force | Out-Null
        }
        [System.IO.File]::WriteAllText($Out, (ConvertTo-JsonCompat $result),
            (New-Object System.Text.UTF8Encoding($false)))
    } else {
        Write-Output (ConvertTo-JsonCompat $result)
    }
    if ($result.ok -eq $true) { exit (Get-ExitCode Ok) }
    exit (Get-ExitCode VerifyFailed)
}

Write-Log "profile: $($profileInfo.Path) sha256=$($profileInfo.Sha256)" -LogPath $LogPath
Write-Log "ATM: $atmId  vendor: $vendor  agent server: $server" -LogPath $LogPath

$validation = Test-ProfileDocument -Profile $document -MonitorRoot $MonitorRoot `
    -AllowNotReady:$AllowNotReady -AllowUnsignedProfile:$AllowUnsignedProfile
foreach ($warning in $validation.Warnings) { Write-Log $warning 'WARN' $LogPath }
if (-not $validation.Ok) {
    foreach ($message in $validation.Errors) { Write-Log $message 'ERROR' $LogPath }
    $code = Get-ExitCode InvalidProfile
    if (@($validation.Errors | Where-Object { $_ -match 'not ready' }).Count -gt 0) {
        $code = Get-ExitCode NotReady
    } elseif (@($validation.Errors | Where-Object { $_ -match 'unsigned' }).Count -gt 0) {
        $code = Get-ExitCode UnsignedProfile
    }
    Write-Result @{
        ok       = $false
        atm_id   = $atmId
        error    = ($validation.Errors -join '; ')
        warnings = @($validation.Warnings)
    }
    exit $code
}

if (-not $AgentHome) {
    $agentInfo = Get-Agent2Info
    if ($agentInfo.Home) { $AgentHome = $agentInfo.Home }
    elseif ($env:ProgramFiles) { $AgentHome = Join-Path $env:ProgramFiles 'Zabbix Agent 2' }
    else { $AgentHome = 'C:\Program Files\Zabbix Agent 2' }
}
if (-not $AgentConf) { $AgentConf = Join-Path $AgentHome 'zabbix_agent2.conf' }

$plan = Get-InstallPlan -Profile $document -MonitorRoot $MonitorRoot -AgentConf $AgentConf `
    -ProbeRoot $ProbeRoot -AgentMsi $AgentMsi -AgentVersion $AgentVersion `
    -NoAgentInstall:$NoAgentInstall -NoService:$NoService `
    -ReplaceLegacyAgent:$ReplaceLegacyAgent -Reassign:$Reassign

foreach ($warning in $plan.Warnings) { Write-Log $warning 'WARN' $LogPath }
Show-Plan $plan

if ($plan.Errors.Count -gt 0) {
    foreach ($message in $plan.Errors) { Write-Log $message 'ERROR' $LogPath }
    $first = [string]$plan.Errors[0]
    Write-Result @{
        ok       = $false
        atm_id   = $atmId
        error    = ($plan.Errors -join '; ')
        warnings = @($plan.Warnings)
    }
    exit (Get-ExitCodeForError $first)
}

if ($DryRun) {
    $summary = @{
        ok                = $true
        dry_run           = $true
        atm_id            = $atmId
        profile_hash      = [string](Get-MapValue $document 'profile_hash')
        profile_sha256    = $profileInfo.Sha256
        changed           = [bool]$plan.Changed
        actions           = @($plan.Items | ForEach-Object {
            @{ kind = $_.Kind; action = $_.Action; path = $_.Path; reason = $_.Reason }
        })
        warnings          = @($plan.Warnings)
        installer_version = Get-InstallerVersion
    }
    Write-Log ('dry run: {0} action(s), changed={1}' -f $summary.actions.Count, $summary.changed) -LogPath $LogPath
    Write-Result $summary
    exit (Get-ExitCode Ok)
}

try {
    $summary = Invoke-InstallPlan -Plan $plan -Profile $document -ScriptPath $scriptPath `
        -ProfilePath $profileInfo.Path -ProfileSha256 $profileInfo.Sha256 `
        -MonitorRoot $MonitorRoot -AgentConf $AgentConf -AgentVersion $AgentVersion `
        -LogPath $LogPath -NoService:$NoService -NoServiceAccountTest:$NoServiceAccountTest `
        -AllowUnverifiedServiceAccount:$AllowUnverifiedServiceAccount
    Write-Log 'install completed' -LogPath $LogPath
    Write-Result $summary
    exit (Get-ExitCode Ok)
} catch {
    $message = $_.Exception.Message
    Write-Log $message 'ERROR' $LogPath
    Write-Result @{
        ok     = $false
        atm_id = $atmId
        error  = $message
    }
    exit (Get-ExitCodeForError $message)
}
