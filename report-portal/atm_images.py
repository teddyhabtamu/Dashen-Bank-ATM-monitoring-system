"""Portal-side client for the host ATM image bridge.

The portal container has no route to an ATM and no `zabbix_get`.  It talks to
a small host-side bridge over a Unix socket mounted read-only into the
container.  This module is the only place that knows the bridge protocol.

Contract:
  * the browser never supplies a path, only an ATM id, a day and (for a file)
    a keyed HMAC locator returned by the bridge;
  * image bytes are streamed from the bridge response to the browser and are
    never written to the portal filesystem, Postgres, OpenSearch or a log;
  * missing evidence raises :class:`NotAvailable`, whose public text is the
    exact operator-facing state **Not available on ATM**;
  * a bridge/transport failure raises :class:`BridgeUnavailable` or
    :class:`BridgeError`, never `NotAvailable`, so a dead puller cannot be
    disguised as an empty ATM.

Configuration (environment):
  ATM_IMAGE_BRIDGE_SOCKET   default /run/atm-image-bridge/bridge.sock
  ATM_IMAGE_BRIDGE_TOKEN    shared bearer token, at least 32 characters
  ATM_IMAGE_BRIDGE_URL      test/dev only; http://host:port instead of UDS
  ATM_IMAGE_BRIDGE_TIMEOUT  read timeout in seconds, default 180
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.parse
from typing import Iterator

DEFAULT_SOCKET = '/run/atm-image-bridge/bridge.sock'
DEFAULT_TIMEOUT = 180
MAX_ERROR_BODY = 8192
NOT_AVAILABLE = 'Not available on ATM'


class BridgeError(RuntimeError):
    """A refused or failed bridge request."""

    def __init__(self, message: str, *, code: str = 'bridge',
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class BridgeUnavailable(BridgeError):
    """The bridge is absent, unconfigured, or did not answer."""


class NotAvailable(BridgeError):
    """The ATM no longer has the requested evidence."""

    def __init__(self, message: str = NOT_AVAILABLE):
        super().__init__(message, code='not_available', status=404)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a Unix domain socket.

    ``http.client`` has no Unix-socket transport.  Overriding ``connect`` is
    enough because every other part of the request/response path is the same.
    """

    def __init__(self, socket_path: str, timeout: float = DEFAULT_TIMEOUT):
        super().__init__('localhost', timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def socket_path() -> str:
    return os.environ.get('ATM_IMAGE_BRIDGE_SOCKET', DEFAULT_SOCKET)


def _token() -> str:
    token = os.environ.get('ATM_IMAGE_BRIDGE_TOKEN', '').strip()
    if len(token) < 32:
        raise BridgeUnavailable('The ATM image bridge is not configured.')
    return token


def _timeout() -> float:
    try:
        value = float(os.environ.get('ATM_IMAGE_BRIDGE_TIMEOUT',
                                     str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT
    return max(5.0, min(value, 600.0))


def configured() -> bool:
    """True when the portal has a socket and a token to try.

    A missing socket is not a fatal configuration error: the page says the
    bridge is not running, which is different from "Not available on ATM".
    """
    if len(os.environ.get('ATM_IMAGE_BRIDGE_TOKEN', '').strip()) < 32:
        return False
    if os.environ.get('ATM_IMAGE_BRIDGE_URL'):
        return True
    return os.path.exists(socket_path())


def _connection():
    url = os.environ.get('ATM_IMAGE_BRIDGE_URL')
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'http' or not parsed.hostname:
            raise BridgeUnavailable('The ATM image bridge URL is invalid.')
        return http.client.HTTPConnection(
            parsed.hostname, parsed.port or 80, timeout=_timeout())
    path = socket_path()
    if not os.path.exists(path):
        raise BridgeUnavailable('The ATM image bridge is not running.')
    return _UnixHTTPConnection(path, timeout=_timeout())


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {_token()}',
        'Accept': 'application/json',
    }


def _body(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def _error(response, conn) -> BridgeError:
    try:
        raw = response.read(MAX_ERROR_BODY)
    except OSError:
        raw = b''
    finally:
        conn.close()
    code = 'bridge'
    message = 'The ATM image bridge refused the request.'
    try:
        payload = json.loads(raw.decode('utf-8'))
        if isinstance(payload, dict):
            code = str(payload.get('error') or code)
            message = str(payload.get('message') or message)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if response.status == 404 and code == 'not_available':
        return NotAvailable(message)
    if response.status in (401, 403):
        return BridgeUnavailable('The ATM image bridge rejected its token.')
    if response.status == 409:
        return BridgeError(message, code='changed', status=409)
    if response.status == 429:
        return BridgeError(message, code='busy', status=429)
    if response.status == 503 and code == 'agent_unreachable':
        return BridgeError(message, code='agent_unreachable', status=503)
    if response.status >= 500:
        return BridgeUnavailable(message)
    return BridgeError(message, code=code, status=response.status)


def _request_json(method: str, path: str, payload: dict | None = None) -> dict:
    conn = None
    try:
        conn = _connection()
        headers = _headers()
        body = None
        if payload is not None:
            body = _body(payload)
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise _error(response, conn)
        raw = response.read()
    except BridgeError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise BridgeUnavailable(
            'The ATM image bridge did not respond.') from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError('The ATM image bridge returned invalid JSON.') from exc
    if not isinstance(payload, dict):
        raise BridgeError('The ATM image bridge returned an invalid response.')
    return payload


def candidates(atm_id: str, day: str, start: str, end: str) -> dict:
    """Return masked candidates for one ATM/day/session window."""
    return _request_json('POST', '/v1/candidates', {
        'atm_id': atm_id,
        'day': day,
        'start': start,
        'end': end,
    })


class BridgeStream:
    """A live bridge response that the Flask route streams and then closes."""

    def __init__(self, conn, response):
        self._conn = conn
        self._response = response
        self.headers = response.headers
        self.status = response.status

    def iter_bytes(self, size: int = 65536) -> Iterator[bytes]:
        while True:
            try:
                chunk = self._response.read(size)
            except (OSError, http.client.HTTPException):
                return
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            try:
                self._conn.close()
            except OSError:
                pass


def _open(path: str) -> BridgeStream:
    conn = None
    try:
        conn = _connection()
        conn.request('GET', path, headers=_headers())
        response = conn.getresponse()
        if response.status != 200:
            raise _error(response, conn)
        return BridgeStream(conn, response)
    except BridgeError:
        if conn is not None:
            conn.close()
        raise
    except (OSError, http.client.HTTPException) as exc:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        raise BridgeUnavailable(
            'The ATM image bridge did not respond.') from exc


def open_image(atm_id: str, day: str, name_hmac: str) -> BridgeStream:
    query = urllib.parse.urlencode({
        'atm_id': atm_id,
        'day': day,
        'name_hmac': name_hmac,
    })
    return _open(f'/v1/image?{query}')


def zip_entries(atm_id: str, day: str, zip_hmac: str,
                start: str | None = None, end: str | None = None) -> dict:
    body = {
        'atm_id': atm_id,
        'day': day,
        'zip_hmac': zip_hmac,
    }
    if start and end:
        body['start'] = start
        body['end'] = end
    return _request_json('POST', '/v1/zip/list', body)


def open_zip_image(atm_id: str, day: str, zip_hmac: str,
                   index: int) -> BridgeStream:
    query = urllib.parse.urlencode({
        'atm_id': atm_id,
        'day': day,
        'zip_hmac': zip_hmac,
        'index': int(index),
    })
    return _open(f'/v1/zip/image?{query}')
