"""Admin-only, on-demand ATM image evidence viewer.

The browser never sees an ATM path.  It chooses an ATM, a local day and a
session window; the host bridge lists the allow-listed day folder and returns
masked candidates with keyed HMAC locators.  Viewing a candidate asks the
bridge for those bytes again, verifies them on the host, and streams them here
without writing a central copy.

Missing evidence is rendered as **Not available on ATM**.  A bridge or ATM
transport failure is a separate error and is never shown as missing evidence.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    make_response,
    render_template,
    request,
    stream_with_context,
)

import atm_images as bridge
from audit import log_action
from blueprints.auth import login_required, role_required, ROLE_ADMIN
from db import get_db

bp = Blueprint('atm_images', __name__)

EAT = timezone(timedelta(hours=3))
MAX_DAYS_BACK = 90
DEFAULT_WINDOW = timedelta(seconds=120)
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
HMAC_RE = re.compile(r'^[0-9a-f]{64}$')
INDEX_LIMIT = 5000
ALLOWED_MIME = frozenset({'image/jpeg', 'image/png', 'video/mp4'})


def _today() -> date:
    return datetime.now(EAT).date()


def _parse_day(raw: str | None, *, default: date | None = None) -> date:
    if not raw:
        if default is not None:
            return default
        return _today()
    if not DAY_RE.match(raw):
        raise ValueError('Day must be YYYY-MM-DD.')
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise ValueError('Day must be YYYY-MM-DD.') from None
    today = _today()
    if day > today:
        raise ValueError('Future evidence cannot be requested.')
    if day < today - timedelta(days=MAX_DAYS_BACK):
        raise ValueError(
            f'Only the last {MAX_DAYS_BACK} days can be requested.')
    return day


def _required_day(raw: str | None) -> date:
    if not raw:
        raise ValueError('A day is required.')
    return _parse_day(raw)


def _parse_at(raw: str | None) -> datetime | None:
    """Parse the EJ transaction timestamp (EAT, naive local clock)."""
    if not raw:
        return None
    text = raw.strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError('The transaction timestamp is not valid.')


def _parse_time(raw: str | None, default: time) -> time:
    if not raw:
        return default
    text = raw.strip()
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError('Time must be HH:MM or HH:MM:SS.')


def _window(day: date, at_raw: str | None, start_raw: str | None,
            end_raw: str | None) -> tuple[datetime, datetime]:
    at = _parse_at(at_raw)
    if at is not None:
        start = at - DEFAULT_WINDOW
        end = at + DEFAULT_WINDOW
        if start.date() != day:
            start = datetime.combine(day, time.min)
        if end.date() != day:
            end = datetime.combine(day, time.max)
        return start, end

    start = datetime.combine(day, _parse_time(start_raw, time.min))
    end = datetime.combine(day, _parse_time(end_raw, time.max))
    if end < start:
        raise ValueError('The window end must not be before its start.')
    if end - start > timedelta(hours=24):
        raise ValueError('The window must be 24 hours or less.')
    return start, end


def _atm_or_404(atm_id: str) -> dict:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT atm_id, branch, vendor, terminal_id, status
                  FROM atm_locations
                 WHERE atm_id = %s
            """, (atm_id,))
            row = cur.fetchone()
            cur.close()
    except Exception as exc:  # noqa: BLE001 - do not leak DB detail
        current_app.logger.warning('ATM image page lookup failed: %s', exc)
        abort(404)
    if not row:
        abort(404)
    return {
        'atm_id': row[0],
        'branch': row[1],
        'vendor': row[2],
        'terminal_id': row[3],
        'status': row[4],
    }


def _hmac_arg(raw: str | None) -> str:
    value = (raw or '').strip().lower()
    if not HMAC_RE.match(value):
        raise ValueError('The evidence locator is invalid.')
    return value


def _index_arg(raw: str | None) -> int:
    try:
        value = int(raw or '')
    except (TypeError, ValueError):
        raise ValueError('The archive index is invalid.') from None
    if value < 0 or value >= INDEX_LIMIT:
        raise ValueError('The archive index is invalid.')
    return value


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


@bp.route('/ej-journal/<atm_id>/images')
@login_required
@role_required(ROLE_ADMIN)
def atm_images(atm_id):
    atm = _atm_or_404(atm_id)
    day_raw = (request.args.get('day') or '').strip()
    at_raw = (request.args.get('at') or '').strip()
    start_raw = (request.args.get('start') or '').strip()
    end_raw = (request.args.get('end') or '').strip()
    searched = bool(day_raw or at_raw or start_raw or end_raw)

    form_error = None
    result = None
    unavailable = False
    bridge_error = None
    day = _today()
    start = datetime.combine(day, time.min)
    end = datetime.combine(day, time.max)

    try:
        at = _parse_at(at_raw)
        if at is not None:
            day = _parse_day(at.date().isoformat())
        else:
            day = _parse_day(day_raw or None)
        start, end = _window(day, at_raw, start_raw, end_raw)
    except ValueError as exc:
        form_error = str(exc)
        searched = False

    if searched and not form_error:
        try:
            result = bridge.candidates(
                atm_id, day.isoformat(),
                start.strftime('%Y-%m-%d %H:%M:%S'),
                end.strftime('%Y-%m-%d %H:%M:%S'),
            )
        except bridge.NotAvailable:
            unavailable = True
        except bridge.BridgeUnavailable as exc:
            bridge_error = str(exc)
        except bridge.BridgeError as exc:
            bridge_error = str(exc)

        confidence = (result or {}).get('confidence', 'none')
        count = len((result or {}).get('candidates') or [])
        log_action(
            'IMAGE_SEARCH',
            f'ATM {atm_id} day {day.isoformat()} '
            f'{start.strftime("%H:%M:%S")}-{end.strftime("%H:%M:%S")} '
            f'confidence {confidence} candidates {count} '
            f'unavailable {int(unavailable)} error {int(bool(bridge_error))}',
        )

    response = make_response(render_template(
        'atm_images.html',
        atm=atm,
        day=day,
        day_value=day.isoformat(),
        today_value=_today().isoformat(),
        start_value=start.strftime('%H:%M:%S'),
        end_value=end.strftime('%H:%M:%S'),
        searched=searched,
        form_error=form_error,
        result=result,
        unavailable=unavailable,
        bridge_error=bridge_error,
        bridge_configured=bridge.configured(),
        not_available_text=bridge.NOT_AVAILABLE,
        max_days_back=MAX_DAYS_BACK,
    ))
    return _no_store(response)


def _stream_evidence(atm_id: str, day: date, opener, *,
                     audit_tail: str, unavailable_tail: str):
    try:
        stream = opener()
    except bridge.NotAvailable:
        log_action('IMAGE_VIEW', f'ATM {atm_id} day {day.isoformat()} '
                                  f'{unavailable_tail} unavailable')
        return _no_store(Response(
            bridge.NOT_AVAILABLE,
            status=404,
            mimetype='text/plain',
        ))
    except bridge.BridgeError as exc:
        log_action('IMAGE_VIEW', f'ATM {atm_id} day {day.isoformat()} '
                                  f'{unavailable_tail} error {exc.code}')
        status = exc.status if exc.status in (400, 409, 429) else 502
        return _no_store(Response(
            str(exc),
            status=status,
            mimetype='text/plain',
        ))

    content_type = (stream.headers.get('Content-Type')
                    or 'application/octet-stream').split(';', 1)[0].strip()
    if content_type not in ALLOWED_MIME:
        stream.close()
        log_action('IMAGE_VIEW', f'ATM {atm_id} day {day.isoformat()} '
                                  f'{unavailable_tail} refused content type')
        return _no_store(Response(
            'The ATM returned an unsupported evidence type.',
            status=502,
            mimetype='text/plain',
        ))

    size = stream.headers.get('X-Evidence-Size') or '?'
    sha256 = stream.headers.get('X-Evidence-SHA256') or '?'
    crc32 = stream.headers.get('X-Evidence-CRC32')
    audit_hash = f'sha256 {sha256}'
    if crc32:
        audit_hash += f' crc32 {crc32}'
    log_action(
        'IMAGE_VIEW',
        f'ATM {atm_id} day {day.isoformat()} {audit_tail} '
        f'size {size} {audit_hash}',
    )

    def generate():
        try:
            yield from stream.iter_bytes()
        finally:
            stream.close()

    response = Response(
        stream_with_context(generate()),
        mimetype=content_type,
    )
    length = stream.headers.get('Content-Length')
    if length:
        response.headers['Content-Length'] = length
    response.headers['Content-Disposition'] = (
        stream.headers.get('Content-Disposition')
        or 'inline; filename="atm-evidence"'
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "sandbox"
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Accept-Ranges'] = 'none'
    if sha256 != '?':
        response.headers['X-Evidence-SHA256'] = sha256
    if size != '?':
        response.headers['X-Evidence-Size'] = size
    return _no_store(response)


@bp.route('/ej-journal/<atm_id>/images/file')
@login_required
@role_required(ROLE_ADMIN)
def atm_image_file(atm_id):
    _atm_or_404(atm_id)
    try:
        day = _required_day(request.args.get('day'))
        name_hmac = _hmac_arg(request.args.get('name_hmac'))
    except ValueError as exc:
        return _no_store(Response(str(exc), status=400, mimetype='text/plain'))

    return _stream_evidence(
        atm_id, day,
        lambda: bridge.open_image(atm_id, day.isoformat(), name_hmac),
        audit_tail=f'hmac {name_hmac[:16]}...',
        unavailable_tail=f'hmac {name_hmac[:16]}...',
    )


@bp.route('/ej-journal/<atm_id>/images/zip')
@login_required
@role_required(ROLE_ADMIN)
def atm_image_zip(atm_id):
    atm = _atm_or_404(atm_id)
    try:
        day = _required_day(request.args.get('day'))
        zip_hmac = _hmac_arg(request.args.get('zip_hmac'))
    except ValueError as exc:
        return _no_store(Response(str(exc), status=400, mimetype='text/plain'))

    result = None
    unavailable = False
    bridge_error = None
    try:
        result = bridge.zip_entries(atm_id, day.isoformat(), zip_hmac)
    except bridge.NotAvailable:
        unavailable = True
    except bridge.BridgeError as exc:
        bridge_error = str(exc)
    count = len((result or {}).get('entries') or [])
    log_action(
        'IMAGE_SEARCH',
        f'ATM {atm_id} day {day.isoformat()} zip hmac {zip_hmac[:16]}... '
        f'entries {count} unavailable {int(unavailable)} '
        f'error {int(bool(bridge_error))}',
    )
    response = make_response(render_template(
        'atm_image_zip.html',
        atm=atm,
        day=day,
        day_value=day.isoformat(),
        zip_hmac=zip_hmac,
        result=result,
        unavailable=unavailable,
        bridge_error=bridge_error,
        not_available_text=bridge.NOT_AVAILABLE,
    ))
    return _no_store(response)


@bp.route('/ej-journal/<atm_id>/images/zip/file')
@login_required
@role_required(ROLE_ADMIN)
def atm_zip_image_file(atm_id):
    _atm_or_404(atm_id)
    try:
        day = _required_day(request.args.get('day'))
        zip_hmac = _hmac_arg(request.args.get('zip_hmac'))
        index = _index_arg(request.args.get('index'))
    except ValueError as exc:
        return _no_store(Response(str(exc), status=400, mimetype='text/plain'))

    return _stream_evidence(
        atm_id, day,
        lambda: bridge.open_zip_image(
            atm_id, day.isoformat(), zip_hmac, index),
        audit_tail=f'zip hmac {zip_hmac[:16]}... index {index}',
        unavailable_tail=f'zip hmac {zip_hmac[:16]}... index {index}',
    )
