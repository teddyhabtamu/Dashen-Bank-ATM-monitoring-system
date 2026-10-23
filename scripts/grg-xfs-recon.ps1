#Requires -Version 3.0
<#
.SYNOPSIS
  Dashen Bank ATM - read-only GRG/XFS reconnaissance.
.DESCRIPTION
  Run elevated on the GRG test ATM. Makes no changes. Finds the data path for
  hardware telemetry before we install or write a collector:

    1. NetXMS ATM agent: config, subagent modules, and whether its XFS metrics
       can be queried locally with nxget.
    2. XFS stack: msxfs.dll bitness/version, service-provider registry, and the
       ATM application's logical-service/cassette configuration.
    3. Existing Zabbix/SNMP listeners, so we know what is already installed.

  Output goes to Desktop\grg-xfs-recon.txt. Passwords/secrets are redacted.
#>

$ErrorActionPreference = 'SilentlyContinue'
$report = Join-Path ([Environment]::GetFolderPath('Desktop')) 'grg-xfs-recon.txt'
try { Stop-Transcript | Out-Null } catch {}
Start-Transcript -Path $report -Force | Out-Null

function Head($t) { Write-Output "`n===== $t =====" }
function Safe-Line($line) {
  if ($line -match '(?i)(pass(word)?|secret|token|api[_-]?key)\s*=') {
    return ($line -replace '=.*$', '= <redacted>')
  }
  return $line
}

Write-Output "GRG/XFS reconnaissance - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Output "Computer: $env:COMPUTERNAME   User: $(whoami)   PS: $($PSVersionTable.PSVersion)   64-bit: $([Environment]::Is64BitProcess)"

Head 'NETXMS - CONFIG'
$nxRoot = 'C:\NetXMS'
$nxConf = Join-Path $nxRoot 'etc\nxagentd.conf'
if (Test-Path $nxConf) {
  Write-Output "File: $nxConf"
  Get-Content $nxConf | ForEach-Object {
    if ($_ -match '^\s*#') { return }
    Safe-Line $_
  }
  $nxConfD = Join-Path $nxRoot 'etc\nxagentd.conf.d'
  if (Test-Path $nxConfD) {
    Write-Output "`nConfig fragments:"
    Get-ChildItem $nxConfD -File | Select-Object FullName
  }
} else {
  Write-Output "Not found: $nxConf"
}

Head 'NETXMS - FILES / SUBAGENTS'
if (Test-Path $nxRoot) {
  Get-ChildItem $nxRoot -Recurse -File |
    Where-Object { $_.Extension -in @('.nsm', '.dll') -or $_.Name -match 'nxget|nxcon|atm|xfs|grg' } |
    Select-Object FullName, Length, @{n='Version';e={$_.VersionInfo.FileVersion}} |
    Format-Table -AutoSize
} else {
  Write-Output "Not found: $nxRoot"
}

Head 'NETXMS - RUNNING MODULES'
Get-Process -Name nxagentd | ForEach-Object {
  Write-Output "PID $($_.Id)  $($_.Path)"
  $_.Modules | Where-Object { $_.FileName -match 'atm|xfs|grg|ncr' } |
    Select-Object ModuleName, FileName, @{n='Version';e={$_.FileVersionInfo.FileVersion}} |
    Format-Table -AutoSize
}

Head 'NETXMS - NXGET METRIC DISCOVERY'
$nxget = Get-ChildItem $nxRoot -Recurse -File -Filter 'nxget.exe' | Select-Object -First 1
if ($nxget) {
  Write-Output "nxget: $($nxget.FullName)"
  & $nxget.FullName -h 2>&1 | Select-Object -First 40
  foreach ($metric in @('Agent.SupportedMetrics', 'Agent.SupportedParameters', 'Agent.SupportedLists', 'Agent.SupportedTables')) {
    Write-Output "`n--- $metric ---"
    $out = & $nxget.FullName -e0 localhost $metric 2>&1
    if ($LASTEXITCODE -ne 0 -or ($out -join ' ') -match '(?i)unknown option|invalid option') {
      $out = & $nxget.FullName localhost $metric 2>&1
    }
    $out | Select-Object -First 120
  }
} else {
  Write-Output "nxget.exe not found under C:\NetXMS."
  Write-Output "Fallback: inspect ATM/XFS strings in the agent modules below."
}

Head 'NETXMS - ATM/XFS STRINGS IN AGENT MODULES'
$nxModules = Get-ChildItem $nxRoot -Recurse -File |
  Where-Object { $_.Extension -in @('.nsm', '.dll') -and ($_.Name -match 'atm|xfs|grg|ncr') -and $_.Length -lt 8MB }
foreach ($m in $nxModules) {
  Write-Output "`n### $($m.FullName)"
  $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($m.FullName))
  [regex]::Matches($text, '[ -~]{4,}') |
    ForEach-Object { $_.Value } |
    Where-Object { $_ -match '(?i)ATM|XFS|Cassette|CashUnit|Dispenser|CardReader|Printer|Sensor|EJ|Journal' } |
    Sort-Object -Unique |
    Select-Object -First 120
}

Head 'NETXMS - LOGS / DATA'
$nxLog = Get-ChildItem $nxRoot -Recurse -File |
  Where-Object { $_.Name -match '(?i)nxagentd.*\.log$|agent.*\.log$' -and $_.Length -lt 20MB } |
  Select-Object -First 3
foreach ($l in $nxLog) {
  Write-Output "`n### $($l.FullName) (tail)"
  Get-Content $l.FullName -Tail 120 | Where-Object { $_ -match '(?i)atm|xfs|grg|ncr|subagent|error|fail' }
}

Head 'SNMP / ZABBIX / LISTENERS'
Get-Service -Name 'SNMP', 'SNMPTRAP', 'Zabbix Agent', 'Zabbix Agent 2' |
  Select-Object Name, DisplayName, Status, StartType | Format-Table -AutoSize
Get-ChildItem 'C:\Program Files', 'C:\Program Files (x86)' -Recurse -File -Filter 'zabbix_agentd.exe' -Depth 3 |
  Select-Object FullName, @{n='Version';e={$_.VersionInfo.FileVersion}}
netstat -ano | Select-String ':4700\s|:10050\s|:10051\s|:161\s'

Head 'XFS - API DLLS'
foreach ($dll in @(
  'C:\Windows\SysWOW64\msxfs.dll',
  'C:\Windows\System32\msxfs.dll',
  'C:\Program Files (x86)\GrgBanking\GRGXFSSP5.0\GrgSp5.exe'
)) {
  if (Test-Path $dll) {
    Get-Item $dll | Select-Object FullName, Length, @{n='Version';e={$_.VersionInfo.FileVersion}}, @{n='Product';e={$_.VersionInfo.ProductName}}
  } else {
    Write-Output "Not found: $dll"
  }
}

Head 'XFS - SERVICE PROVIDERS (REGISTRY)'
foreach ($root in @(
  'HKLM:\SOFTWARE\XFS',
  'HKLM:\SOFTWARE\WOW6432Node\XFS',
  'HKLM:\SOFTWARE\WOW6432Node\XFS-'
)) {
  if (-not (Test-Path $root)) { continue }
  Write-Output "Root: $root"
  Get-ChildItem "$root\SERVICE_PROVIDERS" | ForEach-Object {
    $name = $_.PSChildName
    if ($name -notmatch '(?i)CDM|CIM|IDC|CRD|PIN|SIU|TTU|PTR') { return }
    Write-Output "`n### $name"
    $p = Get-ItemProperty $_.PSPath
    $p.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object {
      Write-Output "  $($_.Name) = $($_.Value)"
    }
  }
  if (Test-Path "$root\LOGICAL_SERVICES") {
    Write-Output "`n[LOGICAL_SERVICES]"
    Get-ChildItem "$root\LOGICAL_SERVICES" | ForEach-Object {
      Write-Output "`n### $($_.PSChildName)"
      $p = Get-ItemProperty $_.PSPath
      $p.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object {
        Write-Output "  $($_.Name) = $($_.Value)"
      }
    }
  }
}

Head 'XFS - GRG REGISTRY KEYS (NAMES ONLY)'
foreach ($key in @(
  'HKLM:\SOFTWARE\WOW6432Node\GrgBanking',
  'HKLM:\SOFTWARE\WOW6432Node\GrgBanking-',
  'HKLM:\SOFTWARE\WOW6432Node\GRGFD',
  'HKLM:\SOFTWARE\WOW6432Node\GRGAV'
)) {
  if (-not (Test-Path $key)) { continue }
  Write-Output "`n### $key"
  Get-ChildItem $key -Recurse -Depth 2 | Select-Object -ExpandProperty Name
}

Head 'XFS - ATM APPLICATION FILES / CONFIG'
$appDirs = @(
  'D:\Program Files\DTATMW',
  'C:\GrgBanking',
  'C:\Program Files (x86)\GrgBanking'
)
$configExt = @('.ini', '.xml', '.cfg', '.conf', '.json', '.properties', '.dat')
foreach ($dir in $appDirs) {
  if (-not (Test-Path $dir)) { continue }
  Write-Output "`n### $dir"
  Get-ChildItem $dir -Directory -Recurse -Depth 2 | Select-Object -ExpandProperty FullName
  $files = Get-ChildItem $dir -Recurse -File -Depth 4 |
    Where-Object { $configExt -contains $_.Extension.ToLower() -and $_.Length -lt 2MB }
  $files | Select-Object FullName, Length | Format-Table -AutoSize
  foreach ($f in $files) {
    $hits = Select-String -Path $f.FullName -Pattern 'XFS|CDM|CIM|IDC|CRD|PIN|SIU|TTU|CashUnit|Cassette|LogicalService|ServiceProvider|Dispenser' |
      Select-Object -First 20
    if ($hits) {
      Write-Output "`n--- $($f.FullName) ---"
      $hits | ForEach-Object { Safe-Line $_.Line.Trim() }
    }
  }
}

Head 'DONE'
Write-Output "Report: $report"
try { Stop-Transcript | Out-Null } catch {}
