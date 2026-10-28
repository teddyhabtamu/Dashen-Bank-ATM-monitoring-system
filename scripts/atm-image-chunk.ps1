#Requires -Version 2.0
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

# PowerShell 2.0 has no $PSScriptRoot. Resolve the script folder once so the
# roots file is always read from beside the script, whether the agent invokes
# it by absolute path or the operator runs it from another directory.
$Script:ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Script:ScriptRoot) {
    $Script:ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if (-not $Script:ScriptRoot) { throw 'E_CONFIG' }

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

# PowerShell 2.0 has no ConvertTo-Json.  The payloads here are small and
# fixed-shape, so a tiny serializer is enough and keeps one output format on
# every ATM regardless of PowerShell version.
function ConvertTo-JsonString([string]$Value) {
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

function ConvertTo-JsonText {
    param($Value)

    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string]) { return (ConvertTo-JsonString $Value) }
    if ($Value -is [bool]) {
        if ($Value) { return 'true' }
        return 'false'
    }
    if ($Value -is [int] -or $Value -is [long] -or
        $Value -is [uint32] -or $Value -is [byte]) {
        return [string]$Value
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $parts = @()
        foreach ($key in $Value.Keys) {
            $parts += ((ConvertTo-JsonString ([string]$key)) + ':' +
                       (ConvertTo-JsonText $Value[$key]))
        }
        return '{' + ($parts -join ',') + '}'
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $parts = @()
        foreach ($item in $Value) {
            $parts += (ConvertTo-JsonText $item)
        }
        return '[' + ($parts -join ',') + ']'
    }

    $parts = @()
    foreach ($prop in $Value.PSObject.Properties) {
        $memberType = [string]$prop.MemberType
        if ($memberType -eq 'NoteProperty' -or $memberType -eq 'Property') {
            $parts += ((ConvertTo-JsonString ([string]$prop.Name)) + ':' +
                       (ConvertTo-JsonText $prop.Value))
        }
    }
    return '{' + ($parts -join ',') + '}'
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
    $config = Join-Path $Script:ScriptRoot 'atm-image-roots.txt'
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

    $leaf = [IO.Path]::GetFileName($full)
    if ([string]::IsNullOrEmpty($leaf)) { Fail 'E_PATH' }
    $first = $leaf.Split('.')[0].TrimEnd([char[]]@(' ', '.'))
    if ($first -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
        Fail 'E_PATH'
    }

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
        $entries.Add((New-Object PSObject -Property @{
            index           = $i
            name            = $file.Name
            length          = [long]$file.Length
            last_write_time = $file.LastWriteTime.ToString('o')
        }))
    }

    $next = $null
    if ($end -lt $total) { $next = $end }

    $payload = New-Object PSObject -Property @{
        total   = $total
        offset  = $start
        next    = $next
        entries = @($entries.ToArray())
    }
    return (ConvertTo-JsonText $payload)
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
    return (ConvertTo-JsonText (New-Object PSObject -Property @{
        size   = [long]$size
        sha256 = $hex
    }))
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

function Get-ZipEntryTime([int]$Date, [int]$Time) {
    $year = 1980 + [int](($Date -band 0xFE00) / 512)
    $month = [int](($Date -band 0x01E0) / 32)
    $day = $Date -band 0x001F
    $hour = [int](($Time -band 0xF800) / 2048)
    $minute = [int](($Time -band 0x07E0) / 32)
    $second = ($Time -band 0x001F) * 2

    if ($month -lt 1 -or $month -gt 12 -or
        $day -lt 1 -or $day -gt 31 -or
        $hour -gt 23 -or $minute -gt 59 -or $second -gt 59) {
        return [datetime]::MinValue
    }
    try {
        $value = Get-Date -Year $year -Month $month -Day $day `
            -Hour $hour -Minute $minute -Second $second
        return $value.AddTicks(-($value.Ticks % [TimeSpan]::TicksPerSecond))
    } catch {
        return [datetime]::MinValue
    }
}

function Open-FallbackZipEntry($Entry) {
    $fs = $null
    try {
        $fs = [IO.File]::Open(
            $Entry.ZipPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        [void]$fs.Seek([long]$Entry.LocalOffset, [IO.SeekOrigin]::Begin)
        $br = New-Object System.IO.BinaryReader -ArgumentList $fs

        if ($br.ReadUInt32() -ne [uint32]0x04034b50) { Fail 'E_ZIP' }
        [void]$br.ReadUInt16()              # version needed
        $flags = $br.ReadUInt16()
        $method = $br.ReadUInt16()
        [void]$br.ReadUInt16()              # time
        [void]$br.ReadUInt16()              # date
        [void]$br.ReadUInt32()              # crc
        [void]$br.ReadUInt32()              # compressed size
        [void]$br.ReadUInt32()              # uncompressed size
        $nameLen = $br.ReadUInt16()
        $extraLen = $br.ReadUInt16()

        if (($flags -band 1) -ne 0) { Fail 'E_ZIP' }
        if ([int]$method -ne [int]$Entry.Method) { Fail 'E_ZIP' }

        $dataOffset = [long]$Entry.LocalOffset + 30 +
                      [long]$nameLen + [long]$extraLen
        if ($dataOffset -gt $fs.Length) { Fail 'E_ZIP' }

        if ([int]$Entry.Method -eq 0) {
            [void]$fs.Seek($dataOffset, [IO.SeekOrigin]::Begin)
            return $fs
        }

        if ([int]$Entry.Method -eq 8) {
            $compressed = [long]$Entry.CompressedLength
            if ($compressed -le 0 -or
                $compressed -gt ([long]$Script:MaxZipEntryBytes + 1048576)) {
                Fail 'E_SIZE'
            }
            if ($dataOffset + $compressed -gt $fs.Length) { Fail 'E_ZIP' }

            [void]$fs.Seek($dataOffset, [IO.SeekOrigin]::Begin)
            $buffer = New-Object byte[] ([int]$compressed)
            $read = 0
            while ($read -lt $buffer.Length) {
                $got = $fs.Read($buffer, $read, $buffer.Length - $read)
                if ($got -le 0) { Fail 'E_ZIP' }
                $read += $got
            }
            $fs.Dispose()
            $fs = $null

            $memory = New-Object System.IO.MemoryStream
            $memory.Write($buffer, 0, $buffer.Length)
            $memory.Position = 0
            return (New-Object System.IO.Compression.DeflateStream `
                -ArgumentList @(
                    $memory,
                    [System.IO.Compression.CompressionMode]::Decompress
                ))
        }

        Fail 'E_ZIP'
    } catch {
        if ($fs) { $fs.Dispose() }
        throw
    }
}

function Open-ZipFallback([string]$FullPath) {
    $fs = $null
    try {
        $fs = [IO.File]::Open(
            $FullPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        $length = [long]$fs.Length
        if ($length -lt 22) { Fail 'E_ZIP' }

        $tailLength = [int][Math]::Min($length, [long]65557)
        $tail = New-Object byte[] $tailLength
        [void]$fs.Seek($length - $tailLength, [IO.SeekOrigin]::Begin)
        $tailRead = 0
        while ($tailRead -lt $tailLength) {
            $got = $fs.Read($tail, $tailRead, $tailLength - $tailRead)
            if ($got -le 0) { Fail 'E_ZIP' }
            $tailRead += $got
        }

        $eocd = -1
        for ($i = $tailLength - 22; $i -ge 0; $i--) {
            if ($tail[$i] -eq 0x50 -and $tail[$i + 1] -eq 0x4B -and
                $tail[$i + 2] -eq 0x05 -and $tail[$i + 3] -eq 0x06) {
                $eocd = $i
                break
            }
        }
        if ($eocd -lt 0) { Fail 'E_ZIP' }

        $count = [int][BitConverter]::ToUInt16($tail, $eocd + 10)
        $cdSize = [long][BitConverter]::ToUInt32($tail, $eocd + 12)
        $cdOffset = [long][BitConverter]::ToUInt32($tail, $eocd + 16)

        if ($count -eq 0xFFFF -or $cdSize -eq [long][uint32]::MaxValue -or
            $cdOffset -eq [long][uint32]::MaxValue) {
            Fail 'E_ZIP'   # ZIP64 is outside this reader
        }
        if ($cdOffset + $cdSize -gt $length) { Fail 'E_ZIP' }
        if ($count -gt $Script:MaxZipEntries) { Fail 'E_ZIP_COUNT' }

        [void]$fs.Seek($cdOffset, [IO.SeekOrigin]::Begin)
        $br = New-Object System.IO.BinaryReader -ArgumentList $fs
        $entries = New-Object 'System.Collections.Generic.List[object]'

        for ($n = 0; $n -lt $count; $n++) {
            if ($br.ReadUInt32() -ne [uint32]0x02014b50) { Fail 'E_ZIP' }
            [void]$br.ReadUInt16()          # version made by
            [void]$br.ReadUInt16()          # version needed
            $flags = $br.ReadUInt16()
            $method = $br.ReadUInt16()
            $time = $br.ReadUInt16()
            $date = $br.ReadUInt16()
            $crc = $br.ReadUInt32()
            $compressed = $br.ReadUInt32()
            $uncompressed = $br.ReadUInt32()
            $nameLen = $br.ReadUInt16()
            $extraLen = $br.ReadUInt16()
            $commentLen = $br.ReadUInt16()
            [void]$br.ReadUInt16()          # disk start
            [void]$br.ReadUInt16()          # internal attributes
            [void]$br.ReadUInt32()          # external attributes
            $localOffset = $br.ReadUInt32()

            $nameBytes = $br.ReadBytes([int]$nameLen)
            if ($extraLen -gt 0) { [void]$br.ReadBytes([int]$extraLen) }
            if ($commentLen -gt 0) { [void]$br.ReadBytes([int]$commentLen) }

            if (($flags -band 1) -ne 0) { Fail 'E_ZIP' }
            if ($compressed -eq [uint32]::MaxValue -or
                $uncompressed -eq [uint32]::MaxValue -or
                $localOffset -eq [uint32]::MaxValue) {
                Fail 'E_ZIP'   # ZIP64 is outside this reader
            }

            if (($flags -band 0x800) -ne 0) {
                $name = [Text.Encoding]::UTF8.GetString($nameBytes)
            } else {
                $name = [Text.Encoding]::Default.GetString($nameBytes)
            }
            $entryTime = Get-ZipEntryTime -Date ([int]$date) -Time ([int]$time)

            $entry = New-Object PSObject -Property @{
                ZipPath          = $FullPath
                FullName         = $name
                Length           = [long]$uncompressed
                CompressedLength = [long]$compressed
                Crc32            = [long]$crc
                Method           = [int]$method
                LocalOffset      = [long]$localOffset
                LastWriteTime    = $entryTime
            }
            $entry | Add-Member -MemberType ScriptMethod -Name Open -Value {
                return (Open-FallbackZipEntry $this)
            }
            $entries.Add($entry)
        }

        $zip = New-Object PSObject -Property @{
            Entries = @($entries.ToArray())
        }
        $zip | Add-Member -MemberType ScriptMethod -Name Dispose -Value { }
        return $zip
    } catch {
        throw
    } finally {
        if ($fs) { $fs.Dispose() }
    }
}

function Open-Zip([string]$FullPath) {
    $zipItem = Get-ItemOrFail $FullPath 'E_ZIP'
    if ($zipItem.PSIsContainer) { Fail 'E_ZIP' }
    if ($zipItem.Length -le 0 -or $zipItem.Length -gt $Script:MaxZipBytes) {
        Fail 'E_SIZE'
    }

    try {
        $zip = Open-ZipFallback $FullPath
    } catch [IO.IOException] {
        Fail 'E_ZIP_BUSY'
    } catch {
        $code = [string]$_.Exception.Message
        if ($code -match '^E_[A-Z_]+$') { Fail $code }
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

        # Do not hand an archive entry name to [IO.Path]: a name can contain
        # characters Windows rejects in a filesystem path, and those names are
        # still valid inside a zip.  The extension check only needs the leaf.
        $leaf = $entry.FullName.Replace('\', '/')
        $slash = $leaf.LastIndexOf('/')
        if ($slash -ge 0) { $leaf = $leaf.Substring($slash + 1) }
        $dot = $leaf.LastIndexOf('.')
        if ($dot -lt 0) { continue }
        $extension = $leaf.Substring($dot).ToLowerInvariant()

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
            $entries.Add((New-Object PSObject -Property @{
                index             = $i
                name              = $entry.FullName
                length            = [long]$entry.Length
                compressed_length = [long]$entry.CompressedLength
                crc32             = [long]$entry.Crc32
                last_write_time   = $entry.LastWriteTime.ToString('o')
            }))
        }

        $next = $null
        if ($end -lt $total) { $next = $end }

        $payload = New-Object PSObject -Property @{
            total   = $total
            offset  = $start
            next    = $next
            entries = @($entries.ToArray())
        }
        return (ConvertTo-JsonText $payload)
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
