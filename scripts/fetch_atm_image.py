#!/usr/bin/env python3
"""Fetch ATM image evidence on demand through the Zabbix agent.

This is the host-side half of the read-only image chunk transport:

    atm-image-chunk.ps1   (on the ATM)
      atm.image.info[<b64url path>]                  -> {"size": n, "sha256": "..."}
      atm.image.chunk[<b64url path>,o,l]             -> base64 chunk
      atm.image.dir[<b64url dir>,o,n]                -> {"total":n,"next":...}
      atm.image.ziplist[<b64url zip>,o,n]            -> {"total":n,"next":...}
      atm.image.zipentry[<b64url zip>,i,o,l]         -> base64 chunk

No image bytes are stored centrally.  The CLI writes the requested file to
``--out`` only because a human asked for it; the portal bridge will hold the
same chunks in memory, verify them, stream them, and discard them.

Safety rules, applied here and again on the ATM:

* only absolute Windows paths under the configured roots;
* only image/video/zip extensions;
* no ``..`` traversal, UNC paths, alternate data streams, reserved device
  names, or NUL bytes;
* 32 MiB still-image cap, 8 MiB video/zip-entry cap, 32 KiB chunk cap;
* size + SHA-256 (or MD5) verification before the output is published
  atomically;
* zip entries are verified by size + CRC32 and selected by archive index,
  so an entry name never appears in a Zabbix key;
* a JPEG/PNG/MP4 magic-byte check rejects an error page saved as an image.

Usage on the UAT host:

    # metadata only, no bytes written
    python3 scripts/fetch_atm_image.py --host 10.41.30.115 \
      --path 'E:\\pic\\2026\\10\\22\\CAM[1]...jpg' --info

    # fetch one file
    python3 scripts/fetch_atm_image.py --host 10.41.30.115 \
      --path 'E:\\pic\\2026\\10\\22\\CAM[1]...jpg' \
      --out /tmp/grg-22oct.jpg

    # list the GRG day folder (non-recursive, paged)
    python3 scripts/fetch_atm_image.py --host 10.41.30.115 \
      --dir 'E:\\pic\\2026\\10\\22'

    # list the NCR zip folder, then one daily zip
    python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
      --dir 'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip'
    python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
      --zip 'C:\\ISUITE\\ICAM\\Permit\\iCamImageZip\\...zip' --list

    # fetch one zip entry by name or by index
    python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
      --zip 'C:\\...\\...zip' --entry 'image.jpg' --out /tmp/ncr.jpg

Requirements: python3 and zabbix_get.  The ATM UserParameters are in
config/zabbix/atm-agent-userparams.conf.  The agent's own Timeout must be at
least 10 seconds (30 recommended); each call starts PowerShell.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import logging
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import Iterable, Iterator, Sequence

LOG = logging.getLogger('atm-image')

DEFAULT_ROOTS = (
    r'E:\pic',
    r'C:\ISUITE\ICAM\Permit\icamimages',
    r'C:\ISUITE\ICAM\Permit\iCamImageZip',
    r'D:\Image\icamimages',
)
IMAGE_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png'})
VIDEO_EXTENSIONS = frozenset({'.mp4'})
FILE_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
ZIP_EXTENSIONS = frozenset({'.zip'})
ENTRY_EXTENSIONS = FILE_EXTENSIONS
DIR_EXTENSIONS = FILE_EXTENSIONS | ZIP_EXTENSIONS

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 8 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 8 * 1024 * 1024
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_DIR_ENTRIES = 5000
MAX_CHUNK = 32768
MIN_CHUNK = 16
DEFAULT_CHUNK = 24576
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 1
LIST_PAGE = 25
MAX_KEY_LENGTH = 240
MAX_ZIP_NAME_LENGTH = 256

_CARD_RE = re.compile(r'(?<!\d)(\d{13,19})(?!\d)')
_GROUPED_CARD_RE = re.compile(r'(?<!\d)(\d{4}(?:[ -]\d{4}){2,3})(?!\d)')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f\u2028\u2029]')
_MD5_RE = re.compile(r'^[0-9a-f]{32}$')
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_RESERVED_RE = re.compile(
    r'^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$')


class ImageFetchError(RuntimeError):
    """A refused or failed image fetch, with a stable, path-free reason."""


def b64url(value: str) -> str:
    """Base64url without padding: safe inside a Zabbix item key."""
    encoded = base64.urlsafe_b64encode(value.encode('utf-8')).decode('ascii')
    return encoded.rstrip('=')


def mask_sensitive(value: str) -> str:
    """Mask PAN-like digit runs for logs and summaries.

    The ATM filenames can carry a full card number.  The exact name is needed
    to fetch the file, but it must not be copied into a ticket or log.
    Handles both contiguous and space/hyphen-grouped PANs.
    """
    def _mask_digits(digits: str) -> str:
        return digits[:4] + '*' * (len(digits) - 8) + digits[-4:]

    def _replace_grouped(match: re.Match[str]) -> str:
        return _mask_digits(re.sub(r'\D', '', match.group(1)))

    def _replace(match: re.Match[str]) -> str:
        return _mask_digits(match.group(1))

    grouped = _GROUPED_CARD_RE.sub(_replace_grouped, value)
    masked = _CARD_RE.sub(_replace, grouped)
    return _CONTROL_RE.sub('?', masked)


def _md5():
    """MD5 for transport integrity, not authentication; tolerate FIPS mode."""
    try:
        return hashlib.md5(usedforsecurity=False)
    except TypeError:
        try:
            return hashlib.md5()
        except ValueError:
            return None
    except ValueError:
        return None


def _normalise_windows_path(value: str) -> str:
    if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ImageFetchError('E_PATH')
    if not ntpath.isabs(value):
        raise ImageFetchError('E_PATH')
    if value.startswith('\\') and not value.startswith('\\\\'):
        raise ImageFetchError('E_ROOT')
    if value.startswith('\\\\'):
        raise ImageFetchError('E_ROOT')  # UNC shares are outside the contract

    normal = ntpath.normpath(value.replace('/', '\\'))
    if not ntpath.isabs(normal) or normal.startswith('\\\\'):
        raise ImageFetchError('E_PATH')
    if ':' in normal[2:]:
        raise ImageFetchError('E_PATH')  # alternate data stream
    return normal


def _is_narrow_root(value: str) -> bool:
    if not value or not ntpath.isabs(value) or value.startswith('\\\\'):
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if re.search(r'(^|[\\/])\.\.([\\/]|$)', value):
        return False
    normal = ntpath.normpath(value.replace('/', '\\'))
    if not ntpath.isabs(normal) or normal.startswith('\\\\'):
        return False
    if normal.startswith('\\') and not normal.startswith('\\\\'):
        return False
    if normal == '\\':
        return False
    if re.fullmatch(r'[A-Za-z]:\\?', normal):
        return False
    if ':' in normal[2:]:
        return False
    return not re.search(r'[\*\?]', normal)


def validate_path(value: str, roots: Sequence[str] = DEFAULT_ROOTS,
                  *, kind: str = 'file') -> str:
    """Return the normalised path if it is inside an allow-listed root."""
    normal = _normalise_windows_path(value)
    extension = ntpath.splitext(normal)[1].casefold()

    if kind == 'file':
        allowed = FILE_EXTENSIONS
    elif kind == 'zip':
        allowed = ZIP_EXTENSIONS
    else:
        raise ValueError(f'unknown path kind: {kind}')
    if extension not in allowed:
        raise ImageFetchError('E_EXT')

    leaf = ntpath.basename(normal)
    if not leaf or leaf in ('.', '..'):
        raise ImageFetchError('E_PATH')
    stem = ntpath.splitext(leaf)[0]
    if _RESERVED_RE.match(stem.split('.')[0].rstrip(' .')):
        raise ImageFetchError('E_PATH')

    normal_cf = normal.casefold()
    for root in roots:
        if not _is_narrow_root(str(root)):
            continue
        root_normal = ntpath.normpath(str(root).replace('/', '\\'))
        root_cf = root_normal.casefold().rstrip('\\')
        if normal_cf == root_cf:
            raise ImageFetchError('E_PATH')
        if normal_cf.startswith(root_cf + '\\'):
            return normal
    raise ImageFetchError('E_ROOT')


def validate_dir(value: str, roots: Sequence[str] = DEFAULT_ROOTS) -> str:
    """Return the normalised directory if it is inside an allow-listed root.

    Unlike a file path, a directory may be the allow-list root itself.  The
    listing is non-recursive; callers derive the day/zip folder explicitly.
    """
    normal = _normalise_windows_path(value).rstrip('\\')
    if not normal or normal.endswith(':'):
        raise ImageFetchError('E_PATH')

    leaf = ntpath.basename(normal)
    if not leaf or leaf in ('.', '..'):
        raise ImageFetchError('E_PATH')
    if _RESERVED_RE.match(leaf.split('.')[0].rstrip(' .')):
        raise ImageFetchError('E_PATH')

    normal_cf = normal.casefold()
    for root in roots:
        if not _is_narrow_root(str(root)):
            continue
        root_normal = ntpath.normpath(str(root).replace('/', '\\'))
        root_cf = root_normal.casefold().rstrip('\\')
        if normal_cf == root_cf:
            return normal
        if normal_cf.startswith(root_cf + '\\'):
            return normal
    raise ImageFetchError('E_ROOT')


def validate_entry(value: str) -> str:
    """Reject zip entries that could escape the archive or a Windows path."""
    if not isinstance(value, str) or not value or len(value) > MAX_ZIP_NAME_LENGTH:
        raise ImageFetchError('E_ENTRY')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ImageFetchError('E_ENTRY')
    if ':' in value:
        raise ImageFetchError('E_ENTRY')
    if value.startswith(('/', '\\')):
        raise ImageFetchError('E_ENTRY')
    parts = re.split(r'[/\\]', value)
    if any(part in ('', '.', '..') for part in parts):
        raise ImageFetchError('E_ENTRY')
    return value


def _resolve_zabbix_get(command: str | None = None) -> str:
    if command:
        return command
    env = os.environ.get('ZBX_GET')
    if env:
        return env
    for candidate in ('/usr/bin/zabbix_get', '/usr/local/bin/zabbix_get'):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which('zabbix_get')
    if found:
        return found
    raise ImageFetchError('zabbix_get not found; install it or set ZBX_GET')


def zbx_get(host: str, key: str, *, timeout: int = DEFAULT_TIMEOUT,
            command: str | None = None,
            retries: int = DEFAULT_RETRIES) -> bytes:
    """Call zabbix_get and return the value, or raise ImageFetchError.

    Refusals from the ATM script (`ERR:...`) and Zabbix's own
    `ZBX_NOTSUPPORTED` are deterministic: they are never retried.  Timeouts
    and empty responses are retried with a small backoff.
    """
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ImageFetchError('E_KEY')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
        raise ImageFetchError('E_KEY')
    if (not host or host.startswith('-') or len(host) > 255
            or any(ord(ch) <= 32 or ord(ch) == 127 for ch in host)):
        raise ImageFetchError('E_HOST')

    binary = _resolve_zabbix_get(command)
    effective_timeout = max(1, min(int(timeout), 30))
    attempts = max(0, min(int(retries), 5)) + 1
    last_error: ImageFetchError | None = None

    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                [binary, '-t', str(effective_timeout), '-s', host, '-k', key],
                capture_output=True,
                timeout=effective_timeout + 5,
            )
        except subprocess.TimeoutExpired:
            last_error = ImageFetchError('agent timeout')
        except OSError as exc:
            raise ImageFetchError(f'agent call failed: {exc}') from exc
        else:
            if proc.returncode == 0:
                out = proc.stdout.strip()
                if out:
                    head = out[:200].lstrip()
                    if (head.startswith(b'ZBX_NOTSUPPORTED')
                            or head.startswith(b'ERR:')):
                        message = head.decode('utf-8', 'replace').strip()[:300]
                        raise ImageFetchError(mask_sensitive(message))
                    return out
                last_error = ImageFetchError('empty agent response')
            else:
                message = (proc.stderr or proc.stdout or b'fetch failed').decode(
                    'utf-8', 'replace').strip()
                first = message.splitlines()[0] if message else 'fetch failed'
                if 'ZBX_NOTSUPPORTED' in first or 'ERR:' in first:
                    raise ImageFetchError(mask_sensitive(first[:300]))
                last_error = ImageFetchError(mask_sensitive(first[:300]))

        if attempt + 1 < attempts:
            time.sleep(0.5 * (attempt + 1))

    raise last_error or ImageFetchError('agent call failed')


def _as_int(value, *, default=None) -> int:
    if isinstance(value, bool):
        raise ImageFetchError('E_JSON')
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    if default is not None:
        return default
    raise ImageFetchError('E_JSON')


def _max_bytes_for(path: str) -> int:
    if ntpath.splitext(path)[1].casefold() in VIDEO_EXTENSIONS:
        return MAX_VIDEO_BYTES
    return MAX_FILE_BYTES


def _file_info(host: str, path: str, *, timeout: int,
               command: str | None,
               retries: int) -> tuple[int, str | None, str | None]:
    key = f'atm.image.info["{b64url(path)}"]'
    raw = zbx_get(host, key, timeout=timeout, command=command, retries=retries)
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageFetchError('E_JSON') from exc
    if not isinstance(payload, dict):
        raise ImageFetchError('E_JSON')

    size = _as_int(payload.get('size'))
    sha256 = str(payload.get('sha256') or '').strip().lower()
    md5 = str(payload.get('md5') or '').strip().lower()
    if sha256 and not _SHA256_RE.match(sha256):
        raise ImageFetchError('E_HASH')
    if md5 and not _MD5_RE.match(md5):
        raise ImageFetchError('E_HASH')
    if not sha256 and not md5:
        raise ImageFetchError('E_HASH')

    max_bytes = _max_bytes_for(path)
    if size <= 0 or size > max_bytes:
        raise ImageFetchError('E_SIZE')
    return size, sha256 or None, md5 or None


def _file_chunks(host: str, path: str, size: int, *, chunk: int,
                 timeout: int, command: str | None,
                 retries: int) -> Iterator[bytes]:
    offset = 0
    while offset < size:
        length = min(chunk, size - offset)
        LOG.debug('file %s offset=%d length=%d',
                  mask_sensitive(path), offset, length)
        key = f'atm.image.chunk["{b64url(path)}",{offset},{length}]'
        raw = zbx_get(host, key, timeout=timeout, command=command,
                      retries=retries)
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageFetchError('E_BASE64') from exc
        if len(data) != length:
            raise ImageFetchError('E_CHUNK')
        yield data
        offset += length


def _zip_chunks(host: str, zip_path: str, index: int, size: int, *,
                chunk: int, timeout: int,
                command: str | None, retries: int) -> Iterator[bytes]:
    offset = 0
    while offset < size:
        length = min(chunk, size - offset)
        LOG.debug('zip %s index=%d offset=%d length=%d',
                  mask_sensitive(zip_path), index, offset, length)
        key = (f'atm.image.zipentry["{b64url(zip_path)}",'
               f'{index},{offset},{length}]')
        raw = zbx_get(host, key, timeout=timeout, command=command,
                      retries=retries)
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageFetchError('E_BASE64') from exc
        if len(data) != length:
            raise ImageFetchError('E_CHUNK')
        yield data
        offset += length


def _check_magic(name: str, head: bytes) -> None:
    """Reject an error page or text file saved under an image extension."""
    extension = ntpath.splitext(name)[1].casefold()
    if extension in ('.jpg', '.jpeg'):
        if not head.startswith(b'\xff\xd8\xff'):
            raise ImageFetchError('E_CONTENT')
    elif extension == '.png':
        if not head.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ImageFetchError('E_CONTENT')
    elif extension in VIDEO_EXTENSIONS:
        if len(head) < 12 or head[4:8] not in (
                b'ftyp', b'moov', b'mdat', b'free', b'skip'):
            raise ImageFetchError('E_CONTENT')


def _collect(chunks: Iterable[bytes], expected_size: int, *,
             expected_sha256: str | None = None,
             expected_md5: str | None = None,
             expected_crc32: int | None = None,
             content_name: str | None = None) -> tuple[bytes, dict]:
    """Collect chunks in memory, verify them, and return the bytes + metadata.

    Used by the future portal bridge, which must stream an image without
    writing a central copy to disk.
    """
    buffer = bytearray()
    sha256 = hashlib.sha256()
    md5 = _md5()
    crc = 0
    first = True

    for data in chunks:
        if not isinstance(data, (bytes, bytearray)):
            raise ImageFetchError('E_IO')
        if first:
            if content_name is not None:
                _check_magic(content_name, bytes(data[:16]))
            first = False
        buffer.extend(data)
        sha256.update(data)
        if md5 is not None:
            md5.update(data)
        crc = zlib.crc32(data, crc)

    size = len(buffer)
    if size != expected_size:
        raise ImageFetchError('E_SIZE')
    if expected_sha256 is not None and sha256.hexdigest() != expected_sha256:
        raise ImageFetchError('E_SHA256')
    if expected_md5 is not None:
        if md5 is None:
            raise ImageFetchError('E_HASH')
        if md5.hexdigest() != expected_md5:
            raise ImageFetchError('E_MD5')
    if expected_crc32 is not None \
            and (crc & 0xFFFFFFFF) != (expected_crc32 & 0xFFFFFFFF):
        raise ImageFetchError('E_CRC32')

    return bytes(buffer), {
        'size': size,
        'sha256': sha256.hexdigest(),
        'md5': md5.hexdigest() if md5 is not None else None,
        'crc32': crc & 0xFFFFFFFF,
    }


def _atomic_write(out: Path, chunks: Iterable[bytes], expected_size: int,
                  *, expected_sha256: str | None = None,
                  expected_md5: str | None = None,
                  expected_crc32: int | None = None,
                  content_name: str | None = None) -> dict:
    """Write chunks to a private temp file, verify, then publish atomically."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=out.name + '.', suffix='.part', dir=str(out.parent))
    temp = Path(temp_name)
    size = 0
    sha256 = hashlib.sha256()
    md5 = _md5()
    crc = 0
    first = True

    try:
        with os.fdopen(fd, 'wb') as handle:
            for data in chunks:
                if not isinstance(data, (bytes, bytearray)):
                    raise ImageFetchError('E_IO')
                if first:
                    if content_name is not None:
                        _check_magic(content_name, bytes(data[:16]))
                    first = False
                handle.write(data)
                size += len(data)
                sha256.update(data)
                if md5 is not None:
                    md5.update(data)
                crc = zlib.crc32(data, crc)
            handle.flush()
            os.fsync(handle.fileno())

        if size != expected_size:
            raise ImageFetchError('E_SIZE')
        if expected_sha256 is not None and sha256.hexdigest() != expected_sha256:
            raise ImageFetchError('E_SHA256')
        if expected_md5 is not None:
            if md5 is None:
                raise ImageFetchError('E_HASH')
            if md5.hexdigest() != expected_md5:
                raise ImageFetchError('E_MD5')
        if expected_crc32 is not None \
                and (crc & 0xFFFFFFFF) != (expected_crc32 & 0xFFFFFFFF):
            raise ImageFetchError('E_CRC32')

        os.replace(temp, out)
        return {
            'bytes_written': size,
            'sha256': sha256.hexdigest(),
            'md5': md5.hexdigest() if md5 is not None else None,
            'crc32': crc & 0xFFFFFFFF,
        }
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def fetch_info(host: str, path: str, *,
               timeout: int = DEFAULT_TIMEOUT,
               roots: Sequence[str] = DEFAULT_ROOTS,
               command: str | None = None,
               retries: int = DEFAULT_RETRIES) -> dict:
    """Return size + hashes without fetching image bytes."""
    source = validate_path(path, roots, kind='file')
    size, sha256, md5 = _file_info(host, source, timeout=timeout,
                                   command=command, retries=retries)
    return {
        'kind': 'info',
        'host': host,
        'source': source,
        'source_masked': mask_sensitive(source),
        'size': size,
        'sha256': sha256,
        'md5': md5,
    }


def fetch_file(host: str, path: str, out: str | Path, *,
               chunk: int = DEFAULT_CHUNK, timeout: int = DEFAULT_TIMEOUT,
               roots: Sequence[str] = DEFAULT_ROOTS,
               command: str | None = None,
               retries: int = DEFAULT_RETRIES) -> dict:
    """Fetch one image/video file from the ATM and verify it."""
    if chunk < MIN_CHUNK or chunk > MAX_CHUNK:
        raise ImageFetchError('E_CHUNK')

    source = validate_path(path, roots, kind='file')
    size, sha256, md5 = _file_info(host, source, timeout=timeout,
                                   command=command, retries=retries)
    written = _atomic_write(
        Path(out),
        _file_chunks(host, source, size, chunk=chunk, timeout=timeout,
                     command=command, retries=retries),
        size,
        expected_sha256=sha256,
        expected_md5=md5,
        content_name=source,
    )
    return {
        'kind': 'file',
        'host': host,
        'source': source,
        'source_masked': mask_sensitive(source),
        'out': str(out),
        **written,
    }


def fetch_bytes(host: str, path: str, *,
                chunk: int = DEFAULT_CHUNK, timeout: int = DEFAULT_TIMEOUT,
                roots: Sequence[str] = DEFAULT_ROOTS,
                command: str | None = None,
                retries: int = DEFAULT_RETRIES) -> tuple[bytes, dict]:
    """Fetch and verify one image in memory, with no file written.

    This is the API the portal bridge will use.  The bytes are held only for
    the life of the request and are never persisted centrally.
    """
    if chunk < MIN_CHUNK or chunk > MAX_CHUNK:
        raise ImageFetchError('E_CHUNK')

    source = validate_path(path, roots, kind='file')
    size, sha256, md5 = _file_info(host, source, timeout=timeout,
                                   command=command, retries=retries)
    data, meta = _collect(
        _file_chunks(host, source, size, chunk=chunk, timeout=timeout,
                     command=command, retries=retries),
        size,
        expected_sha256=sha256,
        expected_md5=md5,
        content_name=source,
    )
    return data, {
        'kind': 'file-bytes',
        'host': host,
        'source': source,
        'source_masked': mask_sensitive(source),
        **meta,
    }


def _parse_dir_entry(item: dict, directory: str) -> dict:
    """Validate one directory-listing row and add its fetchable path."""
    if not isinstance(item, dict):
        raise ImageFetchError('E_JSON')
    name = item.get('name')
    if not isinstance(name, str) or not name:
        raise ImageFetchError('E_JSON')
    if len(name) > MAX_ZIP_NAME_LENGTH:
        raise ImageFetchError('E_ENTRY')
    if '/' in name or '\\' in name:
        raise ImageFetchError('E_ENTRY')
    validate_entry(name)

    extension = ntpath.splitext(name)[1].casefold()
    if extension not in DIR_EXTENSIONS:
        raise ImageFetchError('E_EXT')
    if _RESERVED_RE.match(ntpath.splitext(name)[0].split('.')[0].rstrip(' .')):
        raise ImageFetchError('E_ENTRY')

    index = _as_int(item.get('index'))
    length = _as_int(item.get('length'))
    if index < 0 or length < 0:
        raise ImageFetchError('E_JSON')

    if extension in ZIP_EXTENSIONS:
        kind = 'zip'
        max_bytes = MAX_ZIP_BYTES
    elif extension in VIDEO_EXTENSIONS:
        kind = 'video'
        max_bytes = MAX_VIDEO_BYTES
    else:
        kind = 'image'
        max_bytes = MAX_FILE_BYTES

    path = ntpath.join(directory, name)
    return {
        'index': index,
        'name': name,
        'name_masked': mask_sensitive(name),
        'path': path,
        'path_masked': mask_sensitive(path),
        'length': length,
        'last_write_time': str(item.get('last_write_time') or ''),
        'kind': kind,
        'fetchable': 0 < length <= max_bytes,
    }


def list_dir(host: str, dir_path: str, *,
             timeout: int = DEFAULT_TIMEOUT,
             roots: Sequence[str] = DEFAULT_ROOTS,
             command: str | None = None,
             retries: int = DEFAULT_RETRIES) -> list[dict]:
    """List one allow-listed ATM directory, page by page, non-recursively.

    This replaces the recursive ``vfs.dir.get`` for evidence discovery.  It
    returns names/sizes/timestamps only; a caller fetches hashes with
    :func:`fetch_info` or bytes with :func:`fetch_bytes` when needed.
    """
    source = validate_dir(dir_path, roots)
    offset = 0
    total: int | None = None
    entries: list[dict] = []

    while True:
        key = (f'atm.image.dir["{b64url(source)}",'
               f'{offset},{LIST_PAGE}]')
        raw = zbx_get(host, key, timeout=timeout, command=command,
                      retries=retries)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageFetchError('E_JSON') from exc
        if not isinstance(payload, dict):
            raise ImageFetchError('E_JSON')

        page_total = _as_int(payload.get('total'))
        page_offset = _as_int(payload.get('offset'))
        if page_total < 0 or page_total > MAX_DIR_ENTRIES:
            raise ImageFetchError('E_COUNT')
        if page_offset != offset:
            raise ImageFetchError('E_JSON')
        if total is None:
            total = page_total
        elif total != page_total:
            raise ImageFetchError('E_CHANGED')

        page = payload.get('entries')
        if not isinstance(page, list):
            raise ImageFetchError('E_JSON')
        if not page and payload.get('next') is not None:
            raise ImageFetchError('E_JSON')
        for item in page:
            entries.append(_parse_dir_entry(item, source))

        next_offset = payload.get('next')
        if next_offset is None:
            break
        next_offset = _as_int(next_offset)
        if next_offset <= offset or next_offset > total:
            raise ImageFetchError('E_JSON')
        offset = next_offset
        if len(entries) > MAX_DIR_ENTRIES:
            raise ImageFetchError('E_COUNT')

    if total is None:
        raise ImageFetchError('E_JSON')
    if len(entries) != total:
        raise ImageFetchError('E_CHANGED')
    if [item['index'] for item in entries] != list(range(total)):
        raise ImageFetchError('E_CHANGED')
    return entries


def _parse_zip_entry(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ImageFetchError('E_JSON')
    name = item.get('name')
    if not isinstance(name, str) or not name:
        raise ImageFetchError('E_JSON')
    validate_entry(name)
    if ntpath.splitext(name)[1].casefold() not in ENTRY_EXTENSIONS:
        raise ImageFetchError('E_EXT')

    index = _as_int(item.get('index'))
    length = _as_int(item.get('length'))
    if index < 0 or length < 0:
        raise ImageFetchError('E_JSON')

    return {
        'index': index,
        'name': name,
        'name_masked': mask_sensitive(name),
        'length': length,
        'compressed_length': _as_int(item.get('compressed_length'), default=0),
        'crc32': _as_int(item.get('crc32')),
        'last_write_time': str(item.get('last_write_time') or ''),
        'fetchable': 0 < length <= MAX_ZIP_ENTRY_BYTES,
    }


def list_zip(host: str, zip_path: str, *,
             timeout: int = DEFAULT_TIMEOUT,
             roots: Sequence[str] = DEFAULT_ROOTS,
             command: str | None = None,
             retries: int = DEFAULT_RETRIES) -> list[dict]:
    """List a daily NCR image zip, page by page, without extracting it."""
    source = validate_path(zip_path, roots, kind='zip')
    offset = 0
    total: int | None = None
    entries: list[dict] = []

    while True:
        key = (f'atm.image.ziplist["{b64url(source)}",'
               f'{offset},{LIST_PAGE}]')
        raw = zbx_get(host, key, timeout=timeout, command=command,
                      retries=retries)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageFetchError('E_JSON') from exc
        if not isinstance(payload, dict):
            raise ImageFetchError('E_JSON')

        page_total = _as_int(payload.get('total'))
        page_offset = _as_int(payload.get('offset'))
        if page_total < 0 or page_total > MAX_ZIP_ENTRIES:
            raise ImageFetchError('E_ZIP_COUNT')
        if page_offset != offset:
            raise ImageFetchError('E_JSON')
        if total is None:
            total = page_total
        elif total != page_total:
            raise ImageFetchError('E_CHANGED')

        page = payload.get('entries')
        if not isinstance(page, list):
            raise ImageFetchError('E_JSON')
        if not page and payload.get('next') is not None:
            raise ImageFetchError('E_JSON')
        for item in page:
            entries.append(_parse_zip_entry(item))

        next_offset = payload.get('next')
        if next_offset is None:
            break
        next_offset = _as_int(next_offset)
        if next_offset <= offset or next_offset > total:
            raise ImageFetchError('E_JSON')
        offset = next_offset
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ImageFetchError('E_ZIP_COUNT')

    if total is None:
        raise ImageFetchError('E_JSON')
    if len(entries) != total:
        raise ImageFetchError('E_CHANGED')
    if [item['index'] for item in entries] != list(range(total)):
        raise ImageFetchError('E_CHANGED')
    return entries


def _validate_zip_request(entry: str | None, index: int | None,
                          chunk: int) -> None:
    if chunk < MIN_CHUNK or chunk > MAX_CHUNK:
        raise ImageFetchError('E_CHUNK')
    if (entry is None) == (index is None):
        raise ImageFetchError('E_ARGS')
    if entry is not None:
        validate_entry(entry)
    if index is not None and (isinstance(index, bool)
                              or not isinstance(index, int)
                              or index < 0):
        raise ImageFetchError('E_ARGS')


def _select_zip_entry(entries: list[dict], entry: str | None,
                      index: int | None,
                      expected_size: int | None,
                      expected_crc32: int | None) -> dict:
    if index is not None:
        if index >= len(entries):
            raise ImageFetchError('E_ENTRY')
        match = entries[index]
        if match['index'] != index:
            raise ImageFetchError('E_CHANGED')
    else:
        matches = [item for item in entries if item['name'] == entry]
        if len(matches) != 1:
            raise ImageFetchError('E_ENTRY')
        match = matches[0]

    if expected_size is not None and match['length'] != expected_size:
        raise ImageFetchError('E_CHANGED')
    if expected_crc32 is not None \
            and (int(match['crc32']) & 0xFFFFFFFF) != (int(expected_crc32) & 0xFFFFFFFF):
        raise ImageFetchError('E_CHANGED')
    if not match['fetchable']:
        raise ImageFetchError('E_SIZE')
    return match


def fetch_zip_entry(host: str, zip_path: str, out: str | Path, *,
                    entry: str | None = None,
                    index: int | None = None,
                    expected_size: int | None = None,
                    expected_crc32: int | None = None,
                    chunk: int = DEFAULT_CHUNK,
                    timeout: int = DEFAULT_TIMEOUT,
                    roots: Sequence[str] = DEFAULT_ROOTS,
                    command: str | None = None,
                    retries: int = DEFAULT_RETRIES) -> dict:
    """Fetch one entry from a daily NCR zip and verify size + CRC32.

    The entry is selected from a fresh listing; the chunk key carries only
    the zip path and the entry index, never the entry name.  The caller may
    pass expected size/CRC32 from its metadata index to detect a replaced zip.
    """
    _validate_zip_request(entry, index, chunk)

    source = validate_path(zip_path, roots, kind='zip')
    entries = list_zip(host, source, timeout=timeout, roots=roots,
                       command=command, retries=retries)
    match = _select_zip_entry(entries, entry, index,
                              expected_size, expected_crc32)

    size = int(match['length'])
    written = _atomic_write(
        Path(out),
        _zip_chunks(host, source, int(match['index']), size, chunk=chunk,
                    timeout=timeout, command=command, retries=retries),
        size,
        expected_crc32=int(match['crc32']),
        content_name=match['name'],
    )
    return {
        'kind': 'zip-entry',
        'host': host,
        'zip': source,
        'zip_masked': mask_sensitive(source),
        'entry': match['name'],
        'entry_masked': match['name_masked'],
        'index': match['index'],
        'out': str(out),
        **written,
    }


def fetch_zip_entry_bytes(host: str, zip_path: str, *,
                          entry: str | None = None,
                          index: int | None = None,
                          expected_size: int | None = None,
                          expected_crc32: int | None = None,
                          chunk: int = DEFAULT_CHUNK,
                          timeout: int = DEFAULT_TIMEOUT,
                          roots: Sequence[str] = DEFAULT_ROOTS,
                          command: str | None = None,
                          retries: int = DEFAULT_RETRIES) -> tuple[bytes, dict]:
    """Fetch and verify one zip entry in memory, with no file written."""
    _validate_zip_request(entry, index, chunk)

    source = validate_path(zip_path, roots, kind='zip')
    entries = list_zip(host, source, timeout=timeout, roots=roots,
                       command=command, retries=retries)
    match = _select_zip_entry(entries, entry, index,
                              expected_size, expected_crc32)

    size = int(match['length'])
    data, meta = _collect(
        _zip_chunks(host, source, int(match['index']), size, chunk=chunk,
                    timeout=timeout, command=command, retries=retries),
        size,
        expected_crc32=int(match['crc32']),
        content_name=match['name'],
    )
    return data, {
        'kind': 'zip-entry-bytes',
        'host': host,
        'zip': source,
        'zip_masked': mask_sensitive(source),
        'entry': match['name'],
        'entry_masked': match['name_masked'],
        'index': match['index'],
        **meta,
    }


def _redact_result(result: dict) -> dict:
    """Remove raw ATM paths/entry names from CLI output.

    The functions return them because the bridge needs them to fetch.  The
    human-facing CLI prints only masked forms by default; a pasted ticket must
    not become a copy of a card-bearing filename.
    """
    redacted = dict(result)
    for key in ('source', 'zip', 'entry', 'dir'):
        redacted.pop(key, None)
    if isinstance(redacted.get('entries'), list):
        redacted['entries'] = [
            {key: value for key, value in item.items()
             if key not in ('name', 'path')}
            for item in redacted['entries']
        ]
    return redacted


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Fetch ATM image evidence on demand through the Zabbix agent.')
    parser.add_argument('--host', required=True,
                        help='ATM address reachable by zabbix_get')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--path', help='absolute file path on the ATM')
    source.add_argument('--zip', help='absolute NCR image zip path on the ATM')
    source.add_argument('--dir',
                        help='absolute directory to list (non-recursive)')
    parser.add_argument('--out', help='output file for --path or --zip entry')
    parser.add_argument('--list', action='store_true',
                        help='with --zip: list entries instead of extracting')
    parser.add_argument('--entry', help='with --zip: entry name to extract')
    parser.add_argument('--index', type=int,
                        help='with --zip: entry index to extract')
    parser.add_argument('--info', action='store_true',
                        help='with --path: return size/hashes, write nothing')
    parser.add_argument('--chunk', type=int, default=DEFAULT_CHUNK,
                        help=f'bytes per agent call (default: {DEFAULT_CHUNK}, max: {MAX_CHUNK})')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help='zabbix_get timeout in seconds (default: 30, max: 30)')
    parser.add_argument('--retries', type=int, default=DEFAULT_RETRIES,
                        help='retries for timeouts/empty responses '
                             f'(default: {DEFAULT_RETRIES}, max: 5)')
    parser.add_argument('--root', action='append', default=None,
                        help='add a host-side allowed root (the ATM roots '
                             'file must also allow it; repeatable)')
    parser.add_argument('--zbx-get', default=None,
                        help='zabbix_get binary (default: $ZBX_GET or zabbix_get)')
    parser.add_argument('--show-sensitive', action='store_true',
                        help='print raw ATM paths/entry names (default: masked)')
    parser.add_argument('--verbose', action='store_true')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.chunk < MIN_CHUNK or args.chunk > MAX_CHUNK:
        parser.error(f'--chunk must be between {MIN_CHUNK} and {MAX_CHUNK}')
    if args.timeout < 1 or args.timeout > 30:
        parser.error('--timeout must be between 1 and 30')
    if args.retries < 0 or args.retries > 5:
        parser.error('--retries must be between 0 and 5')

    if args.dir:
        if (args.info or args.list or args.entry is not None
                or args.index is not None):
            parser.error(
                '--info/--list/--entry/--index are not valid with --dir')
        if args.out:
            parser.error('--out is not used with --dir')
    elif args.path:
        if args.list or args.entry is not None or args.index is not None:
            parser.error('--list/--entry/--index are only valid with --zip')
        if args.info and args.out:
            parser.error('--out is not used with --info')
        if not args.info and not args.out:
            parser.error('--out is required with --path')
    else:
        if args.info:
            parser.error('--info is only valid with --path')
        if args.list and (args.entry is not None or args.index is not None):
            parser.error('--list and --entry/--index are mutually exclusive')
        if args.list and args.out:
            parser.error('--out is not used with --list')
        if not args.list and args.entry is None and args.index is None:
            parser.error('--zip requires --list, --entry or --index')
        if not args.list and not args.out:
            parser.error('--out is required with --entry/--index')
        if args.entry is not None and args.index is not None:
            parser.error('--entry and --index are mutually exclusive')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format='%(levelname)s %(message)s',
    )

    roots = tuple(DEFAULT_ROOTS) + tuple(args.root or ())
    command = args.zbx_get
    try:
        if args.dir:
            entries = list_dir(
                args.host, args.dir, timeout=args.timeout, roots=roots,
                command=command, retries=args.retries,
            )
            source = validate_dir(args.dir, roots)
            result = {
                'kind': 'dir-list',
                'host': args.host,
                'dir': source,
                'dir_masked': mask_sensitive(source),
                'count': len(entries),
                'entries': entries,
            }
        elif args.path and args.info:
            result = fetch_info(
                args.host, args.path, timeout=args.timeout, roots=roots,
                command=command, retries=args.retries,
            )
        elif args.path:
            result = fetch_file(
                args.host, args.path, args.out,
                chunk=args.chunk, timeout=args.timeout, roots=roots,
                command=command, retries=args.retries,
            )
        elif args.list:
            entries = list_zip(
                args.host, args.zip, timeout=args.timeout, roots=roots,
                command=command, retries=args.retries,
            )
            source = validate_path(args.zip, roots, kind='zip')
            result = {
                'kind': 'zip-list',
                'host': args.host,
                'zip': source,
                'zip_masked': mask_sensitive(source),
                'count': len(entries),
                'entries': entries,
            }
        else:
            result = fetch_zip_entry(
                args.host, args.zip, args.out,
                entry=args.entry, index=args.index,
                chunk=args.chunk, timeout=args.timeout, roots=roots,
                command=command, retries=args.retries,
            )
    except ImageFetchError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1

    if not args.show_sensitive:
        result = _redact_result(result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
