#Requires -Version 5.1
<#
.SYNOPSIS
  Build AtmMonitorSetup.exe from the committed manifest and a pinned Zabbix
  Agent 2 MSI.

.DESCRIPTION
  The build refuses to package a probe whose SHA-256 differs from
  report-portal/agent_manifest.json. That is the same manifest the portal
  renders into every profile, so a release cannot silently ship a probe the
  portal did not describe.

  The agent MSI is downloaded from the official Zabbix CDN and checked against
  a pinned SHA-256. A new agent version requires deliberately adding its hash
  to $PinnedAgentMsi (or -AllowUnpinnedAgent for a lab build).

  Signing is Authenticode. Pass -SignPfx/-SignPassword, or use the CI secrets.
  A production release must be signed (-RequireSignature makes its absence a
  build failure).

.EXAMPLE
  .\build_agent_setup.ps1 -Version 0.1.0 -RequireSignature `
      -SignPfx C:\certs\dashen.pfx -SignPassword $env:PFX_PASSWORD
#>
[CmdletBinding()]
param(
    [string]$Version = '0.0.0-dev',
    [string]$AgentVersion = '6.4.21',
    [string]$AgentMsi = '',
    [string]$InnoCompiler = '',
    [string]$SignPfx = '',
    [string]$SignPassword = '',
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [switch]$RequireSignature,
    [switch]$SkipAgentDownload,
    [switch]$AllowUnpinnedAgent,
    [switch]$KeepStaging
)

$ErrorActionPreference = 'Stop'

# Pinned official packages. Add a new line only with the hash printed by
# `Get-FileHash -Algorithm SHA256` on the downloaded MSI.
$PinnedAgentMsi = @{
    '6.4.21' = '853a8ed21fd3cb14b4ffacda61e3c48165179025bf90c61b40b61d847a16bbd7'
}

$installerDir = $PSScriptRoot
if (-not $installerDir) { $installerDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $installerDir
$staging = Join-Path $installerDir 'staging\payload'
$dist = Join-Path $installerDir 'dist'
$cache = Join-Path $installerDir '.cache'
$manifestPath = Join-Path $repo 'report-portal\agent_manifest.json'

function Write-Step([string]$Message) {
    Write-Host ('==> ' + $Message)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Find-InnoCompiler {
    if ($InnoCompiler) { return $InnoCompiler }
    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'ISCC.exe not found; install Inno Setup 6 or pass -InnoCompiler'
}

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $kits = $null
    if (${env:ProgramFiles(x86)}) {
        $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    }
    if ($kits -and (Test-Path -LiteralPath $kits)) {
        $found = Get-ChildItem -LiteralPath $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw 'signtool.exe not found; install the Windows SDK or pass a certificate via the CI secret'
}

# ── Manifest and probes ────────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "agent manifest not found: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1) { throw "unsupported manifest schema: $($manifest.schema)" }

Write-Step 'Verifying the probe bundle against the manifest'
if (-not $KeepStaging) {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
New-Item -ItemType Directory -Path (Join-Path $staging 'probes') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $staging 'agent') -Force | Out-Null

$probeNames = @()
foreach ($property in $manifest.files.PSObject.Properties) {
    $name = $property.Name
    $entry = $property.Value
    $source = Join-Path $repo ($entry.source -replace '/', '\')
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "manifest names a missing probe: $source"
    }
    $actual = Get-Sha256 $source
    if ($actual -ne $entry.sha256) {
        throw "probe $name is stale: manifest $($entry.sha256), file $actual"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $staging ('probes\' + $name)) -Force
    $probeNames += $name
    Write-Host ('    {0}  {1}' -f $actual.Substring(0, 12), $name)
}

if ($probeNames.Count -eq 0) { throw 'the manifest contains no deployable probes' }

foreach ($name in @('AtmInstall.psm1', 'atm-install.ps1')) {
    $source = Join-Path $installerDir $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "installer file missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $staging $name) -Force
}

# ── Agent MSI ──────────────────────────────────────────────────────────────
Write-Step "Preparing Zabbix Agent 2 $AgentVersion"
if (-not (Test-Path -LiteralPath $cache)) {
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
}
$msiName = "zabbix_agent2-$AgentVersion-windows-amd64-openssl.msi"
$cachedMsi = Join-Path $cache $msiName
if ($AgentMsi) {
    if (-not (Test-Path -LiteralPath $AgentMsi -PathType Leaf)) {
        throw "agent MSI not found: $AgentMsi"
    }
    Copy-Item -LiteralPath $AgentMsi -Destination $cachedMsi -Force
} elseif (-not (Test-Path -LiteralPath $cachedMsi -PathType Leaf)) {
    if ($SkipAgentDownload) {
        throw "agent MSI is not cached and -SkipAgentDownload was set: $cachedMsi"
    }
    $url = "https://cdn.zabbix.com/zabbix/binaries/stable/6.4/$AgentVersion/$msiName"
    Write-Host "    downloading $url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $cachedMsi -UseBasicParsing
}

$msiHash = Get-Sha256 $cachedMsi
if ($PinnedAgentMsi.ContainsKey($AgentVersion)) {
    if ($msiHash -ne $PinnedAgentMsi[$AgentVersion]) {
        throw "agent MSI hash mismatch: pinned $($PinnedAgentMsi[$AgentVersion]), got $msiHash"
    }
} elseif (-not $AllowUnpinnedAgent) {
    throw "agent version $AgentVersion has no pinned SHA-256; add it to `$PinnedAgentMsi or pass -AllowUnpinnedAgent"
}
Copy-Item -LiteralPath $cachedMsi -Destination (Join-Path $staging ('agent\' + $msiName)) -Force
Write-Host ('    {0}  {1}' -f $msiHash.Substring(0, 12), $msiName)

# ── Version stamp ──────────────────────────────────────────────────────────
$commit = 'unknown'
try {
    $commit = (& git -C $repo rev-parse --short HEAD 2>$null)
    if (-not $commit) { $commit = 'unknown' }
} catch {}
$builtAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$versionText = @(
    "installer_version=$Version",
    "agent_version=$AgentVersion",
    "agent_msi_sha256=$msiHash",
    "git_commit=$commit",
    "built_at=$builtAt"
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $staging 'VERSION.txt'), $versionText + "`r`n",
    (New-Object System.Text.UTF8Encoding($false)))

# ── Inno compile ───────────────────────────────────────────────────────────
Write-Step 'Compiling AtmMonitorSetup.exe'
$iscc = Find-InnoCompiler
if (-not (Test-Path -LiteralPath $dist)) { New-Item -ItemType Directory -Path $dist -Force | Out-Null }
$spec = Join-Path $installerDir 'AtmMonitorSetup.iss'
$env:ATM_SETUP_VERSION = $Version
$env:ATM_SETUP_PAYLOAD = $staging
& $iscc $spec
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$exe = Join-Path $dist ("AtmMonitorSetup-$Version.exe")
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "expected installer was not produced: $exe"
}

# ── Authenticode ───────────────────────────────────────────────────────────
$signed = $false
if ($SignPfx) {
    Write-Step 'Signing AtmMonitorSetup.exe'
    $signtool = Find-SignTool
    & $signtool sign /fd SHA256 /f $SignPfx /p $SignPassword /tr $TimestampUrl /td SHA256 $exe
    if ($LASTEXITCODE -ne 0) { throw "signtool sign failed with exit code $LASTEXITCODE" }
    & $signtool verify /pa /v $exe
    if ($LASTEXITCODE -ne 0) { throw "signtool verify failed with exit code $LASTEXITCODE" }
    $signed = $true
} elseif ($RequireSignature) {
    throw '-RequireSignature was set but no -SignPfx was supplied'
} else {
    Write-Warning 'Building an UNSIGNED installer. Do not call this a production release.'
}

$exeHash = Get-Sha256 $exe
$hashFile = Join-Path $dist ("AtmMonitorSetup-$Version.sha256")
[System.IO.File]::WriteAllText($hashFile, "$exeHash  AtmMonitorSetup-$Version.exe`r`n",
    (New-Object System.Text.UTF8Encoding($false)))

$release = [ordered]@{
    version           = $Version
    agent_version     = $AgentVersion
    agent_msi_sha256  = $msiHash
    profile_schema    = 1
    git_commit        = $commit
    built_at          = $builtAt
    exe               = "AtmMonitorSetup-$Version.exe"
    exe_sha256        = $exeHash
    signed            = $signed
    probes            = @($probeNames)
}
$releaseJson = $release | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText((Join-Path $dist 'release.json'), $releaseJson + "`r`n",
    (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
Write-Host "Built:  $exe"
Write-Host "SHA256: $exeHash"
Write-Host "Signed: $signed"
Write-Host "Probes: $($probeNames.Count)"
