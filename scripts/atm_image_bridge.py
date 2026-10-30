#!/usr/bin/env python3
"""Host-side, read-only bridge for on-demand ATM image evidence.

The portal container must never talk to an ATM and must never receive a raw
ATM path or a card-bearing filename.  This process runs on the monitoring
host, listens on a Unix socket, and exposes only:

    POST /v1/candidates          day + session window -> masked candidates
    GET  /v1/image               masked HMAC locator -> one image/video
    POST /v1/zip/list            day + zip HMAC -> masked zip entries
    GET  /v1/zip/image           zip HMAC + entry index -> one entry

All bytes are held in memory for the life of one response.  Nothing is
written to disk, Postgres, OpenSearch, or the portal.  The only durable
artifacts are the portal audit row and whatever the browser chooses to save.

Security rules:
  * only ATM ids in the host-side allow-list config are accepted;
  * the ATM vendor, roots and zip/image roots come from that config, never
    from the request;
  * the browser supplies a keyed HMAC locator, not a path or a filename;
  * the bridge re-lists the allowed ATM directory and recomputes the HMAC
    before every byte is fetched, so a stale locator cannot become a path;
  * missing evidence is 404 "Not available on ATM"; a transport failure is
    502, never disguised as missing evidence;
  * raw names are not returned, logged, or put in an HTTP header.

Run:

    python3 scripts/atm_image_bridge.py --check
    python3 scripts/atm_image_bridge.py

The systemd unit and the portal wiring are documented in
``docs/atm-image-on-demand.md``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import logging
import ntpath
import os
import re
import signal
import socket
import socketserver
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, urlparse

from atm_image_link import (
    LinkError,
    NCR_ENTRY_CAVEAT,
    name_hmac,
    parse_ncr_entry_name,
    parse_ncr_zip_name,
    rank_grg_candidates,
    rank_ncr_entries,
    select_ncr_zips,
)
from fetch_atm_image import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    MAX_ZIP_ENTRIES,
    ImageFetchError,
    _is_narrow_root,
    fetch_bytes,
    fetch_zip_entry_bytes,
    list_dir,
    list_zip,
    mask_sensitive,
    validate_dir,
)
from find_atm_image import grg_directory

LOG = logging.getLogger('atm-image-bridge')

EAT = dt.timezone(dt.timedelta(hours=3))
MAX_DAYS_BACK = 90
MAX_WINDOW = dt.timedelta(hours=24)
MAX_BODY = 8192
MAX_TOKEN = 512
DEFAULT_PREFLIGHT_TIMEOUT = 3
AGENT_PORT = 10050
HMAC_RE = re.compile(r'^[0-9a-f]{64}$')
ATM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
HOST_RE = re.compile(r'^[A-Za-z0-9._:-]{1,255}$')
ALLOWED_MIME = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.mp4': 'video/mp4',
}


class BridgeError(RuntimeError):
    """A refusal with a stable HTTP status and a path-free public message."""

    status = 400
    code = 'bad_request'
    public = 'The image request was refused.'

    def __init__(self, public: str | None = None, *, code: str | None = None,
                 status: int | None = None):
        super().__init__(public or self.public)
        if public:
            self.public = public
        if code:
            self.code = code
        if status:
            self.status = status


class NotAvailable(BridgeError):
    status = 404
    code = 'not_available'
    public = 'Not available on ATM'


class EvidenceChanged(BridgeError):
    status = 409
    code = 'changed'
    public = 'The evidence changed on the ATM. Refresh the candidate list.'


class UpstreamError(BridgeError):
    status = 502
    code = 'upstream'
    public = 'The ATM image service did not respond.'


class AgentUnreachable(BridgeError):
    status = 503
    code = 'agent_unreachable'
    public = ('The monitoring host cannot reach the ATM agent on TCP/10050. '
              'The ATM may be off or off the network.')


class Busy(BridgeError):
    status = 429
    code = 'busy'
    public = 'The ATM image service is busy. Try again in a moment.'


@dataclass(frozen=True)
class AtmConfig:
    atm_id: str
    host: str
    vendor: str
    roots: tuple[str, ...]
    image_root: str | None
    zip_root: str | None


@dataclass(frozen=True)
class BridgeConfig:
    atms: dict[str, AtmConfig]
    max_days_back: int
    timeout: int
    retries: int
    preflight_timeout: int = DEFAULT_PREFLIGHT_TIMEOUT


def _today() -> dt.date:
    return dt.datetime.now(EAT).date()


def _parse_day(value: str, max_days_back: int) -> dt.date:
    if not isinstance(value, str) or not value:
        raise BridgeError('A day is required (YYYY-MM-DD).')
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise BridgeError('The day must be YYYY-MM-DD.') from None
    today = _today()
    if day > today:
        raise BridgeError('Future evidence cannot be requested.')
    if day < today - dt.timedelta(days=max_days_back):
        raise BridgeError(
            f'Only the last {max_days_back} days can be requested.')
    return day


def _parse_local(value: str, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise BridgeError(f'{field} is required.')
    text = value.strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise BridgeError(f'{field} must be YYYY-MM-DD HH:MM:SS.')


def _window(day: dt.date, start_raw: str,
            end_raw: str) -> tuple[dt.datetime, dt.datetime]:
    start = _parse_local(start_raw, 'start')
    end = _parse_local(end_raw, 'end')
    if start.date() != day or end.date() != day:
        raise BridgeError('The session window must be on the requested day.')
    if end < start:
        raise BridgeError('The window end must not be before its start.')
    if end - start > MAX_WINDOW:
        raise BridgeError('The session window must be 24 hours or less.')
    return start, end


def _read_private_file(path: str, label: str) -> bytes:
    """Read an operator-supplied config/key file without following a symlink."""
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise RuntimeError(f'{label} must be a regular file')
    candidate = candidate.resolve(strict=True)
    try:
        # NOSONAR: resolved regular file, not a directory or symlink.
        return candidate.read_bytes()  # NOSONAR
    except OSError as exc:
        raise RuntimeError(f'cannot read the {label}: {exc}') from exc


def _load_key(path: str | None) -> bytes:
    env = os.environ.get('ATM_IMAGE_HMAC_KEY')
    if env:
        raw = env.encode('utf-8')
    elif path:
        raw = _read_private_file(path, 'HMAC key file')
    else:
        raise RuntimeError('set ATM_IMAGE_HMAC_KEY or pass --hmac-key-file')
    raw = raw.strip()
    if len(raw) < 32:
        raise RuntimeError('the image HMAC key must be at least 32 bytes')
    return raw


def _load_token() -> str:
    token = os.environ.get('ATM_IMAGE_BRIDGE_TOKEN', '').strip()
    if len(token) < 32:
        raise RuntimeError(
            'ATM_IMAGE_BRIDGE_TOKEN is missing or shorter than 32 characters')
    if (len(token) > MAX_TOKEN or any(ord(ch) < 32 for ch in token)
            or not token.isascii()):
        raise RuntimeError('ATM_IMAGE_BRIDGE_TOKEN is invalid')
    return token


def _atm_config(item: dict, atm_id: str) -> AtmConfig:
    if not isinstance(item, dict):
        raise RuntimeError(f'ATM {atm_id}: configuration must be an object')
    host = str(item.get('host') or '').strip()
    if (not host or host.startswith('-') or not HOST_RE.match(host)
            or any(ord(ch) <= 32 for ch in host)):
        raise RuntimeError(f'ATM {atm_id}: host is invalid')
    vendor = str(item.get('vendor') or '').strip().upper()
    if vendor not in ('GRG', 'NCR'):
        raise RuntimeError(f'ATM {atm_id}: vendor must be GRG or NCR')

    roots_raw = item.get('roots')
    if not isinstance(roots_raw, list) or not roots_raw:
        raise RuntimeError(f'ATM {atm_id}: roots must be a non-empty list')
    roots: list[str] = []
    for value in roots_raw:
        if not isinstance(value, str) or not _is_narrow_root(value):
            raise RuntimeError(f'ATM {atm_id}: root {value!r} is not narrow')
        roots.append(value.replace('/', '\\').rstrip('\\'))

    image_root = item.get('image_root')
    zip_root = item.get('zip_root')
    if image_root is not None:
        if not isinstance(image_root, str) or not _is_narrow_root(image_root):
            raise RuntimeError(f'ATM {atm_id}: image_root is invalid')
        image_root = image_root.replace('/', '\\').rstrip('\\')
        validate_dir(image_root, roots)
    if zip_root is not None:
        if not isinstance(zip_root, str) or not _is_narrow_root(zip_root):
            raise RuntimeError(f'ATM {atm_id}: zip_root is invalid')
        zip_root = zip_root.replace('/', '\\').rstrip('\\')
        validate_dir(zip_root, roots)

    if vendor == 'GRG' and not image_root:
        raise RuntimeError(f'ATM {atm_id}: GRG requires image_root')
    if vendor == 'NCR' and not zip_root:
        raise RuntimeError(f'ATM {atm_id}: NCR requires zip_root')

    return AtmConfig(
        atm_id=atm_id, host=host, vendor=vendor, roots=tuple(roots),
        image_root=image_root, zip_root=zip_root,
    )


def load_config(path: str | os.PathLike[str]) -> BridgeConfig:
    raw = _read_private_file(str(path), 'bridge config')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError as exc:
        raise RuntimeError(f'bridge config is not valid UTF-8: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'bridge config is not valid JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise RuntimeError('bridge config must be a JSON object')

    raw_atms = payload.get('atms')
    if not isinstance(raw_atms, dict) or not raw_atms:
        raise RuntimeError('bridge config must contain a non-empty atms object')

    atms: dict[str, AtmConfig] = {}
    for atm_id, item in raw_atms.items():
        if not isinstance(atm_id, str) or not ATM_ID_RE.match(atm_id):
            raise RuntimeError(f'atm id {atm_id!r} is invalid')
        atms[atm_id] = _atm_config(item, atm_id)

    try:
        max_days_back = int(payload.get('max_days_back', MAX_DAYS_BACK))
        timeout = int(payload.get('timeout', DEFAULT_TIMEOUT))
        retries = int(payload.get('retries', DEFAULT_RETRIES))
        preflight_timeout = int(payload.get(
            'preflight_timeout', DEFAULT_PREFLIGHT_TIMEOUT))
    except (TypeError, ValueError):
        raise RuntimeError('max_days_back, timeout and retries must be integers') \
            from None
    if not 1 <= max_days_back <= 365:
        raise RuntimeError('max_days_back must be between 1 and 365')
    if not 1 <= timeout <= 30:
        raise RuntimeError('timeout must be between 1 and 30')
    if not 0 <= retries <= 5:
        raise RuntimeError('retries must be between 0 and 5')
    if not 0 <= preflight_timeout <= 10:
        raise RuntimeError('preflight_timeout must be between 0 and 10')

    return BridgeConfig(atms=atms, max_days_back=max_days_back,
                        timeout=timeout, retries=retries,
                        preflight_timeout=preflight_timeout)


def _content_type(name: str) -> str:
    ext = ntpath.splitext(name)[1].casefold()
    try:
        return ALLOWED_MIME[ext]
    except KeyError:
        raise BridgeError('Unsupported evidence type.') from None


def _size_label(size) -> str:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return '—'
    if value < 1024:
        return f'{value} B'
    if value < 1024 * 1024:
        return f'{value / 1024:.1f} KB'
    return f'{value / (1024 * 1024):.1f} MB'


def _safe_ext(name: str) -> str:
    ext = ntpath.splitext(name)[1].casefold()
    return ext if ext in ALLOWED_MIME else '.bin'


def _hmac(value: str) -> str:
    if not isinstance(value, str) or not HMAC_RE.match(value):
        raise BridgeError('The evidence locator is invalid.')
    return value


def _project_grg(item: dict, day: dt.date, key: bytes,
                 fetchable: bool = True) -> dict:
    """Drop the raw name/path and add a keyed locator."""
    return {
        'name_masked': item['name_masked'],
        'name_hmac': name_hmac(item['name'], key),
        'day': day.isoformat(),
        'captured_at': item['captured_at'],
        'file_mtime': item['file_mtime'],
        'capture_basis': item['capture_basis'],
        'clip_end': item['clip_end'],
        'clip_duration_s': item['clip_duration_s'],
        'camera': item['camera'],
        'event': item['event'],
        'kind': item['kind'],
        'card_field': item['card_field'],
        'card_field_present': item['card_field_present'],
        'delta_start_s': item['delta_start_s'],
        'delta_end_s': item['delta_end_s'],
        'in_interval': item['in_interval'],
        'service': item['service'],
        'role': item['role'],
        'score': item['score'],
        'size': item['size'],
        'size_label': _size_label(item['size']),
        'content_type': _content_type(item['name']),
        'fetchable': bool(fetchable),
    }


def _project_ncr(item: dict, day: dt.date, key: bytes,
                 fetchable: bool = True) -> dict:
    """Drop the raw zip name and expose only the keyed locator."""
    return {
        'name_masked': item['name_masked'],
        'name_hmac': name_hmac(item['name'], key),
        'day': day.isoformat(),
        'zip_bytes': item['zip_bytes'],
        'image_count': item['image_count'],
        'camera': item['camera'],
        'length': item['length'],
        'size': item['length'],
        'size_label': _size_label(item['length']),
        'last_write_time': item['last_write_time'],
        'kind': 'zip',
        'content_type': 'application/zip',
        'fetchable': bool(fetchable),
    }


def _is_missing(exc: Exception) -> bool:
    return 'E_MISSING' in str(exc)


def _upstream(exc: Exception) -> BridgeError:
    text = str(exc)
    if 'agent timeout' in text.lower() or 'timeout' in text.lower():
        return UpstreamError('The ATM did not answer before the timeout.')
    if 'zabbix_get not found' in text.lower():
        return UpstreamError('The host image transport is not installed.')
    return UpstreamError()


class BridgeApp:
    """The transport-independent core, kept testable without a socket."""

    def __init__(self, config: BridgeConfig, key: bytes,
                 *, timeout: int | None = None, per_atm: int = 2,
                 preflight_timeout: int | None = None):
        self.config = config
        self.key = key
        self.timeout = timeout or config.timeout
        self.retries = config.retries
        self.preflight_timeout = (
            config.preflight_timeout if preflight_timeout is None
            else max(0, preflight_timeout)
        )
        self._slots = {
            atm_id: threading.BoundedSemaphore(max(1, per_atm))
            for atm_id in config.atms
        }

    def _preflight(self, atm: AtmConfig) -> None:
        """Fail fast when TCP/10050 is closed instead of retrying for 60s.

        The bridge already has no route to an ATM except this port; if the
        connect fails, every ``zabbix_get`` will fail the same way.  Keeping
        this small and boring means an offline ATM produces the same answer
        in about three seconds, while a reachable ATM pays milliseconds.
        """
        if not self.preflight_timeout:
            return
        try:
            with socket.create_connection(
                    (atm.host, AGENT_PORT), timeout=self.preflight_timeout):
                return
        except OSError:
            raise AgentUnreachable() from None

    @contextmanager
    def _guard(self, atm_id: str):
        """Do not let two admins turn one ATM into a PowerShell queue."""
        slot = self._slots[atm_id]
        if not slot.acquire(blocking=False):
            raise Busy()
        try:
            yield
        finally:
            slot.release()

    def guard(self, atm_id: str):
        if atm_id not in self._slots:
            raise BridgeError('Unknown ATM.', status=404, code='unknown_atm')
        return self._guard(atm_id)

    def atm(self, atm_id: str) -> AtmConfig:
        if not isinstance(atm_id, str) or atm_id not in self.config.atms:
            raise BridgeError('Unknown ATM.', status=404, code='unknown_atm')
        return self.config.atms[atm_id]

    def _list(self, atm: AtmConfig, directory: str) -> list[dict]:
        try:
            return list_dir(
                atm.host, directory, timeout=self.timeout, roots=atm.roots,
                retries=self.retries,
            )
        except ImageFetchError as exc:
            if _is_missing(exc):
                raise NotAvailable() from None
            raise _upstream(exc) from None

    def _list_zip(self, atm: AtmConfig, zip_path: str) -> list[dict]:
        try:
            return list_zip(
                atm.host, zip_path, timeout=self.timeout, roots=atm.roots,
                retries=self.retries,
            )
        except ImageFetchError as exc:
            if _is_missing(exc):
                raise NotAvailable() from None
            raise _upstream(exc) from None

    def _locate(self, atm: AtmConfig, directory: str,
                requested_hmac: str) -> dict:
        """Re-list the allow-listed folder and find one HMAC match."""
        wanted = _hmac(requested_hmac)
        rows = self._list(atm, directory)
        matches = [
            row for row in rows
            if hmac.compare_digest(name_hmac(row['name'], self.key), wanted)
        ]
        if not matches:
            raise NotAvailable()
        if len(matches) > 1:
            raise EvidenceChanged(
                'More than one ATM file matched the locator. Refresh the list.')
        return matches[0]

    def candidates(self, atm_id: str, day_raw: str,
                   start_raw: str, end_raw: str) -> dict:
        atm = self.atm(atm_id)
        day = _parse_day(day_raw, self.config.max_days_back)
        start, end = _window(day, start_raw, end_raw)
        self._preflight(atm)

        if atm.vendor == 'GRG':
            directory = grg_directory(day, atm.image_root)
            rows = self._list(atm, directory)
            try:
                ranked = rank_grg_candidates(rows, start, end)
            except LinkError as exc:
                raise BridgeError('The ATM listing could not be ranked.') from exc
            fetchable = {
                row['name']: row.get('fetchable', True) for row in rows
            }
            candidates = [
                _project_grg(item, day, self.key,
                             fetchable.get(item['name'], True))
                for item in ranked['candidates']
            ]
            # Keep anchors first, then still images before the slow video.
            candidates.sort(key=lambda item: (
                -item['score'], item['kind'] == 'video',
                item['captured_at'], item['name_masked']))
            result = {key: value for key, value in ranked.items()
                      if key != 'candidates'}
        else:
            directory = atm.zip_root
            rows = self._list(atm, directory)
            try:
                selected = select_ncr_zips(rows, day)
            except LinkError as exc:
                raise BridgeError('The ATM listing could not be selected.') from exc
            fetchable = {
                row['name']: row.get('fetchable', True) for row in rows
            }
            candidates = [
                _project_ncr(item, day, self.key,
                             fetchable.get(item['name'], True))
                for item in selected['candidates']
            ]
            result = {key: value for key, value in selected.items()
                      if key != 'candidates'}

        result.update({
            'kind': 'atm-image-candidates',
            'host': atm.host,
            'atm_id': atm.atm_id,
            'session_start': start.isoformat(sep=' '),
            'session_end': end.isoformat(sep=' '),
            'available': True,
            'directory_masked': mask_sensitive(directory),
            'candidates': candidates,
        })
        return result

    def image(self, atm_id: str, day_raw: str,
              requested_hmac: str) -> tuple[bytes, dict, str, str]:
        atm = self.atm(atm_id)
        if atm.vendor != 'GRG' or not atm.image_root:
            raise BridgeError('This ATM does not expose direct image files.')
        day = _parse_day(day_raw, self.config.max_days_back)
        self._preflight(atm)
        directory = grg_directory(day, atm.image_root)
        match = self._locate(atm, directory, requested_hmac)
        _content_type(match['name'])
        try:
            data, meta = fetch_bytes(
                atm.host, match['path'], timeout=self.timeout,
                roots=atm.roots, retries=self.retries,
            )
        except ImageFetchError as exc:
            if _is_missing(exc):
                raise NotAvailable() from None
            if any(code in str(exc) for code in
                   ('E_SIZE', 'E_SHA256', 'E_MD5', 'E_CONTENT', 'E_CHANGED')):
                raise EvidenceChanged() from None
            raise _upstream(exc) from None
        return (data, meta, _content_type(match['name']),
                _safe_ext(match['name']))

    def _locate_zip(self, atm: AtmConfig, day: dt.date,
                    requested_hmac: str) -> tuple[str, dict]:
        if not atm.zip_root:
            raise BridgeError('This ATM does not expose image archives.')
        match = self._locate(atm, atm.zip_root, requested_hmac)
        info = parse_ncr_zip_name(match['name'])
        if not info or info['day'] != day:
            raise NotAvailable()
        return match['path'], match

    def zip_entries(self, atm_id: str, day_raw: str,
                    zip_hmac: str, start_raw: str | None = None,
                    end_raw: str | None = None) -> dict:
        atm = self.atm(atm_id)
        day = _parse_day(day_raw, self.config.max_days_back)
        self._preflight(atm)
        zip_path, match = self._locate_zip(atm, day, zip_hmac)
        entries = self._list_zip(atm, zip_path)

        ranked = None
        by_index: dict[int, dict] = {}
        start = end = None
        if start_raw or end_raw:
            start, end = _window(day, start_raw or '', end_raw or '')
            try:
                ranked = rank_ncr_entries(entries, start, end)
            except LinkError as exc:
                raise BridgeError('The archive entries could not be ranked.') \
                    from exc
            by_index = {
                item['index']: item for item in ranked['candidates']
                if isinstance(item.get('index'), int)
            }

        projected = []
        for item in entries:
            parsed = parse_ncr_entry_name(item['name'])
            scored = by_index.get(item['index']) if ranked else None
            projected.append({
                'index': item['index'],
                'name_masked': item['name_masked'],
                'length': item['length'],
                'size_label': _size_label(item['length']),
                'compressed_length': item['compressed_length'],
                'crc32': item['crc32'],
                'last_write_time': item['last_write_time'],
                'zip_mtime_utc': item['last_write_time'],
                'captured_at': (parsed['captured_at'].strftime(
                    '%Y-%m-%d %H:%M:%S') if parsed else ''),
                'session_at': (parsed['session_at'].strftime(
                    '%Y-%m-%d %H:%M:%S') if parsed else ''),
                'camera': parsed['camera'] if parsed else None,
                'image_type': parsed['image_type'] if parsed else '',
                'kind': parsed['kind'] if parsed else 'image',
                'card_field_present': (
                    parsed['card_field_present'] if parsed else False),
                'fetchable': item['fetchable'],
                'content_type': _content_type(item['name']),
                'role': scored['role'] if scored else '',
                'score': scored['score'] if scored else 0,
                'in_interval': scored['in_interval'] if scored else False,
                'delta_start_s': scored['delta_start_s'] if scored else None,
                'delta_end_s': scored['delta_end_s'] if scored else None,
            })

        if ranked:
            projected.sort(key=lambda entry: (
                -(entry['score'] or 0),
                entry['captured_at'] or '',
                entry['index'],
            ))

        # A day zip is usually several sessions.  Grouping here keeps the
        # portal from repeating the same session timestamp on every row.
        grouped: dict[str, list[dict]] = {}
        for entry in projected:
            key = entry['session_at'] or 'Unknown'
            grouped.setdefault(key, []).append(entry)

        sessions = []
        for session_at, items in grouped.items():
            roles = [item['role'] for item in items if item['role']]
            if 'start' in roles:
                role = 'start'
            elif 'in_window' in roles:
                role = 'in_window'
            elif 'near_window' in roles:
                role = 'near_window'
            elif roles:
                role = 'other'
            else:
                role = ''
            cameras = sorted({
                item['camera'] for item in items
                if isinstance(item['camera'], int)
            })
            sessions.append({
                'session_at': session_at,
                'label': (session_at[11:19]
                          if session_at != 'Unknown' and len(session_at) >= 19
                          else session_at),
                'count': len(items),
                'cameras': cameras,
                'indices': [item['index'] for item in items],
                'role': role,
                'match_count': sum(1 for item in items if item['score'] > 0),
                'first_capture': min(
                    (item['captured_at'] for item in items
                     if item['captured_at']), default=''),
                'last_capture': max(
                    (item['captured_at'] for item in items
                     if item['captured_at']), default=''),
            })
        sessions.sort(key=lambda item: (
            -item['match_count'],
            item['session_at'] == 'Unknown',
            item['session_at'],
        ))

        result = {
            'kind': 'atm-zip-entries',
            'atm_id': atm.atm_id,
            'host': atm.host,
            'day': day.isoformat(),
            'zip_masked': mask_sensitive(match['name']),
            'zip_hmac': _hmac(zip_hmac),
            'count': len(projected),
            'entries': projected,
            'sessions': sessions,
            'caveat': NCR_ENTRY_CAVEAT,
            'available': True,
        }
        if ranked:
            result.update({
                'session_start': start.strftime('%Y-%m-%d %H:%M:%S'),
                'session_end': end.strftime('%Y-%m-%d %H:%M:%S'),
                'confidence': ranked['confidence'],
                'candidate_count': ranked['candidate_count'],
                'entry_count': ranked['entry_count'],
                'unparsed_count': ranked['unparsed_count'],
                'start_anchor_count': ranked['start_anchor_count'],
                'in_window_count': ranked['in_window_count'],
                'card_field_used_for_link': False,
            })
        return result

    def zip_image(self, atm_id: str, day_raw: str, zip_hmac: str,
                  index: int) -> tuple[bytes, dict, str, str]:
        atm = self.atm(atm_id)
        day = _parse_day(day_raw, self.config.max_days_back)
        if isinstance(index, bool) or not isinstance(index, int) \
                or index < 0 or index >= MAX_ZIP_ENTRIES:
            raise BridgeError('The archive index is invalid.')
        self._preflight(atm)
        zip_path, _match = self._locate_zip(atm, day, zip_hmac)
        entries = self._list_zip(atm, zip_path)
        if index >= len(entries):
            raise NotAvailable()
        entry = entries[index]
        if entry['index'] != index:
            raise EvidenceChanged()
        _content_type(entry['name'])
        try:
            data, meta = fetch_zip_entry_bytes(
                atm.host, zip_path, index=index,
                expected_size=int(entry['length']),
                expected_crc32=int(entry['crc32']),
                timeout=self.timeout, roots=atm.roots, retries=self.retries,
            )
        except ImageFetchError as exc:
            if _is_missing(exc):
                raise NotAvailable() from None
            if any(code in str(exc) for code in
                   ('E_SIZE', 'E_CRC32', 'E_CONTENT', 'E_CHANGED', 'E_ENTRY')):
                raise EvidenceChanged() from None
            raise _upstream(exc) from None
        return (data, meta, _content_type(entry['name']),
                _safe_ext(entry['name']))


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = 'atm-image-bridge'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    @property
    def app(self) -> BridgeApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        # Never include the query string: it carries an HMAC locator.
        LOG.info('%s %s', self.command, urlparse(self.path).path)

    def _authorized(self) -> bool:
        token = getattr(self.server, 'token', '')
        got = self.headers.get('Authorization', '')
        if not token:
            return False
        try:
            return hmac.compare_digest(got, f'Bearer {token}')
        except TypeError:
            return False

    def _send_json(self, status: int, payload: dict) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Pragma', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_error(self, exc: BridgeError) -> None:
        self._send_json(exc.status, {
            'error': exc.code,
            'message': exc.public,
        })

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
        except ValueError:
            raise BridgeError('Invalid request length.') from None
        if length <= 0 or length > MAX_BODY:
            raise BridgeError('Invalid request body.')
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError('Invalid JSON body.') from None
        if not isinstance(payload, dict):
            raise BridgeError('Invalid JSON body.')
        return payload

    def _send_evidence(self, data: bytes, meta: dict,
                       content_type: str, ext: str) -> None:
        size = len(data)
        if size != int(meta.get('size', -1)):
            self._send_error(UpstreamError())
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(size))
        self.send_header(
            'Content-Disposition', f'inline; filename="atm-evidence{ext}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Pragma', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "sandbox")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Accept-Ranges', 'none')
        self.send_header('X-Evidence-Size', str(size))
        if meta.get('sha256'):
            self.send_header('X-Evidence-SHA256', str(meta['sha256']))
        if meta.get('md5'):
            self.send_header('X-Evidence-MD5', str(meta['md5']))
        if meta.get('crc32') is not None:
            self.send_header('X-Evidence-CRC32', str(meta['crc32']))
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _dispatch(self) -> None:
        if not self._authorized():
            self._send_json(401, {
                'error': 'unauthorized',
                'message': 'The image bridge token is missing or invalid.',
            })
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == '/healthz' and self.command == 'GET':
                self._send_json(200, {
                    'ok': True,
                    'atms': len(self.app.config.atms),
                })
                return

            if parsed.path == '/v1/candidates' and self.command == 'POST':
                body = self._read_json()
                atm_id = str(body.get('atm_id') or '')
                with self.app.guard(atm_id):
                    result = self.app.candidates(
                        atm_id,
                        str(body.get('day') or ''),
                        str(body.get('start') or ''),
                        str(body.get('end') or ''),
                    )
                self._send_json(200, result)
                return

            if parsed.path == '/v1/image' and self.command == 'GET':
                atm_id = (query.get('atm_id') or [''])[0]
                with self.app.guard(atm_id):
                    data, meta, content_type, ext = self.app.image(
                        atm_id,
                        (query.get('day') or [''])[0],
                        (query.get('name_hmac') or [''])[0],
                    )
                self._send_evidence(data, meta, content_type, ext)
                return

            if parsed.path == '/v1/zip/list' and self.command == 'POST':
                body = self._read_json()
                atm_id = str(body.get('atm_id') or '')
                with self.app.guard(atm_id):
                    result = self.app.zip_entries(
                        atm_id,
                        str(body.get('day') or ''),
                        str(body.get('zip_hmac') or ''),
                        str(body.get('start') or '') or None,
                        str(body.get('end') or '') or None,
                    )
                self._send_json(200, result)
                return

            if parsed.path == '/v1/zip/image' and self.command == 'GET':
                try:
                    index = int((query.get('index') or [''])[0])
                except ValueError:
                    raise BridgeError('The archive index is invalid.') from None
                atm_id = (query.get('atm_id') or [''])[0]
                with self.app.guard(atm_id):
                    data, meta, content_type, ext = self.app.zip_image(
                        atm_id,
                        (query.get('day') or [''])[0],
                        (query.get('zip_hmac') or [''])[0],
                        index,
                    )
                self._send_evidence(data, meta, content_type, ext)
                return

            self._send_json(404, {
                'error': 'not_found',
                'message': 'No such image bridge route.',
            })
        except BridgeError as exc:
            LOG.info('refused %s: %s', parsed.path, exc.code)
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001 - never leak the exception
            LOG.warning('bridge failure on %s: %s', parsed.path,
                        mask_sensitive(str(exc))[:300])
            self._send_error(UpstreamError())

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, handler, app: BridgeApp,
                 token: str):
        self.app = app
        self.token = token
        self.socket_path = socket_path
        super().__init__(socket_path, handler)  # NOSONAR

    def server_bind(self):
        path = self.socket_path
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            current = None
        if current is not None:
            if not stat.S_ISSOCK(current.st_mode):
                raise RuntimeError(f'refusing to replace non-socket {path}')
            os.unlink(path)
        super().server_bind()
        os.chmod(path, 0o660)


def _check(config_path: str, key_path: str | None) -> int:
    config = load_config(config_path)
    _load_key(key_path)
    _load_token()
    print(json.dumps({
        'ok': True,
        'max_days_back': config.max_days_back,
        'timeout': config.timeout,
        'retries': config.retries,
        'preflight_timeout': config.preflight_timeout,
        'atms': {
            atm_id: {
                'host': atm.host,
                'vendor': atm.vendor,
                'roots': len(atm.roots),
                'image_root': bool(atm.image_root),
                'zip_root': bool(atm.zip_root),
            }
            for atm_id, atm in config.atms.items()
        },
    }, indent=2))
    return 0


def _serve(config: BridgeConfig, key: bytes, token: str,
           socket_path: str) -> int:
    socket_path = Path(socket_path).expanduser()
    if not socket_path.is_absolute():
        raise RuntimeError('the bridge socket path must be absolute')
    parent = socket_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    parent = parent.resolve(strict=True)
    socket_path = parent / socket_path.name
    if socket_path.is_symlink():
        raise RuntimeError(f'refusing to use symlinked socket {socket_path}')
    app = BridgeApp(config, key)
    server = UnixHTTPServer(str(socket_path), BridgeHandler, app, token)  # NOSONAR

    def _stop(_signum, _frame):
        # shutdown() waits for serve_forever() to finish.  Calling it from the
        # same (main) thread deadlocks, so ask a helper thread to do it.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    LOG.info('listening on %s for %d ATM(s)', socket_path, len(config.atms))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if stat.S_ISSOCK(os.lstat(socket_path).st_mode):
                os.unlink(socket_path)  # NOSONAR
        except FileNotFoundError:
            pass
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='On-demand ATM image evidence bridge (Unix socket).')
    parser.add_argument('--config', default=os.environ.get(
        'ATM_IMAGE_BRIDGE_CONFIG', '/etc/atm-monitoring/atm-image-bridge.json'),
        help='allow-list JSON (default: %(default)s)')
    parser.add_argument('--hmac-key-file', default=os.environ.get(
        'ATM_IMAGE_HMAC_KEY_FILE',
        '/etc/atm-monitoring/image-hmac.key'),
        help='key file; ATM_IMAGE_HMAC_KEY overrides it')
    parser.add_argument('--socket', default=os.environ.get(
        'ATM_IMAGE_BRIDGE_SOCKET', '/run/atm-image-bridge/bridge.sock'),
        help='Unix socket path (default: %(default)s)')
    parser.add_argument('--check', action='store_true',
                        help='validate config, key and token, then exit')
    parser.add_argument('--log-level', default='INFO',
                        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    try:
        config = load_config(args.config)
        key = _load_key(args.hmac_key_file)
        token = _load_token()
        if args.check:
            return _check(args.config, args.hmac_key_file)
        return _serve(config, key, token, args.socket)
    except Exception as exc:  # noqa: BLE001 - startup refusal
        LOG.error('bridge refused to start: %s', mask_sensitive(str(exc))[:300])
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
