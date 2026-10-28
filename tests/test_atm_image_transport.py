"""On-demand ATM image transport: path guards, chunk assembly, zip index.

The transport is a file-read primitive exposed through the Zabbix agent, so
the tests concentrate on the two things that must never regress: the allow-list
cannot be widened by a crafted path, and the host never accepts bytes that do
not match the ATM's size/hash (or a zip entry's CRC32).
"""
import base64
import hashlib
import json
import re
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import fetch_atm_image as f  # noqa: E402

JPEG = b'\xff\xd8\xff\xe0' + b'jpeg-payload' * 64
ZIP = r'C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip'
GRG = (r'E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_'
       r'[CarNum-8888########8888]CardInserted.jpg')


# ---------------------------------------------------------------------------
# Path and key guards
# ---------------------------------------------------------------------------

def test_confirmed_grg_file_is_allowed():
    assert f.validate_path(GRG) == GRG


def test_confirmed_ncr_zip_is_allowed():
    assert f.validate_path(ZIP, kind='zip') == ZIP


@pytest.mark.parametrize('path', [
    r'E:\pic\..\Windows\x.jpg',
    r'E:\pic\sub\..\..\Windows\x.jpg',
    r'E:\pic\a.jpg:ads',
    r'E:\pic\CON.jpg',
    r'E:\pic\COM1.jpeg',
    r'E:\pic\CON.tar.jpg',
    r'E:\pic\CON .jpg',
    'E:\\pic\\bad\nname.jpg',
    r'E:\pic\sub',
    r'\\server\share\a.jpg',
    r'\pic\a.jpg',
    r'E:pic\a.jpg',
    r'C:\Windows\x.jpg',
    r'C:\ISUITE\ICAM\Permit\icamimages2\x.jpg',
])
def test_unsafe_paths_are_refused(path):
    with pytest.raises(f.ImageFetchError):
        f.validate_path(path)


def test_extension_and_kind_are_enforced():
    with pytest.raises(f.ImageFetchError):
        f.validate_path(r'E:\pic\a.exe')
    with pytest.raises(f.ImageFetchError):
        f.validate_path(r'E:\pic\a.zip')
    with pytest.raises(f.ImageFetchError):
        f.validate_path(
            r'C:\ISUITE\ICAM\Permit\iCamImageZip\a.jpg', kind='zip')
    with pytest.raises(f.ImageFetchError):
        f.validate_path(ZIP)


def test_a_drive_root_is_not_a_valid_allow_list_root():
    with pytest.raises(f.ImageFetchError):
        f.validate_path(r'E:\pic\a.jpg', roots=('E:\\',))
    with pytest.raises(f.ImageFetchError):
        f.validate_path(r'E:\pic\a.jpg', roots=(r'\\server\share',))
    with pytest.raises(f.ImageFetchError):
        f.validate_path(r'E:\pic\a.jpg', roots=(r'E:\pic\..\Windows',))
    with pytest.raises(f.ImageFetchError):
        f.validate_path(r'E:\pic\a.jpg', roots=(r'\pic',))


DIR = r'E:\pic\2026\10\22'
ZIPDIR = r'C:\ISUITE\ICAM\Permit\iCamImageZip'


def test_confirmed_directories_are_allowed():
    assert f.validate_dir(DIR) == DIR
    assert f.validate_dir(ZIPDIR) == ZIPDIR
    assert f.validate_dir(r'E:\pic') == r'E:\pic'
    assert f.validate_dir(r'E:\pic\\') == r'E:\pic'


@pytest.mark.parametrize('path', [
    r'E:\pic\..\Windows',
    r'E:\pic\2026\..\..\Windows',
    r'E:\pic2',
    r'\\server\share\pic',
    r'\pic',
    r'E:pic',
    'C:\\',
    r'C:',
])
def test_unsafe_directories_are_refused(path):
    with pytest.raises(f.ImageFetchError):
        f.validate_dir(path)


def test_directory_validation_requires_an_absolute_path():
    with pytest.raises(f.ImageFetchError):
        f.validate_dir('pic')
    with pytest.raises(f.ImageFetchError):
        f.validate_dir('')


@pytest.mark.parametrize('name', [
    '',
    '../x.jpg',
    'a/../../b.jpg',
    '/x.jpg',
    r'\x.jpg',
    r'C:\x.jpg',
    'a:b.jpg',
    'a//b.jpg',
    'a\x00b.jpg',
    'a\x7fb.jpg',
])
def test_bad_zip_entry_names_are_refused(name):
    with pytest.raises(f.ImageFetchError):
        f.validate_entry(name)


@pytest.mark.parametrize('name', [
    'image.jpg',
    '2026/10/22/image.jpg',
    'a..b.jpg',
    'IMG_0001.JPG',
])
def test_normal_zip_entry_names_are_allowed(name):
    assert f.validate_entry(name) == name


def test_mask_sensitive_keeps_the_timestamp_but_masks_the_pan():
    path = (r'E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_'
            r'[CarNum-1234567890123456]CardInserted.jpg')
    masked = f.mask_sensitive(path)
    assert '261022153444' in masked
    assert '1234********3456' in masked
    assert '1234567890123456' not in masked


def test_mask_sensitive_handles_a_grouped_pan():
    value = 'CarNum-1234-5678-9012-3456.jpg'
    assert '1234********3456' in f.mask_sensitive(value)
    assert '1234-5678-9012-3456' not in f.mask_sensitive(value)


def test_b64url_is_safe_inside_a_zabbix_key():
    encoded = f.b64url(GRG)
    assert '=' not in encoded
    assert '+' not in encoded
    assert '/' not in encoded
    padded = encoded + '=' * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == GRG


def test_overlong_keys_are_refused_before_any_process_call():
    key = 'atm.image.chunk["' + 'a' * 300 + '",0,1]'
    with pytest.raises(f.ImageFetchError) as exc:
        f.zbx_get('10.0.0.1', key, command='/bin/true', retries=0)
    assert str(exc.value) == 'E_KEY'


def test_a_host_that_looks_like_an_option_is_refused():
    with pytest.raises(f.ImageFetchError) as exc:
        f.zbx_get('-k', 'atm.image.info["x"]', command='/bin/true', retries=0)
    assert str(exc.value) == 'E_HOST'


# ---------------------------------------------------------------------------
# File mode
# ---------------------------------------------------------------------------

def _file_agent(monkeypatch, data, *, sha256=None, md5=None):
    calls = []

    def fake(host, key, **kwargs):
        calls.append(key)
        if key.startswith('atm.image.info'):
            payload = {'size': len(data)}
            if sha256 is not None:
                payload['sha256'] = sha256
            if md5 is not None:
                payload['md5'] = md5
            return json.dumps(payload).encode()
        if key.startswith('atm.image.chunk'):
            match = re.search(r',(\d+),(\d+)\]$', key)
            offset, length = int(match.group(1)), int(match.group(2))
            return base64.b64encode(data[offset:offset + length])
        raise AssertionError(key)

    monkeypatch.setattr(f, 'zbx_get', fake)
    return calls


def test_file_fetch_verifies_sha256_and_writes_atomically(monkeypatch, tmp_path):
    data = JPEG + b'x' * 1000
    sha = hashlib.sha256(data).hexdigest()
    calls = _file_agent(monkeypatch, data, sha256=sha)
    out = tmp_path / 'image.jpg'

    result = f.fetch_file('10.0.0.1', GRG, out, chunk=256)

    assert out.read_bytes() == data
    assert result['sha256'] == sha
    assert result['bytes_written'] == len(data)
    assert result['source_masked'] == GRG
    assert not list(tmp_path.glob('*.part'))
    assert any(key.startswith('atm.image.info') for key in calls)
    assert any(key.startswith('atm.image.chunk') for key in calls)


def test_file_info_returns_metadata_without_fetching_bytes(monkeypatch):
    data = JPEG + b'y' * 100
    sha = hashlib.sha256(data).hexdigest()
    calls = _file_agent(monkeypatch, data, sha256=sha)

    result = f.fetch_info('10.0.0.1', GRG)

    assert result['size'] == len(data)
    assert result['sha256'] == sha
    assert len(calls) == 1
    assert calls[0].startswith('atm.image.info')


def test_fetch_bytes_returns_verified_bytes_without_a_file(monkeypatch):
    data = JPEG + b'm' * 500
    sha = hashlib.sha256(data).hexdigest()
    _file_agent(monkeypatch, data, sha256=sha)

    payload, result = f.fetch_bytes('10.0.0.1', GRG, chunk=128)

    assert payload == data
    assert result['size'] == len(data)
    assert result['sha256'] == sha
    assert result['kind'] == 'file-bytes'


def test_md5_only_info_is_still_verified(monkeypatch, tmp_path):
    data = JPEG + b'z' * 100
    md5 = hashlib.md5(data).hexdigest()
    _file_agent(monkeypatch, data, md5=md5)

    out = tmp_path / 'image.jpg'
    result = f.fetch_file('10.0.0.1', GRG, out, chunk=128)

    assert out.read_bytes() == data
    assert result['md5'] == md5


def test_hash_mismatch_leaves_no_output(monkeypatch, tmp_path):
    data = JPEG + b'x' * 100
    _file_agent(monkeypatch, data, sha256='0' * 64)
    out = tmp_path / 'image.jpg'

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_file('10.0.0.1', GRG, out, chunk=128)

    assert str(exc.value) == 'E_SHA256'
    assert not out.exists()
    assert not list(tmp_path.glob('*.part'))


def test_content_that_is_not_a_jpeg_is_refused(monkeypatch, tmp_path):
    data = b'<html>not an image</html>'
    sha = hashlib.sha256(data).hexdigest()
    _file_agent(monkeypatch, data, sha256=sha)
    out = tmp_path / 'image.jpg'

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_file('10.0.0.1', GRG, out, chunk=128)

    assert str(exc.value) == 'E_CONTENT'
    assert not out.exists()


def test_bad_hash_shape_is_refused(monkeypatch):
    _file_agent(monkeypatch, JPEG, sha256='not-a-hash')
    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_info('10.0.0.1', GRG)
    assert str(exc.value) == 'E_HASH'


# ---------------------------------------------------------------------------
# Zip mode
# ---------------------------------------------------------------------------

def _entry(index, name, data):
    return {
        'index': index,
        'name': name,
        'length': len(data),
        'compressed_length': len(data),
        'crc32': zlib.crc32(data) & 0xFFFFFFFF,
        'last_write_time': '2026-10-22T10:00:00+03:00',
        'data': data,
    }


def _zip_agent(monkeypatch, entries):
    calls = []

    def fake(host, key, **kwargs):
        calls.append(key)
        if key.startswith('atm.image.ziplist'):
            match = re.search(r',(\d+),(\d+)\]$', key)
            offset, limit = int(match.group(1)), int(match.group(2))
            page = entries[offset:offset + limit]
            next_offset = offset + len(page) if offset + len(page) < len(entries) else None
            return json.dumps({
                'total': len(entries),
                'offset': offset,
                'next': next_offset,
                'entries': [
                    {k: v for k, v in item.items() if k != 'data'}
                    for item in page
                ],
            }).encode()
        if key.startswith('atm.image.zipentry'):
            match = re.search(r',(\d+),(\d+),(\d+)\]$', key)
            index, offset, length = (int(match.group(i)) for i in (1, 2, 3))
            data = entries[index]['data']
            return base64.b64encode(data[offset:offset + length])
        raise AssertionError(key)

    monkeypatch.setattr(f, 'zbx_get', fake)
    return calls


def test_zip_listing_is_paged_and_indexed(monkeypatch):
    entries = [
        _entry(0, 'first.jpg', JPEG + b'0'),
        _entry(1, 'second.jpg', JPEG + b'1'),
        _entry(2, 'third.jpg', JPEG + b'2'),
    ]
    calls = _zip_agent(monkeypatch, entries)
    monkeypatch.setattr(f, 'LIST_PAGE', 1)

    result = f.list_zip('10.0.0.1', ZIP, retries=0)

    assert [item['index'] for item in result] == [0, 1, 2]
    assert [item['name'] for item in result] == [
        'first.jpg', 'second.jpg', 'third.jpg']
    assert all(item['fetchable'] for item in result)
    assert sum(key.startswith('atm.image.ziplist') for key in calls) == 3


def test_zip_listing_skips_non_image_entries(monkeypatch):
    entries = [
        _entry(0, 'image.jpg', JPEG + b'0'),
        _entry(1, 'notes.txt', b'not an image'),
    ]
    _zip_agent(monkeypatch, entries)

    with pytest.raises(f.ImageFetchError) as exc:
        f.list_zip('10.0.0.1', ZIP, retries=0)

    assert str(exc.value) == 'E_EXT'


def test_zip_entry_is_fetched_by_index_without_the_name_in_the_key(
        monkeypatch, tmp_path):
    data = JPEG + b'entry'
    entries = [_entry(0, 'first.jpg', JPEG + b'0'), _entry(1, 'second.jpg', data)]
    calls = _zip_agent(monkeypatch, entries)
    out = tmp_path / 'entry.jpg'

    result = f.fetch_zip_entry('10.0.0.1', ZIP, out, index=1, chunk=64)

    assert out.read_bytes() == data
    assert result['index'] == 1
    assert result['entry'] == 'second.jpg'
    assert result['entry_masked'] == 'second.jpg'
    chunk_keys = [key for key in calls if key.startswith('atm.image.zipentry')]
    assert chunk_keys
    assert all('second.jpg' not in key for key in chunk_keys)
    assert all(f.b64url('second.jpg') not in key for key in chunk_keys)


def test_zip_entry_can_be_selected_by_name(monkeypatch, tmp_path):
    data = JPEG + b'named'
    entries = [_entry(0, 'first.jpg', JPEG + b'0'), _entry(1, 'named.jpg', data)]
    _zip_agent(monkeypatch, entries)
    out = tmp_path / 'named.jpg'

    result = f.fetch_zip_entry('10.0.0.1', ZIP, out, entry='named.jpg',
                               chunk=64)

    assert out.read_bytes() == data
    assert result['index'] == 1


def test_zip_entry_bytes_are_returned_without_a_file(monkeypatch):
    data = JPEG + b'bytes'
    entries = [_entry(0, 'first.jpg', data)]
    _zip_agent(monkeypatch, entries)

    payload, result = f.fetch_zip_entry_bytes(
        '10.0.0.1', ZIP, index=0, chunk=64)

    assert payload == data
    assert result['index'] == 0
    assert result['entry_masked'] == 'first.jpg'
    assert result['kind'] == 'zip-entry-bytes'


def test_zip_crc_mismatch_leaves_no_output(monkeypatch, tmp_path):
    data = JPEG + b'crc'
    entry = _entry(0, 'first.jpg', data)
    entry['crc32'] = (entry['crc32'] + 1) & 0xFFFFFFFF
    _zip_agent(monkeypatch, [entry])
    out = tmp_path / 'entry.jpg'

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_zip_entry('10.0.0.1', ZIP, out, index=0, chunk=64)

    assert str(exc.value) == 'E_CRC32'
    assert not out.exists()


def test_zip_crc_mismatch_is_also_refused_in_memory(monkeypatch):
    data = JPEG + b'crc'
    entry = _entry(0, 'first.jpg', data)
    entry['crc32'] = (entry['crc32'] + 1) & 0xFFFFFFFF
    _zip_agent(monkeypatch, [entry])

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_zip_entry_bytes('10.0.0.1', ZIP, index=0, chunk=64)

    assert str(exc.value) == 'E_CRC32'


def test_zip_changed_since_index_is_refused(monkeypatch, tmp_path):
    entries = [_entry(0, 'first.jpg', JPEG + b'0')]
    _zip_agent(monkeypatch, entries)
    out = tmp_path / 'entry.jpg'

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_zip_entry('10.0.0.1', ZIP, out, index=0,
                          expected_crc32=123, chunk=64)

    assert str(exc.value) == 'E_CHANGED'
    assert not out.exists()


def test_duplicate_zip_entry_names_are_ambiguous(monkeypatch, tmp_path):
    entries = [
        _entry(0, 'dup.jpg', JPEG + b'0'),
        _entry(1, 'dup.jpg', JPEG + b'1'),
    ]
    _zip_agent(monkeypatch, entries)

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_zip_entry('10.0.0.1', ZIP, tmp_path / 'x.jpg',
                          entry='dup.jpg', chunk=64)

    assert str(exc.value) == 'E_ENTRY'


def test_zip_entry_traversal_is_refused_before_listing(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(f, 'zbx_get', lambda *a, **k: called.append(a))

    with pytest.raises(f.ImageFetchError) as exc:
        f.fetch_zip_entry('10.0.0.1', ZIP, tmp_path / 'x.jpg',
                          entry='../x.jpg', chunk=64)

    assert str(exc.value) == 'E_ENTRY'
    assert called == []


# ---------------------------------------------------------------------------
# Directory mode
# ---------------------------------------------------------------------------

def _dir_rows(entries):
    return [
        {
            'index': index,
            'name': name,
            'length': length,
            'last_write_time': '2026-10-22T15:34:44.0000000+03:00',
        }
        for index, (name, length) in enumerate(entries)
    ]


def _dir_agent(monkeypatch, rows):
    calls = []

    def fake(host, key, **kwargs):
        calls.append(key)
        if key.startswith('atm.image.dir'):
            match = re.search(r',(\d+),(\d+)\]$', key)
            offset, limit = int(match.group(1)), int(match.group(2))
            page = rows[offset:offset + limit]
            next_offset = (offset + len(page)
                           if offset + len(page) < len(rows) else None)
            return json.dumps({
                'total': len(rows),
                'offset': offset,
                'next': next_offset,
                'entries': page,
            }).encode()
        raise AssertionError(key)

    monkeypatch.setattr(f, 'zbx_get', fake)
    return calls


def test_directory_listing_is_paged_and_masked(monkeypatch):
    rows = _dir_rows([
        ('CAM[1]_[DateTime-261022153444]_[CarNum-1234567890123456]'
         'CardInserted.jpg', 114843),
        ('CAM[1]_[DateTime-261022153519]_[CarNum-1234567890123456]'
         'EnterInfo.jpg', 100000),
        ('CAM[1]_[DateTime-261022153444]_[CarNum-1234567890123456].mp4',
         1000),
    ])
    calls = _dir_agent(monkeypatch, rows)
    monkeypatch.setattr(f, 'LIST_PAGE', 1)

    entries = f.list_dir('10.0.0.1', DIR, retries=0)

    assert [item['index'] for item in entries] == [0, 1, 2]
    assert entries[0]['path'].endswith('CardInserted.jpg')
    assert '1234********3456' in entries[0]['name_masked']
    assert '1234567890123456' not in entries[0]['path_masked']
    assert entries[0]['kind'] == 'image'
    assert entries[2]['kind'] == 'video'
    assert all(item['fetchable'] for item in entries)
    assert sum(key.startswith('atm.image.dir') for key in calls) == 3


def test_empty_directory_is_a_valid_listing(monkeypatch):
    _dir_agent(monkeypatch, [])

    assert f.list_dir('10.0.0.1', DIR, retries=0) == []


def test_directory_listing_rejects_a_traversal_name(monkeypatch):
    _dir_agent(monkeypatch, _dir_rows([('../x.jpg', 100)]))

    with pytest.raises(f.ImageFetchError) as exc:
        f.list_dir('10.0.0.1', DIR, retries=0)

    assert str(exc.value) == 'E_ENTRY'


def test_directory_listing_rejects_a_non_evidence_entry(monkeypatch):
    _dir_agent(monkeypatch, _dir_rows([('notes.txt', 100)]))

    with pytest.raises(f.ImageFetchError) as exc:
        f.list_dir('10.0.0.1', DIR, retries=0)

    assert str(exc.value) == 'E_EXT'


def test_directory_listing_rejects_a_gap_in_the_index(monkeypatch):
    rows = _dir_rows([('a.jpg', 100), ('b.jpg', 100)])
    rows[1]['index'] = 7
    _dir_agent(monkeypatch, rows)

    with pytest.raises(f.ImageFetchError) as exc:
        f.list_dir('10.0.0.1', DIR, retries=0)

    assert str(exc.value) == 'E_CHANGED'


def test_directory_listing_marks_oversized_entries_unfetchable(monkeypatch):
    rows = _dir_rows([
        ('huge.jpg', f.MAX_FILE_BYTES + 1),
        ('huge.mp4', f.MAX_VIDEO_BYTES + 1),
        ('huge.zip', f.MAX_ZIP_BYTES + 1),
    ])
    _dir_agent(monkeypatch, rows)

    entries = f.list_dir('10.0.0.1', DIR, retries=0)

    assert [item['fetchable'] for item in entries] == [False, False, False]
    assert [item['kind'] for item in entries] == ['image', 'video', 'zip']


def test_directory_mode_refuses_a_file_path_outside_the_roots(monkeypatch):
    called = []
    monkeypatch.setattr(f, 'zbx_get', lambda *a, **k: called.append(a))

    with pytest.raises(f.ImageFetchError) as exc:
        f.list_dir('10.0.0.1', r'C:\Windows', retries=0)

    assert str(exc.value) == 'E_ROOT'
    assert called == []


# ---------------------------------------------------------------------------
# Static guardrails on the ATM script and its configuration
# ---------------------------------------------------------------------------

PS1 = (ROOT / 'scripts' / 'atm-image-chunk.ps1').read_text()
ROOTS = (ROOT / 'config' / 'zabbix' / 'atm-image-roots.txt').read_text()
USERPARAMS = (ROOT / 'config' / 'zabbix' / 'atm-agent-userparams.conf').read_text()


def test_chunk_script_writes_one_clean_line():
    # Write-Output wraps long JSON/base64 at console width and inserts CRLF;
    # the WMI collector hit exactly that bug on 2026-10-09.
    assert '[System.Console]::WriteLine' in PS1
    assert 'Write-Output' not in PS1


def test_chunk_script_runs_on_powershell_2():
    assert '#Requires -Version 2.0' in PS1
    assert 'ConvertTo-Json -InputObject' not in PS1
    assert 'Join-Path $PSScriptRoot' not in PS1
    assert 'System.IO.Compression.ZipFile' not in PS1
    assert ' -shr ' not in PS1
    assert 'ConvertTo-JsonText' in PS1
    assert 'DeflateStream' in PS1


def test_chunk_script_avoids_dotnet4_only_apis():
    # The NCR's PowerShell 2.0 runs on CLR 2.0.  These APIs exist in .NET 4
    # but not there; both were hit on the live ATM on 2026-10-28.
    assert '[string]::IsNullOrWhiteSpace' not in PS1
    assert '$sha.Dispose()' not in PS1
    assert '$sha.Clear()' in PS1
    assert 'CopyTo(' not in PS1


def test_chunk_script_has_no_write_or_execute_cmdlets():
    for cmdlet in ('Remove-Item', 'Set-Content', 'Out-File', 'New-Item',
                   'Move-Item', 'Copy-Item', 'Start-Process',
                   'Invoke-Expression'):
        assert cmdlet not in PS1


def test_roots_file_is_required_and_narrow():
    assert "Test-Path -LiteralPath $config -PathType Leaf" in PS1
    assert "Fail 'E_CONFIG'" in PS1
    roots = [line.strip() for line in ROOTS.splitlines()
             if line.strip() and not line.strip().startswith('#')]
    assert roots == [
        r'E:\pic',
        r'C:\ISUITE\ICAM\Permit\icamimages',
        r'C:\ISUITE\ICAM\Permit\iCamImageZip',
        r'D:\Image\icamimages',
    ]


def test_zip_list_helper_does_not_unroll_a_single_entry():
    assert 'return ,$files' in PS1
    assert 'Get-ZipImageList' in PS1
    assert '$Script:EntryExtensions' in PS1


def test_userparams_expose_read_only_keys_and_zip_index():
    assert 'atm.image.info[*]' in USERPARAMS
    assert 'atm.image.chunk[*]' in USERPARAMS
    assert 'atm.image.dir[*]' in USERPARAMS
    assert 'atm.image.ziplist[*]' in USERPARAMS
    assert 'atm.image.zipentry[*]' in USERPARAMS
    assert '-DirPathB64 "$1" -DirList' in USERPARAMS
    assert '-EntryIndex "$2"' in USERPARAMS
    assert '-EntryB64' not in USERPARAMS
    assert 'Timeout=30' in USERPARAMS


def test_directory_listing_is_non_recursive_and_paged():
    assert 'Get-DirectoryEntries' in PS1
    assert 'Get-ChildItem -LiteralPath $FullPath -Force' in PS1
    assert '-Recurse' not in PS1
    assert 'MaxDirEntries' in PS1
    assert 'DirOffset' in PS1
    assert 'DirLimit' in PS1


def test_directory_resolver_rejects_reserved_device_names():
    # The file resolver and the directory resolver must share the check.
    assert PS1.count('$first -match') == 2


def test_read_only_codes_are_documented():
    for code in ('E_MISSING', 'E_REPARSE', 'E_ROOT', 'E_EXT',
                 'E_CONFIG', 'E_ZIP_BUSY', 'E_COUNT'):
        assert code in PS1


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------

def test_result_redaction_hides_raw_paths_and_entry_names():
    result = {
        'kind': 'zip-list',
        'zip': ZIP,
        'zip_masked': ZIP,
        'entries': [
            {'index': 0, 'name': '1234567890123456.jpg',
             'name_masked': '1234********3456.jpg'},
        ],
    }

    redacted = f._redact_result(result)

    assert 'zip' not in redacted
    assert 'name' not in redacted['entries'][0]
    assert redacted['entries'][0]['name_masked'] == '1234********3456.jpg'


def test_result_redaction_hides_raw_directory_and_entry_paths():
    result = {
        'kind': 'dir-list',
        'dir': DIR,
        'dir_masked': DIR,
        'entries': [
            {'index': 0, 'name': '1234567890123456.jpg',
             'name_masked': '1234********3456.jpg',
             'path': DIR + r'\1234567890123456.jpg',
             'path_masked': DIR + r'\1234********3456.jpg'},
        ],
    }

    redacted = f._redact_result(result)

    assert 'dir' not in redacted
    assert 'name' not in redacted['entries'][0]
    assert 'path' not in redacted['entries'][0]
    assert redacted['entries'][0]['path_masked'].endswith(
        r'\1234********3456.jpg')


def test_cli_rejects_dir_with_an_output_path():
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--dir', DIR, '--out', '/tmp/x'])


def test_cli_rejects_dir_with_zip_actions():
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--dir', DIR, '--list'])
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--dir', DIR, '--info'])


def test_cli_rejects_a_chunk_too_small_for_magic_checks():
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--path', GRG, '--chunk', '1',
                '--out', '/tmp/x.jpg'])


def test_cli_rejects_info_with_output_path():
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--path', GRG, '--info',
                '--out', '/tmp/x.jpg'])


def test_cli_rejects_zip_without_an_action():
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--zip', ZIP])


def test_cli_rejects_entry_and_index_together():
    with pytest.raises(SystemExit):
        f.main(['--host', '10.0.0.1', '--zip', ZIP, '--entry', 'a.jpg',
                '--index', '0', '--out', '/tmp/x.jpg'])
