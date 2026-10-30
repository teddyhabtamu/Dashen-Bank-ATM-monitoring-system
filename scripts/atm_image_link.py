"""Timestamp/event matching for ATM image evidence.

The image filename is not a transaction key.  GRG proved that on 2026-10-22:
the filename said ``CarNum-8888########8888`` while the EJ said ``NOCARD``.
This module therefore ranks files by capture time and event sequence, and
never by the card field.

It is deliberately pure: the caller lists an allow-listed directory, then
passes the rows here.  No bytes are read, no file is written, and nothing is
stored.  The future bridge can use the same functions, so the ranking rule is
tested once and used everywhere.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from typing import Sequence

from fetch_atm_image import mask_sensitive

# CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg
# CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888].mp4
GRG_NAME_RE = re.compile(
    r'^CAM\[(?P<camera>\d{1,3})\]_'
    r'\[DateTime-(?P<stamp>\d{12})\]_'
    r'\[CarNum-(?P<card>[^\]]*)\]'
    r'(?P<event>[A-Za-z][A-Za-z0-9_]*)?'
    r'\.(?P<ext>jpg|jpeg|png|mp4)$',
    re.IGNORECASE,
)

# 172.23.248.4_102226_163377_16_1.zip
NCR_ZIP_RE = re.compile(
    r'^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})_'
    r'(?P<date>\d{6})_(?P<size>\d+)_(?P<count>\d+)_'
    r'(?P<camera>\d+)\.zip$',
    re.IGNORECASE,
)

# 1.102226.103353.Default-PicOnly.1.102226.103353.1.123456789012345.jpg
# ^camera ^capture date/time      ^type         ^session date/time
NCR_ENTRY_RE = re.compile(
    r'^(?P<camera>\d{1,3})\.'
    r'(?P<cap_date>\d{6})\.(?P<cap_time>\d{6})\.'
    r'(?P<image_type>[^.]+)\.(?P<seq>\d+)\.'
    r'(?P<ses_date>\d{6})\.(?P<ses_time>\d{6})\.'
    r'(?P<ses_seq>\d+)\.(?P<card>\d+)\.'
    r'(?P<ext>jpg|jpeg|png|mp4)$',
    re.IGNORECASE,
)

START_EVENTS = frozenset({'CardInserted'})
END_EVENTS = frozenset({'CardReturned'})
SERVICE_EVENTS = frozenset({'CabinetOpen'})

DEFAULT_ANCHOR_WINDOW = 3.0
DEFAULT_PRE_WINDOW = 10.0
DEFAULT_POST_WINDOW = 10.0

LINK_BASIS = 'capture_time+event_sequence'
CAVEAT = (
    'Timestamp and event sequence only; this is not a transaction ID. '
    'Two sessions in the same second remain ambiguous. Verify against the '
    'EJ interval and adjacent sessions.'
)
NCR_CAVEAT = (
    'The zip name identifies the day container, not a transaction link. '
    'Open the archive and compare the entry capture/session time with the '
    'EJ interval; the card field is never the link.'
)
NCR_ENTRY_CAVEAT = (
    'The entry name carries the ATM-local capture time and the session time. '
    'The masked card field is display-only and never used as the link. A zip '
    'can contain several sessions; verify the entry against the EJ interval.'
)


class LinkError(RuntimeError):
    """A refused or impossible match request, with a stable reason."""


def name_hmac(name: str, key: bytes) -> str:
    """Return a keyed, non-reversible locator for a card-bearing filename.

    The metadata index stores this instead of the raw name.  A plain hash is
    not enough: filenames have low entropy and can be guessed.  The key lives
    outside the database and must be at least 256 bits.
    """
    if not isinstance(name, str) or not name:
        raise LinkError('E_ARGS')
    if not isinstance(key, (bytes, bytearray)) or len(key) < 32:
        raise LinkError('E_KEY')
    return hmac.new(bytes(key), name.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def parse_grg_name(name: str) -> dict | None:
    """Parse one GRG evidence filename, or return None if it is not one.

    ``captured_at`` is naive local time, because the ATM filename carries no
    timezone.  The caller must supply session times in the same local clock.
    """
    if not isinstance(name, str):
        return None
    match = GRG_NAME_RE.match(name)
    if not match:
        return None
    try:
        captured = dt.datetime.strptime(match.group('stamp'), '%y%m%d%H%M%S')
    except ValueError:
        return None

    extension = match.group('ext').casefold()
    card = match.group('card')
    return {
        'name': name,
        'name_masked': mask_sensitive(name),
        'camera': int(match.group('camera')),
        'captured_at': captured,
        'event': match.group('event') or '',
        'kind': 'video' if extension == 'mp4' else 'image',
        'card_field': mask_sensitive(card),
        'card_field_present': bool(card),
    }


def parse_ncr_zip_name(name: str) -> dict | None:
    """Parse one NCR daily image zip name, or return None."""
    if not isinstance(name, str):
        return None
    match = NCR_ZIP_RE.match(name)
    if not match:
        return None
    try:
        day = dt.datetime.strptime(match.group('date'), '%m%d%y').date()
    except ValueError:
        return None
    return {
        'name': name,
        'name_masked': mask_sensitive(name),
        'atm_ip': match.group('ip'),
        'day': day,
        'size': int(match.group('size')),
        'count': int(match.group('count')),
        'camera': int(match.group('camera')),
    }


def parse_ncr_entry_name(name: str) -> dict | None:
    """Parse one NCR zip entry name, or return None if it is not one.

    The observed 2026-10-22 format is::

        1.102226.103353.Default-PicOnly.1.102226.103353.1.<card>.jpg
        ^cam ^date   ^time   ^type          ^seq ^date   ^time   ^seq

    The first date/time is the image capture time and the second is the
    session (transaction) time.  Both are ATM-local wall-clock time; the ZIP
    mtime is UTC and is deliberately not used here.  The card field is
    returned masked for display only and never contributes to a link.
    """
    if not isinstance(name, str):
        return None
    match = NCR_ENTRY_RE.match(name)
    if not match:
        return None
    try:
        cap_day = dt.datetime.strptime(
            match.group('cap_date'), '%m%d%y').date()
        cap_time = dt.datetime.strptime(
            match.group('cap_time'), '%H%M%S').time()
        ses_day = dt.datetime.strptime(
            match.group('ses_date'), '%m%d%y').date()
        ses_time = dt.datetime.strptime(
            match.group('ses_time'), '%H%M%S').time()
    except ValueError:
        return None

    captured = dt.datetime.combine(cap_day, cap_time)
    session = dt.datetime.combine(ses_day, ses_time)
    extension = match.group('ext').casefold()
    card = match.group('card')
    return {
        'name': name,
        'name_masked': mask_sensitive(name),
        'camera': int(match.group('camera')),
        'captured_at': captured,
        'session_at': session,
        'image_type': match.group('image_type'),
        'kind': 'video' if extension == 'mp4' else 'image',
        'card_field': mask_sensitive(card),
        'card_field_present': bool(card),
    }


def _seconds(value: float) -> float:
    return round(float(value), 3)


def _row_size(row: dict) -> int:
    value = row.get('length') or 0
    if isinstance(value, bool):
        raise LinkError('E_ROWS')
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise LinkError('E_ROWS') from None
    if size < 0:
        raise LinkError('E_ROWS')
    return size


def _parse_mtime(value) -> dt.datetime | None:
    """Parse the ATM's ISO write time as naive local wall-clock time.

    PowerShell emits ``2026-10-22T15:35:21.1063883+03:00``.  The offset is the
    ATM's local offset; the filename and EJ sessions are also ATM-local, so
    the wall-clock part is what we compare.  Fractional seconds are dropped
    for the interval calculation and kept in the raw display string.
    """
    if not isinstance(value, str) or len(value) < 19:
        return None
    text = value[:19].replace('T', ' ')
    try:
        return dt.datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def rank_grg_candidates(rows: Sequence[dict],
                        start: dt.datetime,
                        end: dt.datetime,
                        *,
                        pre_window: float = DEFAULT_PRE_WINDOW,
                        post_window: float = DEFAULT_POST_WINDOW,
                        anchor_window: float = DEFAULT_ANCHOR_WINDOW) -> dict:
    """Rank GRG files for one EJ interval.

    A start anchor (``CardInserted``) and an end anchor (``CardReturned``)
    within ``anchor_window`` seconds of the session edges make the match
    strong.  Everything else inside the interval is supporting evidence.
    The card field is shown but never used for the link.
    """
    if start.tzinfo is not None or end.tzinfo is not None:
        raise LinkError('E_TZ')
    if end < start:
        raise LinkError('E_RANGE')
    if pre_window < 0 or post_window < 0 or anchor_window < 0:
        raise LinkError('E_ARGS')

    candidates: list[dict] = []
    unparsed = 0

    for row in rows:
        if not isinstance(row, dict):
            raise LinkError('E_ROWS')
        info = parse_grg_name(row.get('name'))
        if info is None:
            unparsed += 1
            continue

        captured = info['captured_at']
        mtime_raw = str(row.get('last_write_time') or '')
        mtime = _parse_mtime(mtime_raw)
        clip_end = captured
        if info['kind'] == 'video' and mtime is not None and mtime > captured:
            clip_end = mtime

        delta_start = (captured - start).total_seconds()
        delta_end = (captured - end).total_seconds()
        if info['kind'] == 'video':
            # The filename time is the clip start; the file mtime is the end.
            in_interval = captured <= end and clip_end >= start
            in_window = (captured <= end + dt.timedelta(seconds=post_window)
                         and clip_end >= start - dt.timedelta(
                             seconds=pre_window))
        else:
            in_interval = start <= captured <= end
            in_window = (-pre_window <= delta_start
                         and delta_end <= post_window)
        if not in_window:
            continue

        event = info['event']
        service = event in SERVICE_EVENTS
        if service:
            role = 'service'
            score = 0
        elif event in START_EVENTS and abs(delta_start) <= anchor_window:
            role = 'start'
            score = 80
        elif event in END_EVENTS and abs(delta_end) <= anchor_window:
            role = 'end'
            score = 80
        elif in_interval:
            role = 'in_window'
            score = 30
        else:
            role = 'near_window'
            score = 10

        is_video = info['kind'] == 'video'
        candidates.append({
            'name': info['name'],
            'name_masked': info['name_masked'],
            'captured_at': captured.isoformat(sep=' '),
            'file_mtime': mtime_raw,
            'capture_basis': ('filename_start+file_mtime_end'
                              if is_video else 'filename_time'),
            'clip_end': (clip_end.isoformat(sep=' ')
                         if is_video else None),
            'clip_duration_s': (_seconds((clip_end - captured).total_seconds())
                                if is_video else None),
            'camera': info['camera'],
            'event': event,
            'kind': info['kind'],
            'card_field': info['card_field'],
            'card_field_present': info['card_field_present'],
            'delta_start_s': _seconds(delta_start),
            'delta_end_s': _seconds(delta_end),
            'in_interval': in_interval,
            'service': service,
            'role': role,
            'score': score,
            'size': _row_size(row),
        })

    candidates.sort(key=lambda item: (
        -item['score'], item['captured_at'], item['name']))

    start_anchors = [item for item in candidates if item['role'] == 'start']
    end_anchors = [item for item in candidates if item['role'] == 'end']
    evidence = [item for item in candidates if not item['service']]
    ambiguous = len(start_anchors) > 1 or len(end_anchors) > 1

    if start_anchors and end_anchors:
        confidence = 'medium' if ambiguous else 'high'
    elif start_anchors or end_anchors:
        confidence = 'medium'
    elif evidence:
        confidence = 'low'
    else:
        confidence = 'none'

    return {
        'vendor': 'GRG',
        'link_basis': LINK_BASIS,
        'card_field_used_for_link': False,
        'confidence': confidence,
        'ambiguous': ambiguous,
        'caveat': CAVEAT,
        'candidate_count': len(evidence),
        'service_count': len(candidates) - len(evidence),
        'unparsed_count': unparsed,
        'start_anchor_count': len(start_anchors),
        'end_anchor_count': len(end_anchors),
        'candidates': candidates,
    }


def select_ncr_zips(rows: Sequence[dict], day: dt.date) -> dict:
    """Select the NCR daily zip(s) for one local day.

    The zip name identifies only the day container.  Entries inside the zip
    are ranked separately by :func:`rank_ncr_entries` once the archive is
    opened; this function does not pretend to link an entry.
    """
    if not isinstance(day, dt.date):
        raise LinkError('E_ARGS')
    if isinstance(day, dt.datetime):
        day = day.date()

    found: list[dict] = []
    unparsed = 0
    for row in rows:
        if not isinstance(row, dict):
            raise LinkError('E_ROWS')
        info = parse_ncr_zip_name(row.get('name'))
        if info is None:
            unparsed += 1
            continue
        if info['day'] != day:
            continue
        found.append({
            'name': info['name'],
            'name_masked': info['name_masked'],
            'atm_ip': info['atm_ip'],
            'day': info['day'].isoformat(),
            'zip_bytes': info['size'],
            'image_count': info['count'],
            'camera': info['camera'],
            'length': _row_size(row),
            'last_write_time': str(row.get('last_write_time') or ''),
        })

    found.sort(key=lambda item: (
        item['last_write_time'], item['camera'], item['name']))

    return {
        'vendor': 'NCR',
        'link_basis': 'zip_date_only',
        'card_field_used_for_link': False,
        'confidence': 'low' if found else 'none',
        'ambiguous': False,
        'caveat': NCR_CAVEAT,
        'candidate_count': len(found),
        'service_count': 0,
        'unparsed_count': unparsed,
        'start_anchor_count': 0,
        'end_anchor_count': 0,
        'candidates': found,
    }


NCR_ANCHOR_WINDOW = 5.0
NCR_PRE_WINDOW = 10.0
NCR_POST_WINDOW = 10.0


def rank_ncr_entries(rows: Sequence[dict], start: dt.datetime,
                     end: dt.datetime) -> dict:
    """Rank NCR zip entries against one EJ session interval.

    The first timestamp in an entry name is the image capture time and the
    second is the session time.  Both are ATM-local.  The session timestamp
    is the strongest anchor because it should match the EJ session start;
    the capture time decides whether the frame falls inside the interval.
    The masked card field is display-only and never contributes to the score.
    """
    if not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime):
        raise LinkError('E_ARGS')
    if start.tzinfo is not None or end.tzinfo is not None:
        raise LinkError('E_TZ')
    if end < start:
        raise LinkError('E_ARGS')

    candidates: list[dict] = []
    unparsed = 0
    for row in rows:
        if not isinstance(row, dict):
            raise LinkError('E_ROWS')
        info = parse_ncr_entry_name(row.get('name'))
        if info is None:
            unparsed += 1
            continue

        captured = info['captured_at']
        session = info['session_at']
        delta_start = (session - start).total_seconds()
        delta_end = (captured - end).total_seconds()
        in_interval = start <= captured <= end
        session_match = abs(delta_start) <= NCR_ANCHOR_WINDOW
        near_window = (
            start - dt.timedelta(seconds=NCR_PRE_WINDOW)
            <= captured
            <= end + dt.timedelta(seconds=NCR_POST_WINDOW)
        )

        score = 0
        if session_match:
            score += 100
        if in_interval:
            score += 50
        elif near_window:
            score += 20
        if not session_match and abs(delta_start) <= 60.0:
            score += 10

        if session_match:
            role = 'start'
        elif in_interval:
            role = 'in_window'
        elif near_window:
            role = 'near_window'
        else:
            role = 'other'

        index = row.get('index')
        candidates.append({
            'index': index if isinstance(index, int) else None,
            'name': info['name'],
            'name_masked': info['name_masked'],
            'camera': info['camera'],
            'captured_at': captured,
            'session_at': session,
            'image_type': info['image_type'],
            'kind': info['kind'],
            'card_field': info['card_field'],
            'card_field_present': info['card_field_present'],
            'delta_start_s': _seconds(delta_start),
            'delta_end_s': _seconds(delta_end),
            'in_interval': in_interval,
            'score': score,
            'role': role,
        })

    candidates.sort(key=lambda item: (
        -item['score'],
        item['captured_at'],
        item['index'] if item['index'] is not None else 0,
    ))
    start_anchors = sum(1 for item in candidates if item['role'] == 'start')
    in_window = sum(1 for item in candidates if item['in_interval'])
    near = sum(1 for item in candidates
               if item['role'] in ('in_window', 'near_window'))
    matched = sum(1 for item in candidates if item['score'] > 0)

    if start_anchors and in_window:
        confidence = 'high'
    elif start_anchors or in_window:
        confidence = 'medium'
    elif near:
        confidence = 'low'
    else:
        confidence = 'none'

    return {
        'vendor': 'NCR',
        'link_basis': 'session_time+capture_time',
        'card_field_used_for_link': False,
        'confidence': confidence,
        'caveat': NCR_ENTRY_CAVEAT,
        'candidate_count': matched,
        'entry_count': len(candidates),
        'unparsed_count': unparsed,
        'start_anchor_count': start_anchors,
        'in_window_count': in_window,
        'candidates': candidates,
    }
