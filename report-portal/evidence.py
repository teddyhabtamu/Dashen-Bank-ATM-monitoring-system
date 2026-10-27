"""Safe access to the byte-identical EJ evidence archive.

Raw journals are pulled by the host-side EJ scripts into ``ej-logs/raw`` and
mounted read-only into the portal.  This module is the only place that turns a
database ``file_path`` into a filesystem path, so the rules are applied once:

* use the basename only;
* require the canonical ``<date>_<host>_EJDATA.log`` name;
* resolve inside the configured evidence root;
* refuse symlinks and directory escapes;
* never expose an arbitrary path supplied by a request.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

RAW_FILENAME_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}_[A-Za-z0-9][A-Za-z0-9._-]*_EJDATA\.log$'
)
DEFAULT_RAW_DIR = '/app/evidence/raw'
VIEW_MAX_BYTES = 8 * 1024 * 1024
VIEW_PAGE_SIZE = 200


def raw_dir() -> Path:
    return Path(os.environ.get('EVIDENCE_RAW_DIR', DEFAULT_RAW_DIR))


def raw_filename(file_path) -> str | None:
    """Return the canonical basename of a journal path, or None."""
    if not file_path:
        return None
    text = str(file_path).replace('\\', '/')
    if '..' in text.split('/'):
        return None
    name = text.rsplit('/', 1)[-1]
    if not RAW_FILENAME_RE.match(name):
        return None
    return name


def raw_path(file_path, base: Path | str | None = None) -> Path | None:
    """Resolve an evidence file inside the allowlisted raw directory.

    Returns None for a missing file, an invalid name, a symlink, or a path
    that escapes the configured root.
    """
    name = raw_filename(file_path)
    if not name:
        return None
    root = Path(base if base is not None else raw_dir()).resolve()
    candidate = root / name
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != root or not resolved.is_file():
        return None
    return resolved


def format_size(size: int) -> str:
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size / 1024:.1f} KB'
    return f'{size / (1024 * 1024):.1f} MB'


def raw_info(file_path, base: Path | str | None = None) -> dict:
    path = raw_path(file_path, base=base)
    if not path:
        return {'raw_available': False, 'raw_size': None, 'raw_size_label': '—'}
    try:
        size = path.stat().st_size
    except OSError:
        return {'raw_available': False, 'raw_size': None, 'raw_size_label': '—'}
    return {
        'raw_available': True,
        'raw_size': size,
        'raw_size_label': format_size(size),
    }


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_lines(path: Path | str, max_bytes: int = VIEW_MAX_BYTES) -> dict:
    """Read a raw journal for display, never a partial file.

    The viewer is a convenience over the archived evidence, not a replacement
    for downloading the bytes. Files above ``max_bytes`` are refused rather
    than truncated, because searching a silently partial log is worse than
    telling the operator to download it.
    """
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        return {
            'lines': [],
            'too_large': True,
            'binary': False,
            'size': size,
        }
    with open(path, 'rb') as fh:
        data = fh.read()
    if b'\x00' in data[:4096]:
        return {
            'lines': [],
            'too_large': False,
            'binary': True,
            'size': size,
        }
    lines = data.decode('utf-8', errors='replace').splitlines()
    return {
        'lines': [_display_line(line) for line in lines],
        'too_large': False,
        'binary': False,
        'size': size,
    }


def _display_line(line: str) -> str:
    """Make control bytes visible instead of letting them draw glyph soup.

    Raw EJ writers put STX/ETX and other control bytes between fields. They are
    valid in the evidence, but a browser renders them as replacement boxes or
    invisible layout noise. ``\\x02`` keeps the evidence readable and makes
    it obvious that the byte was present.
    """
    return ''.join(
        char if char == '\t' or char.isprintable()
        else f'\\x{ord(char):02x}'
        for char in line
    )


def paginate_lines(lines, query: str = '', page: int = 1,
                   per_page: int = VIEW_PAGE_SIZE) -> dict:
    """Return one page of raw lines, optionally filtered by substring.

    Search is deliberately literal and case-insensitive: raw EJ text is not a
    query language, and a user searching for a PAN or terminal ID must not have
    their input interpreted as a regex.
    """
    needle = (query or '').strip().casefold()
    matched = [
        (number, line)
        for number, line in enumerate(lines, start=1)
        if not needle or needle in line.casefold()
    ]
    total = len(matched)
    per_page = max(1, int(per_page or VIEW_PAGE_SIZE))
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    page = min(page, pages)
    start = (page - 1) * per_page
    return {
        'lines': [
            {'number': number, 'text': line}
            for number, line in matched[start:start + per_page]
        ],
        'total': total,
        'page': page,
        'pages': pages,
        'per_page': per_page,
    }
