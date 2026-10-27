#Requires -Version 3.0
<#
.SYNOPSIS
  Read-only image chunk reader for the Zabbix agent.

.DESCRIPTION
  Returns base64 chunks of an allow-listed image file, metadata for one file,
  or a page of entries from an allow-listed zip.  It never writes, extracts,
  deletes, renames or executes anything.

  This is a transport primitive for the on-demand image viewer: image bytes
  stay on the ATM and are read only when an authorised operator opens them.
  The portal never talks to this script directly; a host-side bridge does.

  File mode:
    atm-image-chunk.ps1 -Path 'E:\pic\2026\10\22\CAM[1]_...jpg' -Offset 0 -Length 24576
    atm-image-chunk.ps1 -PathB64 '<base64url>' -Info
    atm-image-chunk.ps1 -PathB64 '<base64url>' -Offset 0 -Length 24576

  Directory mode (non-recursive; used to discover evidence on the ATM):
    atm-image-chunk.ps1 -DirPathB64 '<base64url>' -DirList -DirOffset 0 -DirLimit 25

  Zip modes (entries are selected by their archive index, never by name):
    atm-image-chunk.ps1 -ZipPathB64 '<base64url>' -List -ListOffset 0 -ListLimit 25
    atm-image-chunk.ps1 -ZipPathB64 '<base64url>' -EntryIndex 0 -Offset 0 -Length 24576

  Output is one line: base64 data, or single-line JSON for -Info / -List /
  -DirList.  On refusal the script prints ERR:<code> and exits 1.  Codes are
  deliberately generic and never contain a path:
    E_ARGS, E_PATH, E_MISSING, E_ROOT, E_EXT, E_REPARSE, E_RANGE, E_SIZE,
    E_IO, E_ZIP, E_ZIP_BUSY, E_ZIP_COUNT, E_ENTRY, E_COUNT, E_CONFIG

  Allow-list:
    C:\monitor\atm-image-roots.txt, one absolute root per line, # comments.
    The file is REQUIRED; if it is missing or contains no roots the script
    fails closed with E_CONFIG.

  Security:
    * restrict TCP/10050 to the monitoring host/bridge;
    * keep the roots as narrow as possible;
    * do not run this through a general-purpose Zabbix item;
    * use -Trace only for local diagnosis, never in a UserParameter.
#>
param(
    [string]$Path,
    [string]$PathB64,

    [string]$ZipPath,
    [string]$ZipPathB64,
    [long]$EntryIndex = -1,

    [string]$DirPath,
    [string]$DirPathB64,
    [long]$DirOffset = 0,
    [int]$DirLimit = 25,

    [long]$Offset = 0,
    [int]$Length = 24576,

    [long]$ListOffset = 0,
    [int]$ListLimit = 25,

    [switch]$DirList,
    [switch]$List,
    [switch]$Info,
    [switch]$Trace
)

$ErrorActionPreference = 'Stop'

$Script:MaxChunk = 32768
$Script:MaxFileBytes = 33554432          # 32 MiB for a still image
$Script:MaxVideoBytes = 8388608          # 8 MiB for a short MP4
$Script:MaxZipEntryBytes = 8388608       # 8 MiB per zip entry
$Script:MaxZipBytes = 268435456          # 256 MiB per image zip
$Script:MaxZipEntries = 5000
$Script:MaxDirEntries = 5000
$Script:MaxZipNameLength = 256
$Script:MaxListLimit = 100
$Script:AllowedExtensions = @('.jpg', '.jpeg', '.png', '.mp4', '.zip')
$Script:EntryExtensions = @('.jpg', '.jpeg', '.png', '.mp4')

function Fail([string]$Code) {
    throw $Code
}

function ConvertFrom-Base64Url([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { Fail 'E_ARGS' }
    $text = $Value.Replace('-', '+').Replace('_', '/')
    switch ($text.Length % 4) {
        2 { $text += '==' }
        3 { $text += '=' }
        1 { Fail 'E_ARGS' }
    }
    try {
        return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($text))
    } catch {
        Fail 'E_ARGS'
    }
}

function Get-AllowedRoots {
    $config = Join-Path $PSScriptRoot 'atm-image-roots.txt'
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
        Fail 'E_CONFIG'
    }

    $roots = @()
    foreach ($line in Get-Content -LiteralPath $config -ErrorAction Stop) {
        $value = $line.Trim()
        if (-not $value) { continue }
        if ($value.StartsWith('#')) { continue }
        if ($value -match '[\x00-\x1F\x7F]') { Fail 'E_CONFIG' }

        if (-not [IO.Path]::IsPathRooted($value)) { Fail 'E_CONFIG' }
        if ($value -match '^[A-Za-z]:([^\\/]|$)') { Fail 'E_CONFIG' }
        if ($value.StartsWith('\') -and -not $value.StartsWith('\\')) {
            Fail 'E_CONFIG'
        }
        if ($value.StartsWith('\\')) { Fail 'E_CONFIG' }
        if ($value -match '(^|[\\/])\.\.([\\/]|$)') { Fail 'E_CONFIG' }

        try {
            $full = [IO.Path]::GetFullPath($value)
        } catch {
            Fail 'E_CONFIG'
        }
        if ($full.StartsWith('\\')) { Fail 'E_CONFIG' }
        if ($full -match '^[A-Za-z]:\\$') { Fail 'E_CONFIG' }
        if ($full.IndexOf([char]':', 2) -ge 0) { Fail 'E_CONFIG' }
        if ($full -match '[\*\?]') { Fail 'E_CONFIG' }

        $full = $full -replace '[\\/]+$', ''
        if ($roots -notcontains $full) { $roots += $full }
    }

    if ($roots.Count -eq 0) { Fail 'E_CONFIG' }
    return $roots
}

function Get-ItemOrFail([string]$Value, [string]$Code) {
    try {
        return Get-Item -LiteralPath $Value -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        Fail 'E_MISSING'
    } catch {
        Fail $Code
    }
}

function Test-NoReparsePoint([string]$Root, [string]$FullPath) {
    $rootPath = $Root -replace '[\\/]+$', ''
    $rootItem = Get-ItemOrFail $rootPath 'E_ROOT'
    if (-not $rootItem.PSIsContainer) { Fail 'E_ROOT' }
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Fail 'E_REPARSE'
    }

    $fullPath = $FullPath -replace '[\\/]+$', ''
    if ($fullPath.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if (-not $fullPath.StartsWith(
            $rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        Fail 'E_ROOT'
    }

    $relative = $fullPath.Substring($rootPath.Length)
    $current = $rootPath
    foreach ($part in ($relative -split '[\\/]')) {
        if (-not $part) { continue }
        $current = Join-Path $current $part
        $item = Get-ItemOrFail $current 'E_PATH'
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Fail 'E_REPARSE'
        }
    }
}

function Resolve-AllowedPath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { Fail 'E_PATH' }
    if ($Value -match '[\x00-\x1F\x7F]') { Fail 'E_PATH' }
    if ($Value.StartsWith('\') -and -not $Value.StartsWith('\\')) {
        Fail 'E_ROOT'
    }
    if ($Value.StartsWith('\\')) { Fail 'E_ROOT' }
    if (-not [IO.Path]::IsPathRooted($Value)) { Fail 'E_PATH' }
    if ($Value -match '^[A-Za-z]:([^\\/]|$)') { Fail 'E_PATH' }

    try {
        $full = [IO.Path]::GetFullPath($Value)
    } catch {
        Fail 'E_PATH'
    }
    if ($full.StartsWith('\\')) { Fail 'E_ROOT' }
    if ($full.IndexOf([char]':', 2) -ge 0) { Fail 'E_PATH' }   # alternate data streams

    $extension = [IO.Path]::GetExtension($full).ToLowerInvariant()
    if ($Script:AllowedExtensions -notcontains $extension) { Fail 'E_EXT' }

    $stem = [IO.Path]::GetFileNameWithoutExtension($full)
    $first = $stem.Split('.')[0].TrimEnd([char[]]@(' ', '.'))
    if ($first -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
        Fail 'E_PATH'
    }

    $matched = $null
    foreach ($root in (Get-AllowedRoots)) {
        $candidate = $root
        if (-not $candidate.EndsWith('\')) { $candidate += '\' }
        if ($full.StartsWith($candidate, [StringComparison]::OrdinalIgnoreCase)) {
            $matched = $candidate
            break
        }
    }
    if (-not $matched) { Fail 'E_ROOT' }
    if ($full.Equals($matched, [StringComparison]::OrdinalIgnoreCase)) {
        Fail 'E_PATH'
    }

    Test-NoReparsePoint $matched $full
    return $full
}

function Resolve-AllowedDirectory([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { Fail 'E_PATH' }
    if ($Value -match '[\x00-\x1F\x7F]') { Fail 'E_PATH' }
    if ($Value.StartsWith('\') -and -not $Value.StartsWith('\\')) {
        Fail 'E_ROOT'
    }
    if ($Value.StartsWith('\\')) { Fail 'E_ROOT' }
    if (-not [IO.Path]::IsPathRooted($Value)) { Fail 'E_PATH' }
    if ($Value -match '^[A-Za-z]:([^\\/]|$)') { Fail 'E_PATH' }

    try {
        $full = [IO.Path]::GetFullPath($Value)
    } catch {
        Fail 'E_PATH'
    }
    if ($full.StartsWith('\\')) { Fail 'E_ROOT' }
    if ($full.IndexOf([char]':', 2) -ge 0) { Fail 'E_PATH' }

    $full = $full -replace '[\\/]+$', ''
    if ($full -match '^[A-Za-z]:$') { Fail 'E_ROOT' }

    $matched = $null
    foreach ($root in (Get-AllowedRoots)) {
        $rootPath = $root -replace '[\\/]+$', ''
        if ($full.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
            $matched = $rootPath
            break
        }
        $candidate = $rootPath + '\'
        if ($full.StartsWith($candidate, [StringComparison]::OrdinalIgnoreCase)) {
            $matched = $rootPath
            break
        }
    }
    if (-not $matched) { Fail 'E_ROOT' }

    Test-NoReparsePoint $matched $full

    $item = Get-ItemOrFail $full 'E_PATH'
    if (-not $item.PSIsContainer) { Fail 'E_PATH' }
    return $full
}

function Get-DirectoryEntries([string]$FullPath, [long]$PageOffset,
                              [int]$PageLimit) {
    if ($PageOffset -lt 0 -or $PageLimit -le 0 -or
        $PageLimit -gt $Script:MaxListLimit) {
        Fail 'E_RANGE'
    }

    $dirItem = Get-ItemOrFail $FullPath 'E_PATH'
    if (-not $dirItem.PSIsContainer) { Fail 'E_PATH' }

    try {
        $children = @(Get-ChildItem -LiteralPath $FullPath -Force -ErrorAction Stop)
    } catch {
        Fail 'E_IO'
    }

    $files = New-Object 'System.Collections.Generic.List[object]'
    foreach ($child in $children) {
        if ($child.PSIsContainer) { continue }
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            continue
        }
        if ($child.Name.Length -gt $Script:MaxZipNameLength) { continue }
        $extension = [IO.Path]::GetExtension($child.Name).ToLowerInvariant()
        if ($Script:AllowedExtensions -notcontains $extension) { continue }
        $files.Add($child)
    }

    if ($files.Count -gt $Script:MaxDirEntries) { Fail 'E_COUNT' }

    $sorted = @($files.ToArray() | Sort-Object -Property Name)
    $total = $sorted.Count
    $start = [int][Math]::Min($PageOffset, [long]$total)
    $end = [int][Math]::Min(
        ([long]$start + [long]$PageLimit), [long]$total)

    $entries = New-Object 'System.Collections.Generic.List[object]'
    for ($i = $start; $i -lt $end; $i++) {
        $file = $sorted[$i]
        $entries.Add([pscustomobject]@{
            index           = $i
            name            = $file.Name
            length          = [long]$file.Length
            last_write_time = $file.LastWriteTime.ToString('o')
        })
    }

    $next = $null
    if ($end -lt $total) { $next = $end }

    $payload = [pscustomobject]@{
        total   = $total
        offset  = $start
        next    = $next
        entries = @($entries.ToArray())
    }
    return (ConvertTo-Json -InputObject $payload -Compress -Depth 5)
}

function Get-MaxBytes([string]$FullPath) {
    if ([IO.Path]::GetExtension($FullPath).ToLowerInvariant() -eq '.mp4') {
        return [long]$Script:MaxVideoBytes
    }
    return [long]$Script:MaxFileBytes
}

function Get-FileLength([string]$FullPath, [long]$MaxBytes) {
    $item = Get-ItemOrFail $FullPath 'E_PATH'
    if ($item.PSIsContainer) { Fail 'E_PATH' }
    if ($item.Length -le 0 -or $item.Length -gt $MaxBytes) { Fail 'E_SIZE' }
    return [long]$item.Length
}

function Get-FileInfo([string]$FullPath) {
    $maxBytes = Get-MaxBytes $FullPath
    $size = Get-FileLength $FullPath $maxBytes

    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $FullPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        $before = [long]$stream.Length
        if ($before -ne $size) { Fail 'E_IO' }
        [byte[]]$hash = $sha.ComputeHash($stream)
        $after = [long]$stream.Length
        if ($after -ne $before) { Fail 'E_IO' }
    } finally {
        if ($stream) { $stream.Dispose() }
        $sha.Dispose()
    }

    $hex = ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    return (ConvertTo-Json -InputObject ([pscustomobject]@{
        size   = [long]$size
        sha256 = $hex
    }) -Compress)
}

function Read-FileChunk([string]$FullPath, [long]$ChunkOffset, [int]$ChunkLength) {
    if ($ChunkOffset -lt 0 -or $ChunkLength -le 0 -or
        $ChunkLength -gt $Script:MaxChunk) {
        Fail 'E_RANGE'
    }

    $maxBytes = Get-MaxBytes $FullPath
    $size = Get-FileLength $FullPath $maxBytes
    if ($ChunkOffset -ge $size) { Fail 'E_RANGE' }

    $toRead = [int][Math]::Min([long]$ChunkLength, [long]($size - $ChunkOffset))
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $FullPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        $before = [long]$stream.Length
        if ($before -ne $size) { Fail 'E_IO' }

        [void]$stream.Seek($ChunkOffset, [IO.SeekOrigin]::Begin)
        $buffer = New-Object byte[] $toRead
        $read = 0
        while ($read -lt $toRead) {
            $got = $stream.Read($buffer, $read, $toRead - $read)
            if ($got -le 0) { break }
            $read += $got
        }

        $after = [long]$stream.Length
        if ($read -ne $toRead -or $after -ne $before) { Fail 'E_IO' }
        return [Convert]::ToBase64String($buffer, 0, $read)
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Open-Zip([string]$FullPath) {
    $zipItem = Get-ItemOrFail $FullPath 'E_ZIP'
    if ($zipItem.PSIsContainer) { Fail 'E_ZIP' }
    if ($zipItem.Length -le 0 -or $zipItem.Length -gt $Script:MaxZipBytes) {
        Fail 'E_SIZE'
    }

    if (-not ('System.IO.Compression.ZipFile' -as [type])) {
        try {
            Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
        } catch {
            Fail 'E_ZIP'
        }
    }
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($FullPath)
    } catch [IO.IOException] {
        Fail 'E_ZIP_BUSY'
    } catch {
        Fail 'E_ZIP'
    }

    if ($zip.Entries.Count -gt $Script:MaxZipEntries) {
        $zip.Dispose()
        Fail 'E_ZIP_COUNT'
    }
    return $zip
}

function Get-ZipImageList($Zip) {
    $files = New-Object 'System.Collections.Generic.List[object]'
    foreach ($entry in $Zip.Entries) {
        if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) { continue }
        $extension = [IO.Path]::GetExtension(
            $entry.FullName.Replace('/', '\')).ToLowerInvariant()
        if ($Script:EntryExtensions -notcontains $extension) { continue }
        $files.Add($entry)
    }
    # The comma keeps a one-entry list from being unrolled into a scalar.
    return ,$files
}

function Get-ZipEntries([string]$FullPath, [long]$PageOffset, [int]$PageLimit) {
    if ($PageOffset -lt 0 -or $PageLimit -le 0 -or
        $PageLimit -gt $Script:MaxListLimit) {
        Fail 'E_RANGE'
    }

    $zip = Open-Zip $FullPath
    try {
        $files = Get-ZipImageList $zip
        $total = $files.Count
        $start = [int][Math]::Min($PageOffset, [long]$total)
        $end = [int][Math]::Min(
            ([long]$start + [long]$PageLimit), [long]$total)

        $entries = New-Object 'System.Collections.Generic.List[object]'
        for ($i = $start; $i -lt $end; $i++) {
            $entry = $files[$i]
            if ($entry.FullName.Length -gt $Script:MaxZipNameLength) {
                Fail 'E_ENTRY'
            }
            $entries.Add([pscustomobject]@{
                index             = $i
                name              = $entry.FullName
                length            = [long]$entry.Length
                compressed_length = [long]$entry.CompressedLength
                crc32             = [long]$entry.Crc32
                last_write_time   = $entry.LastWriteTime.ToString('o')
            })
        }

        $next = $null
        if ($end -lt $total) { $next = $end }

        $payload = [pscustomobject]@{
            total   = $total
            offset  = $start
            next    = $next
            entries = @($entries.ToArray())
        }
        return (ConvertTo-Json -InputObject $payload -Compress -Depth 5)
    } finally {
        $zip.Dispose()
    }
}

function Read-ZipEntryChunk([string]$FullPath, [long]$Index,
                            [long]$ChunkOffset, [int]$ChunkLength) {
    if ($Index -lt 0 -or $ChunkOffset -lt 0 -or $ChunkLength -le 0 -or
        $ChunkLength -gt $Script:MaxChunk) {
        Fail 'E_RANGE'
    }

    $zip = Open-Zip $FullPath
    try {
        $files = Get-ZipImageList $zip
        if ($Index -ge $files.Count) { Fail 'E_ENTRY' }

        $entry = $files[[int]$Index]
        if ($entry.FullName.Length -gt $Script:MaxZipNameLength) {
            Fail 'E_ENTRY'
        }
        if ($entry.Length -le 0 -or
            $entry.Length -gt $Script:MaxZipEntryBytes) {
            Fail 'E_SIZE'
        }
        if ($ChunkOffset -ge $entry.Length) { Fail 'E_RANGE' }

        $toRead = [int][Math]::Min(
            [long]$ChunkLength, [long]($entry.Length - $ChunkOffset))
        $stream = $entry.Open()
        try {
            $skip = [long]$ChunkOffset
            $scratch = New-Object byte[] 65536
            while ($skip -gt 0) {
                $want = [int][Math]::Min([long]$scratch.Length, $skip)
                $got = $stream.Read($scratch, 0, $want)
                if ($got -le 0) { Fail 'E_IO' }
                $skip -= $got
            }

            $buffer = New-Object byte[] $toRead
            $read = 0
            while ($read -lt $toRead) {
                $got = $stream.Read($buffer, $read, $toRead - $read)
                if ($got -le 0) { break }
                $read += $got
            }
            if ($read -ne $toRead) { Fail 'E_IO' }
            return [Convert]::ToBase64String($buffer, 0, $read)
        } finally {
            $stream.Dispose()
        }
    } finally {
        $zip.Dispose()
    }
}

function Get-EffectivePath([string]$Plain, [string]$Encoded, [string]$Code) {
    if ($Plain -and $Encoded) { Fail 'E_ARGS' }
    if ($Encoded) { return (ConvertFrom-Base64Url $Encoded) }
    if ($Plain) { return $Plain }
    Fail $Code
}

try {
    if ($DirList) {
        if ($Info -or $List -or $Path -or $PathB64 -or
            $ZipPath -or $ZipPathB64 -or $EntryIndex -ge 0) {
            Fail 'E_ARGS'
        }
        $dirValue = Get-EffectivePath $DirPath $DirPathB64 'E_ARGS'
        $fullDir = Resolve-AllowedDirectory $dirValue
        [System.Console]::WriteLine(
            (Get-DirectoryEntries $fullDir $DirOffset $DirLimit))
        exit 0
    }

    if ($Info) {
        if ($List -or $DirPath -or $DirPathB64 -or
            $ZipPath -or $ZipPathB64 -or $EntryIndex -ge 0) {
            Fail 'E_ARGS'
        }
        $fileValue = Get-EffectivePath $Path $PathB64 'E_ARGS'
        $fullFile = Resolve-AllowedPath $fileValue
        if ([IO.Path]::GetExtension($fullFile).ToLowerInvariant() -eq '.zip') {
            Fail 'E_EXT'
        }
        [System.Console]::WriteLine((Get-FileInfo $fullFile))
        exit 0
    }

    if ($List) {
        if ($Path -or $PathB64 -or $DirPath -or $DirPathB64 -or
            $EntryIndex -ge 0) {
            Fail 'E_ARGS'
        }
        $zipValue = Get-EffectivePath $ZipPath $ZipPathB64 'E_ARGS'
        $fullZip = Resolve-AllowedPath $zipValue
        if ([IO.Path]::GetExtension($fullZip).ToLowerInvariant() -ne '.zip') {
            Fail 'E_EXT'
        }
        [System.Console]::WriteLine(
            (Get-ZipEntries $fullZip $ListOffset $ListLimit))
        exit 0
    }

    if ($ZipPath -or $ZipPathB64 -or $EntryIndex -ge 0) {
        if ($Path -or $PathB64 -or $DirPath -or $DirPathB64) {
            Fail 'E_ARGS'
        }
        if ($EntryIndex -lt 0) { Fail 'E_ARGS' }

        $zipValue = Get-EffectivePath $ZipPath $ZipPathB64 'E_ARGS'
        $fullZip = Resolve-AllowedPath $zipValue
        if ([IO.Path]::GetExtension($fullZip).ToLowerInvariant() -ne '.zip') {
            Fail 'E_EXT'
        }
        [System.Console]::WriteLine(
            (Read-ZipEntryChunk $fullZip $EntryIndex $Offset $Length))
        exit 0
    }

    if ($DirPath -or $DirPathB64) { Fail 'E_ARGS' }

    $fileValue = Get-EffectivePath $Path $PathB64 'E_ARGS'
    $fullFile = Resolve-AllowedPath $fileValue
    if ([IO.Path]::GetExtension($fullFile).ToLowerInvariant() -eq '.zip') {
        Fail 'E_EXT'
    }
    [System.Console]::WriteLine(
        (Read-FileChunk $fullFile $Offset $Length))
    exit 0
} catch {
    $code = [string]$_.Exception.Message
    if ($code -notmatch '^E_[A-Z_]+$') {
        if ($Trace) { [System.Console]::Error.WriteLine($_.Exception.ToString()) }
        $code = 'E_IO'
    }
    [System.Console]::WriteLine("ERR:$code")
    exit 1
}
