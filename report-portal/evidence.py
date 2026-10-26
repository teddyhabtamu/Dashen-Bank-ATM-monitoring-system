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
