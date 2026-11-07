"""
blueprints/admin.py
ATM Admin registration routes.
"""
import csv, io, re, os, json, logging, secrets, threading, time, concurrent.futures, urllib.request
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, redirect, jsonify, flash, Response, current_app, abort, session, url_for
from werkzeug.security import generate_password_hash
from blueprints.auth import login_required, role_required, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, password_error
from db import get_db
from audit import log_action
from evidence import raw_info
from state_summary import merge_live_in_service
from helpers import STATE_ORDER, state_label, state_colour
from scheduler import REPORT_TYPE_LABELS, validate_cron
from zabbix_modules.state_freshness import (
    item_delay_seconds as _item_delay_seconds,
    source_freshness,
)
from zabbix_modules.xfs_metrics import DISPENSE_UNITS, is_xfs, normalize
from log_safety import safe_log
from net_url import service_url
from atm_key import choose_atm_id, terminal_key

logger = logging.getLogger(__name__)

# The eight NetXMS states plus the two portal-only setup states the registry
# can contain. The filter strip renders the eight always and the extras only
# when their count is non-zero.
FILTER_STATES = STATE_ORDER + ('NOT_PROVISIONED', 'INACTIVE')


def _bg_zabbix_sync(rows):
    """Queue host provisioning and run one worker pass - never fails the save.

    The old version called provision_atms() directly from a daemon thread. If
    Zabbix was down, the thread logged and vanished, leaving a saved ATM with
    no host and no retry. Now the rows go into the durable outbox first; the
    same scheduler worker that retries failures does the write. The immediate
    pass is only for latency - the outbox is the guarantee.
    """
    try:
        from provision_state import enqueue_many, run_due
        ids = [r.get('atm_id') for r in (rows or []) if (r.get('atm_id') or '').strip()]
        if not ids:
            return
        enqueue_many(ids, reason='registration-save')
        # Bound the immediate pass; the scheduler picks up the rest. An import
        # of 1,200 rows must not hold one thread for the whole fleet.
        out = run_due(limit=min(len(ids), 100))
        if out.get('error'):
            logger.warning('background provisioning read failed: %s', out['error'])
        summary = out.get('summary') or {}
        log_action('ZABBIX_SYNC', 'auto-enqueue: n={} created={} reenabled={} macros={} updated={} skipped={} no_ip={} failed={}'.format(
            len(ids), summary.get('created', 0), summary.get('reenabled', 0),
            summary.get('macros_fixed', 0), summary.get('updated', 0),
            summary.get('skipped', 0), summary.get('no_ip', 0),
            summary.get('failed', 0)))
    except Exception as e:
        logger.warning('background Zabbix sync failed: %s', e)


def _bg_zabbix_disable(atm_ids):
    """Best-effort background host disable on portal delete."""
    try:
        from zabbix_sync import disable_hosts
        s = disable_hosts(atm_ids)
        log_action('ZABBIX_DISABLE', f'auto: disabled={s["disabled"]} skipped={s["skipped"]} failed={s["failed"]}')
    except Exception as e:
        logger.warning('background Zabbix disable failed: %s', e)

EAT = timezone(timedelta(hours=3))

ENABLE_SIMULATORS = os.environ.get('ENABLE_SIMULATORS', 'false').lower() in ('1', 'true', 'yes', 'on')

def to_eat(dt, fmt='%Y-%m-%d %H:%M'):
    if not dt:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EAT).strftime(fmt)
    return str(dt)

# No url_prefix. It was '/admin' for everything in this file, which made the
# prefix describe which FILE the code lives in rather than who may reach it -
# so a viewer read the ATM registry at /admin/atm. Admin-only routes now say
# /admin explicitly; shared surfaces sit at /atms and /cash, where the path
# matches the gate. A test enforces that /admin/* is always ADMIN-gated.
bp = Blueprint('admin', __name__)

# The registry and cash surfaces used to live behind /admin because that was the
# file's url_prefix, not because they were privileged. Anything bookmarked,
# pasted into a runbook or cited in a ticket keeps working via redirect, query
# string included so saved searches and filters survive the move.
#
# 308, not 301: this prefix also fronted /admin/atm/save, /import and /delete, and
# a 301 makes the browser re-issue those as GET - so a stale form would post into a
# 405 instead of saving. 308 is permanent AND preserves the method and body.
@bp.route('/admin/atm')
@bp.route('/admin/atm/<path:rest>')
def _legacy_registry_paths(rest=''):
    target = '/atms' + (('' if rest.startswith('/') else '/') + rest if rest else '')
    if request.query_string:
        target += '?' + request.query_string.decode()
    return redirect(target, code=308)


@bp.route('/admin/cash')
def _legacy_cash_path():
    if request.query_string:
        return redirect('/cash?' + request.query_string.decode(), code=308)
    return redirect('/cash', code=308)

# Credential fields: operator+ only. Viewers keep inventory visibility
# (branch, city, IP) but must never receive shared secrets. Deliberately
# NOT masking ip_address — it's ops inventory, not a credential.
_CRED_FIELDS = ('snmp_community',)


def _strip_secrets(row):
    """Remove credential fields for viewer sessions (in place)."""
    try:
        if session.get('role') == ROLE_VIEWER:
            for f in _CRED_FIELDS:
                row.pop(f, None)
    except Exception:
        pass
    return row

ROLES = [ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN]

# ─── Simulator metrics (for the visual ATM detail page) ────────────────────────
GATEWAY_IP = os.environ.get('GATEWAY_IP', 'atm-sim-engine')
# Legacy fallback for the 7 original ATMs (before sim_port was stored in the DB).
ATM_PORTS = {
    'ATM-001': 1161, 'ATM-002': 1162, 'ATM-003': 1163,
    'ATM-004': 1164, 'ATM-005': 1165,
    'GRG-001': 1166, 'GRG-002': 1167,
}
CASSETTE_MAX = 2500  # default notes-per-cassette capacity used for fill %

_CASSETTE_CAP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'config', 'cassette_capacity.json')


def load_cassette_capacity():
    """Per-ATM notes-per-cassette capacity override (avoids a DB schema change).
    config/cassette_capacity.json maps atm_id -> int; falls back to CASSETTE_MAX."""
    try:
        with open(_CASSETTE_CAP_PATH) as f:
            data = json.load(f)
        data.pop('_comment', None)
        return data
    except Exception:
        return {}


def get_sim_port(atm_id):
    """Return the simulator port for an ATM (from atm_locations.sim_port)."""
    if not ENABLE_SIMULATORS:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT sim_port FROM atm_locations WHERE atm_id = %s", (atm_id,))
            except Exception:
                # sim_port column may not exist yet on a fresh/old DB
                return ATM_PORTS.get(atm_id)
            row = cur.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception:
        pass
    return ATM_PORTS.get(atm_id)


def assign_sim_port(atm_id, vendor):
    """Allocate a sim_port for a newly registered ATM (idempotent).

    Uses a single global sequence (MAX(sim_port)+1) shared by NCR and GRG so
    ports never collide. Legacy ATMs keep their historical ports.
    """
    if not ENABLE_SIMULATORS:
        return
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT sim_port FROM atm_locations WHERE atm_id = %s", (atm_id,))
            row = cur.fetchone()
            if row and row[0]:
                return  # already has one
            cur.execute("SELECT COALESCE(MAX(sim_port), 1160) FROM atm_locations")
            nxt = (cur.fetchone()[0] or 1160) + 1
            cur.execute("UPDATE atm_locations SET sim_port = %s WHERE atm_id = %s", (nxt, atm_id))
            conn.commit()
    except Exception as e:
        print(f"[assign_sim_port] {atm_id}: {e}")


# Map legacy simulator metric OIDs to the Zabbix item keys the detail page
# consumes. Used only to keep the (legacy, simulator-based) path keyed the same
# way as the real-Zabbix path. Best-effort for OIDs that collide across vendors.
SIM_TO_ZBX = {
    '1.1.0': 'atm.status',
    '1.2.0': 'atm.cash.cassette1', '1.3.0': 'atm.cash.cassette2',
    '1.4.0': 'atm.cash.cassette3', '1.5.0': 'atm.cash.cassette4',
    '1.6.0': 'atm.cash.rejectbin', '1.7.0': 'atm.cash.jam',
    '2.1.0': 'atm.cash.cassette1', '2.2.0': 'atm.cash.cassette2',
    '2.3.0': 'atm.cash.cassette3', '2.4.0': 'atm.cash.rejectbin',
    '2.5.0': 'atm.cash.jam',
    '3.1.0': 'atm.card.reader', '4.1.0': 'atm.printer.paper',
    '4.2.0': 'atm.card.reader', '4.3.0': 'atm.hw.pinpad',
    '4.4.0': 'atm.hw.dispenser', '4.5.0': 'atm.printer.receipt',
    '4.6.0': 'atm.printer.journal', '5.1.0': 'atm.security.cabdoor',
    '5.2.0': 'atm.security.vibration', '5.3.0': 'atm.env.temperature',
    '6.1.0': 'atm.net.status', '6.2.0': 'atm.net.latency',
    '6.3.0': 'atm.power.ups', '7.1.0': 'atm.net.link',
    '7.2.0': 'atm.power.ups', '7.3.0': 'atm.security.alarm',
    '8.1.0': 'atm.net.link', '8.2.0': 'atm.txn.total',
    '8.3.0': 'atm.txn.failed', '9.1.0': 'atm.op.uptime',
    '9.2.0': 'atm.op.downtime',
}


def _sim_to_zbx(raw):
    return {SIM_TO_ZBX.get(k, k): v for k, v in (raw or {}).items()}


def fetch_metrics(atm_id, port=None):
    """Return live metrics (keyed by Zabbix item keys) from the simulator, or None."""
    if port is None:
        port = get_sim_port(atm_id)
    if not port:
        return None
    try:
        r = requests.get(
            service_url(f"{GATEWAY_IP}:{port}", '/metrics', 'SIM_SCHEME'),
            timeout=3)
        if r.status_code == 200:
            return _sim_to_zbx(r.json())
    except Exception:
        return None
    return None


# ─── Zabbix API (real-ATM live telemetry) ────────────────────────────────────
ZABBIX_URL = os.environ.get('ZABBIX_URL') or service_url(
    'zabbix-web:8080', '/api_jsonrpc.php', 'ZBX_SCHEME')
ZABBIX_USER = os.environ.get('ZABBIX_USER', 'Admin')
ZABBIX_PASS = os.environ.get('ZABBIX_PASS', 'zabbix')

_zbx_auth = {'token': None}


def _zbx_post(payload):
    req = urllib.request.Request(
        ZABBIX_URL,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json-rpc'},
    )
    # 5s per call: LAN to zabbix-web answers in ms; a hung server must not
    # pin a Flask worker (Monitoring Center fans out to 3+ sequential
    # calls, so every second here multiplies on the page budget).
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _zbx_login():
    if _zbx_auth['token']:
        return _zbx_auth['token']
    try:
        res = _zbx_post({'jsonrpc': '2.0', 'method': 'user.login',
                         'params': {'username': ZABBIX_USER, 'password': ZABBIX_PASS}, 'id': 1})
        _zbx_auth['token'] = res.get('result')
    except Exception as e:
        print(f"[ZABBIX] login failed: {e}")
        _zbx_auth['token'] = None
    return _zbx_auth['token']


def _zbx_call(method, params, auth, _retried=False):
    from zbx_session import error_text, is_auth_error
    try:
        res = _zbx_post({'jsonrpc': '2.0', 'method': method, 'params': params,
                         'auth': auth, 'id': 1})
    except Exception as e:
        print(f"[ZABBIX] call {method} failed: {e}")
        return None

    err = res.get('error')
    if not err:
        return res.get('result')

    # An error response carries no 'result' key, so the old code returned None
    # with no log at all. A dead session token therefore looked exactly like an
    # ATM that was not reporting, and left nothing behind to diagnose - the
    # container log was empty while the page said "not reachable".
    text = error_text(err)
    print(f"[ZABBIX] {method} error {err.get('code')}: {text}")

    # The token is cached per worker and was never revalidated, so once a
    # session expired that worker returned None for every ATM until the
    # container was restarted - with three workers, that is a page that seems
    # to work about two reloads in three.
    if not _retried and is_auth_error(text):
        _zbx_auth['token'] = None
        fresh = _zbx_login()
        if fresh and fresh != auth:
            return _zbx_call(method, params, fresh, _retried=True)
    return None


def fetch_zabbix_snapshot(atm_id):
    """{'metrics', 'clocks', 'delays'} for the ATM's host, or None.

    The clock is load-bearing: without it the portal cannot tell a fresh
    reading from one Zabbix collected 25 minutes ago.  That is how the UAT
    registry kept painting a dead agent's last XFS snapshot as LIVE with
    "Hardware Faults" while the detail page correctly said unreachable.

    Cached for a few seconds (ZABBIX_METRICS_CACHE_TTL, default 10s). Telemetry
    itself only refreshes every 10-30s, so this costs no accuracy and removes
    the portal's own API stampede at fleet scale - see report-portal/zbx_cache.
    """
    from zbx_cache import get as _cached
    return _cached(f'zbx-snapshot:{atm_id}',
                   lambda k: _fetch_zabbix_snapshot(k.split(':', 1)[1]),
                   cacheable=lambda v: v is not None)


def fetch_zabbix_metrics(atm_id):
    """{item_key: lastvalue} for the ATM's Zabbix host, or None."""
    snapshot = fetch_zabbix_snapshot(atm_id)
    return snapshot['metrics'] if snapshot else None


def _fetch_zabbix_snapshot(atm_id):
    auth = _zbx_login()
    if not auth:
        return None
    try:
        hosts = _zbx_call('host.get', {'output': ['hostid'],
                                       'filter': {'host': [atm_id]}}, auth)
        if not hosts:
            return None
        hostid = hosts[0]['hostid']
        items = _zbx_call('item.get', {'hostids': [hostid],
                                       'output': ['key_', 'lastvalue',
                                                  'lastclock', 'delay'],
                                       'filter': {'status': 0}}, auth)
        if not items:
            return None
        # lastclock "0" = never collected (Zabbix still reports lastvalue "0"):
        # map to None so the detail page shows grey no-data instead of fake zeros.
        raw, clocks, delays = {}, {}, {}
        for it in items:
            key = it.get('key_')
            if not key:
                continue
            clock = int(it.get('lastclock') or 0)
            raw[key] = it.get('lastvalue') if clock else None
            clocks[key] = clock
            delays[key] = it.get('delay')
        return {'metrics': normalize(raw), 'clocks': clocks, 'delays': delays}
    except Exception as e:
        # Silence here is why the first diagnosis produced an empty log: every
        # failure path in this function returned None without saying so.
        print(f"[ZABBIX] read failed for {atm_id}: {e!r}")
        return None


def fetch_agent_status(atm_id):
    """Return whether Zabbix is still receiving data from the ATM's agent.

    This is deliberately separate from the device's own ``Network`` chip:
    that chip is XFS/WMI link state reported by the ATM, while this answers a
    different question -- did the monitoring server hear from the agent
    recently?  The two can disagree during an outage, and the page must never
    make one look like the other.
    """
    from zbx_cache import get as _cached
    return _cached(
        f'agent-status:{atm_id}',
        lambda key: _fetch_agent_status(key.split(':', 1)[1]),
        cacheable=lambda value: value is not None,
    )


STATE_MANAGER_FRESH_SECONDS = int(
    os.environ.get('STATE_MANAGER_FRESH_SECONDS', '120'))
STATE_MANAGER_FRESH_SQL = (
    "(s.updated_at > NOW() - INTERVAL '%d seconds')"
    % STATE_MANAGER_FRESH_SECONDS
)


def _state_source_freshness(snapshot, now=None):
    """(age_seconds, allowed_seconds) for the values that decide the state.

    XFS hosts are judged on their XFS master/dependents, WMI hosts on the
    atm.parts master, everything else on the SNMP core keys.  The budget comes
    from the item's own polling interval (two missed polls plus slack), so a
    5-minute WMI probe is not called dead after six minutes while a 30-second
    SNMP item is.  (None, None) when nothing relevant has a clock.
    """
    if not snapshot:
        return None, None
    return source_freshness(snapshot.get('metrics') or {},
                            snapshot.get('clocks') or {},
                            snapshot.get('delays') or {},
                            now=now)


def live_state_is_current(snapshot, now=None):
    """True only when the state's own source is within its polling budget.

    The registry and the dashboard donut must not override a persisted
    UNREACHABLE with a fault recomputed from a stale snapshot: that is the
    exact "Agent Disconnected chips + Hardware Faults rows" contradiction.
    """
    age, allowed = _state_source_freshness(snapshot, now)
    return age is not None and age <= allowed


def _age_label(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f'{seconds} s ago'
    if seconds < 5400:
        return f'{seconds // 60} min ago'
    if seconds < 172800:
        return f'{seconds // 3600} h ago'
    return f'{seconds // 86400} day{"s" if seconds >= 345600 else ""} ago'


def _fetch_agent_status(atm_id):
    auth = _zbx_login()
    if not auth:
        return None
    hosts = _zbx_call('host.get', {'output': ['hostid'],
                                   'filter': {'host': [atm_id]}}, auth)
    if not hosts:
        return {
            'state': 'unknown',
            'reason': 'This ATM is not enrolled in Zabbix, so agent '
                      'reachability cannot be checked.',
        }
    items = _zbx_call('item.get', {
        'hostids': [hosts[0]['hostid']],
        'output': ['key_', 'lastclock', 'delay', 'type'],
        'filter': {'status': 0},
    }, auth)
    if items is None:
        return None

    # Only Zabbix-agent item types prove the agent answered.  SNMP and HTTP
    # agent items can stay fresh while TCP/10050 is dead, which is exactly the
    # "Network OK / agent unreachable" disagreement this strip must expose.
    # Dependent items inherit their master's clock and have delay 0; using
    # them would guess the wrong cadence.
    readings = []
    for item in items:
        if str(item.get('type')) not in ('0', '7'):
            continue
        if str(item.get('delay', '0')) == '0':
            continue
        try:
            clock = int(item.get('lastclock') or 0)
        except (TypeError, ValueError):
            continue
        if clock > 0:
            readings.append((clock, _item_delay_seconds(item.get('delay'))))

    if not readings:
        return {
            'state': 'unknown',
            'reason': 'Zabbix has no collected agent reading for this ATM yet.',
        }

    clock, delay = max(readings, key=lambda row: row[0])
    age = max(0, int(time.time()) - clock)
    # One missed poll is normal; two plus slack is not.  The cap keeps a
    # once-an-hour item from hiding an agent that stopped answering.
    allowed = min(900, max(120, int(delay * 2.5) + 60))
    reachable = age <= allowed
    return {
        'state': 'reachable' if reachable else 'unreachable',
        'last_clock': clock,
        'last_seen': to_eat(datetime.fromtimestamp(clock, tz=timezone.utc),
                            '%d %b %H:%M'),
        'age_seconds': age,
        'age_label': _age_label(age),
        'allowed_seconds': allowed,
        'reason': ('Zabbix received agent data within the expected interval.'
                   if reachable else
                   'Zabbix has not received agent data within the expected '
                   'interval.'),
    }


def determine_live_state(vendor, metrics):
    """Derive ATM state from live Zabbix item values (mirrors state_manager.determine_state)."""
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..'))
        from zabbix_modules.status_rules import interpret_state
        return interpret_state(vendor, metrics)
    except Exception:
        # Fallback kept in sync with zabbix_modules/status_rules.py
        if not metrics:
            return None
        def m(key, default=0):
            v = metrics.get(key)
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default
        status = m('atm.status')
        net = m('atm.net.link')
        cash1 = m('atm.cash.cassette1')
        cash2 = m('atm.cash.cassette2')
        cash_jam = m('atm.cash.jam')
        card = m('atm.card.reader')
        if net == 2:
            return 'OFFLINE'
        if status == 2:
            return 'OUT_OF_SERVICE'
        if status == 4:
            return 'IN_SUPERVISOR'
        if cash1 == 0 and cash2 == 0:
            return 'OUT_OF_CASH'
        if cash_jam == 1 or card == 2:
            return 'HARDWARE_FAULT'
        if status == 1:
            return 'IN_SERVICE'
        return 'UNKNOWN'


def batch_fetch_live_states(atms):
    """Fetch live state for a list of ATM dicts in parallel.
    Returns dict: {atm_id: (state, datetime)} for reachable ATMs only.
    Uses the Zabbix API when simulators are disabled (UAT), else the sim engine.
    """
    def fetch_one(atm):
        atm_id = atm['atm_id']
        vendor = atm.get('vendor', 'NCR')
        # The state manager is the authority while it is alive. Recomputing a
        # fresh-but-raw state here bypassed its confirmation window, so a
        # single transient part error made the UI flip while the persisted
        # state was still deliberately holding. Use the live overlay only as a
        # fallback when the state manager itself has gone quiet.
        if atm.get('state_checked_recently'):
            return (atm_id, None, None)
        if ENABLE_SIMULATORS:
            port = atm.get('sim_port')
            if not port:
                return (atm_id, None, None)
            try:
                r = requests.get(
                    service_url(f"{GATEWAY_IP}:{port}", '/metrics', 'SIM_SCHEME'),
                    timeout=2)
                metrics = _sim_to_zbx(r.json()) if r.status_code == 200 else None
            except Exception:
                metrics = None
            if metrics:
                state = determine_live_state(vendor, metrics)
                if state:
                    return (atm_id, state, datetime.now(timezone.utc))
            return (atm_id, None, None)

        # Real Zabbix: the state is only "live" while the values that drive it
        # are inside their polling budget.  A stale snapshot must not overwrite
        # the persisted state with a fault recomputed from old data - that is
        # how "Agent Disconnected" rows became "Hardware Faults" in UAT.
        snapshot = fetch_zabbix_snapshot(atm_id)
        if snapshot and live_state_is_current(snapshot):
            state = determine_live_state(vendor, snapshot['metrics'])
            if state:
                return (atm_id, state, datetime.now(timezone.utc))
        return (atm_id, None, None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(fetch_one, atms))
    return {r[0]: (r[1], r[2]) for r in results if r[1] is not None}


def build_cassettes(vendor, m, capacity=CASSETTE_MAX):
    """Cash-cassette / module layout inside the ATM 'vault' with SVG geometry + fill level.
    Coordinates are expressed in the 320x540 viewBox used by the detail-page SVG."""
    # Vault interior area (matches the <rect> drawn in the template)
    VX, VY, VW, VH = 52, 364, 216, 150

    def slot(key, label, x, y, w, h):
        val = m.get(key)
        if val is None or val == "":
            return {'label': label, 'x': x, 'y': y, 'w': w, 'h': h,
                    'pct': None, 'fill_h': 0, 'fill_y': y + h, 'color': 'grey', 'notes': None}
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = 0
        pct = max(0, min(100, round(val / capacity * 100)))
        fh = round(h * pct / 100)
        color = 'green' if pct > 40 else ('amber' if pct >= 15 else 'red')
        return {'label': label, 'x': x, 'y': y, 'w': w, 'h': h,
                'pct': pct, 'fill_h': fh, 'fill_y': y + h - fh, 'color': color, 'notes': val}

    out = []
    if vendor == 'NCR':  # 2 x 2 cassette grid
        keys = ['atm.cash.cassette1', 'atm.cash.cassette2', 'atm.cash.cassette3', 'atm.cash.cassette4']
        labels = ['Cass 1', 'Cass 2', 'Cass 3', 'Cass 4']
        gap = 12
        cw = (VW - gap) / 2
        ch = (VH - gap) / 2
        for i, (k, lbl) in enumerate(zip(keys, labels)):
            r, c = divmod(i, 2)
            x = VX + c * (cw + gap)
            y = VY + r * (ch + gap)
            out.append(slot(k, lbl, x, y, cw, ch))
    elif is_xfs(m):
        gap = 10
        cw = (VW - gap) / 2
        ch = (VH - gap) / 2
        for i, unit in enumerate(DISPENSE_UNITS):
            r, c = divmod(i, 2)
            x = VX + c * (cw + gap)
            y = VY + r * (ch + gap)
            out.append(slot(f'atm.cash.cassette{i + 1}', unit, x, y, cw, ch))
    else:  # GRG SNMP — three module bays side by side
        keys = ['atm.cash.cassette1', 'atm.cash.cassette2', 'atm.cash.cassette3']
        labels = ['Module 1', 'Module 2', 'Module 3']
        gap = 14
        cw = (VW - 2 * gap) / 3
        ch = VH - 14
        for i, (k, lbl) in enumerate(zip(keys, labels)):
            x = VX + i * (cw + gap)
            y = VY + 8
            out.append(slot(k, lbl, x, y, cw, ch))
    return out


# Hardware chip / parts rendering lives in hardware_view.py (stdlib-only, unit
# tested) - the same split as onboard_rules.py: load-bearing display logic must
# be verifiable without booting Flask.
from hardware_view import (build_hardware, build_part_rows, build_device_rows,
                           snapshot_is_stale,
                           _wmi_chip, _wmi_dev, _wmi_code, _worst, _RANK,
                           PART_WORDS, DEV_WORDS)



FIELDS = [
    ('terminal_id', 'Terminal ID',  'text',   20),
    ('atm_id',      'ATM ID',       'text',   20),
    ('atm_name',    'Name',         'text',   100),
    ('branch',      'Branch',       'text',   100),
    ('district',    'District',     'text',   100),
    ('city',        'City',         'text',   100),
    ('region',      'Region',       'text',   100),
    ('latitude',    'Latitude',     'number', None),
    ('longitude',   'Longitude',    'number', None),
    ('vendor',      'Vendor',       'text',   50),
    ('model',       'Model',        'text',   50),
    ('install_date','Install Date', 'date',   None),
    ('status',      'Status',       'select', None),
    ('ip_address',  'IP Address',   'text',   45),
    ('snmp_community', 'SNMP Community', 'password', 100),
    ('snmp_version','SNMP Version', 'select', None),
    ('ej_log_format','EJ Format',   'select', None),
    ('ej_hostname', 'EJ Hostname',  'text',   64),
]


def registry_key_index(cur):
    """Return (terminal_id -> atm_id, used atm_ids, ambiguous terminal IDs).

    One query per save/import, not one per row: a 1,200-row import must not
    issue 1,200 lookups just to allocate keys.

    A terminal ID registered on more than one ATM is ambiguous. A TID-only
    import must not guess which row to update: on UAT, PATMT003 was present on
    both a 4 Kilo test ATM and the real Megenagna row, and the importer
    silently overwrote the first one by atm_id order. Ambiguous keys are
    therefore excluded from the lookup map and reported separately so the
    caller can refuse the row instead of clobbering a test asset.
    """
    cur.execute("SELECT atm_id, terminal_id FROM atm_locations ORDER BY atm_id")
    owners = {}
    used = set()
    for atm_id, terminal_id in cur.fetchall():
        if atm_id:
            used.add(atm_id)
        if terminal_id:
            owners.setdefault(terminal_key(terminal_id), []).append(atm_id)
    existing = {key: ids[0] for key, ids in owners.items() if len(ids) == 1}
    ambiguous = {key: ids for key, ids in owners.items() if len(ids) > 1}
    return existing, used, ambiguous


def assign_atm_id(data):
    """Fill data['atm_id'] when the caller did not supply one.

    The terminal ID is the bank's key; the monitoring key is derived from it.
    An existing terminal ID keeps its registered atm_id, so a TID-only
    re-import or edit updates the same row. Raises on DB failure or when the
    terminal ID is registered to more than one ATM (ambiguous identity).
    """
    current = (data.get('atm_id') or '').strip()
    if current:
        data['atm_id'] = current
        return current
    terminal_id = (data.get('terminal_id') or '').strip()
    if not terminal_id:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        existing, used, ambiguous = registry_key_index(cur)
        cur.close()
    key = terminal_key(terminal_id)
    if key in ambiguous:
        raise ValueError(
            f'Terminal ID {terminal_id} is registered to multiple ATMs '
            f'({", ".join(ambiguous[key])}). Resolve the duplicate before saving.')
    data['atm_id'] = choose_atm_id(terminal_id, used, existing)
    return data['atm_id']


@bp.route('/atms')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_list():
    search = request.args.get('search', '').strip()
    vendor = request.args.get('vendor', '').strip()
    status = request.args.get('status', '').strip()
    state_filter = request.args.get('state', '').strip().upper()
    if state_filter and state_filter not in FILTER_STATES:
        state_filter = ''
    region = request.args.get('region', '').strip()
    city = request.args.get('city', '').strip()
    per_page = 50

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1

    with get_db() as conn:
        cur = conn.cursor()

        base_clauses = []
        base_params = []
        if search:
            like = f'%{search}%'
            # The ATM team knows the terminal id (ISO 8583 field 41), not the
            # portal's atm_id. Search both, plus the human name and location.
            base_clauses.append("(l.atm_id ILIKE %s OR l.atm_name ILIKE %s "
                                "OR l.terminal_id ILIKE %s OR l.branch ILIKE %s "
                                "OR l.district ILIKE %s OR l.city ILIKE %s "
                                "OR l.region ILIKE %s)")
            base_params.extend([like, like, like, like, like, like, like])
        if vendor:
            base_clauses.append("l.vendor = %s")
            base_params.append(vendor)
        if status:
            base_clauses.append("l.status = %s")
            base_params.append(status)
        if region:
            base_clauses.append("l.region = %s")
            base_params.append(region)
        if city:
            base_clauses.append("l.city = %s")
            base_params.append(city)

        # The list is paginated, so filter on the persisted state (the system
        # of record, refreshed by state_manager every 30s) rather than fetching
        # live state for the whole fleet on every request. The visible rows
        # still get the fresh live overlay below.
        list_clauses = list(base_clauses)
        list_params = list(base_params)
        if state_filter:
            list_clauses.append("COALESCE(s.state, 'UNKNOWN') = %s")
            list_params.append(state_filter)

        from_sql = ("FROM atm_locations l "
                    "LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id")
        list_where = "WHERE " + " AND ".join(list_clauses) if list_clauses else ""
        base_where = "WHERE " + " AND ".join(base_clauses) if base_clauses else ""

        cur.execute(f"SELECT COUNT(*) {from_sql} {list_where}", list_params)
        total = cur.fetchone()[0]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        cur.execute(f"""SELECT l.*, s.state, s.last_seen AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa' as last_seen,
                               s.state_changed_at,
                               COALESCE({STATE_MANAGER_FRESH_SQL}, FALSE) AS state_checked_recently
                        {from_sql}
                        {list_where}
                        ORDER BY COALESCE(NULLIF(l.terminal_id, ''), l.atm_id), l.atm_id
                        LIMIT %s OFFSET %s""",
                    list_params + [per_page, (page - 1) * per_page])

        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

        # Counts for every state chip. Deliberately excludes the state filter
        # itself so the chips stay switchable while vendor/region/search are
        # active. Persisted state, same source as the filter above.
        cur.execute(f"""SELECT COALESCE(s.state, 'UNKNOWN') AS state, COUNT(*)
                        {from_sql}
                        {base_where}
                        GROUP BY 1""", base_params)
        raw_counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        state_all_count = sum(raw_counts.values())
        state_counts = {key: raw_counts.get(key, 0) for key in STATE_ORDER}
        state_extra = [
            {'state': key, 'label': state_label(key),
             'colour': state_colour(key), 'count': raw_counts[key]}
            for key in ('NOT_PROVISIONED', 'INACTIVE')
            if raw_counts.get(key)
        ]

        # Fetch distinct filter options from the DB for the dropdowns
        cur.execute("SELECT DISTINCT region FROM atm_locations WHERE region IS NOT NULL ORDER BY region")
        region_options = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT city FROM atm_locations WHERE city IS NOT NULL ORDER BY city")
        city_options = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT vendor FROM atm_locations WHERE vendor IS NOT NULL AND vendor <> '' ORDER BY vendor")
        db_vendors = [r[0] for r in cur.fetchall()]
        cur.close()

    # Known Dashen vendors first, then any others already in the DB.
    vendor_options = list(dict.fromkeys(['NCR', 'GRG'] + db_vendors))

    atms = [dict(zip(cols, r)) for r in rows]

    # Override state/last_seen with live sim-engine data (parallel fetch)
    live_states = batch_fetch_live_states(atms)
    for atm in atms:
        atm['stored_state'] = atm.get('state')
        if atm.get('state_checked_recently'):
            # Debounced state from the state manager; do not override it with
            # an unconfirmed raw recomputation. The badge is stable on purpose.
            atm['live'] = False
            continue
        ls = live_states.get(atm['atm_id'])
        if ls:
            atm['state'] = ls[0]
            atm['last_seen'] = ls[1]
            atm['live'] = True
        else:
            atm['live'] = False

    # Summary stat row
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT
                (SELECT COUNT(*) FROM atm_locations) as registered,
                (SELECT COUNT(*) FROM atm_current_state s
                    JOIN atm_locations l ON l.atm_id = s.atm_id
                    WHERE s.state = 'IN_SERVICE' AND l.status = 'active') as in_service,
                (SELECT COUNT(*) FROM atm_locations WHERE vendor = 'NCR') as ncr,
                (SELECT COUNT(*) FROM atm_locations WHERE vendor = 'GRG') as grg""")
            s = cur.fetchone()
            cur.close()
        summary = {'registered': int(s[0] or 0), 'in_service': int(s[1] or 0),
                   'ncr': int(s[2] or 0), 'grg': int(s[3] or 0)}
    except Exception:
        summary = {'registered': total, 'in_service': 0, 'ncr': 0, 'grg': 0}

    summary['in_service'] = merge_live_in_service(summary['in_service'], atms)

    return render_template('admin_atm.html', atms=atms, atm_count=total,
                           page=page, total_pages=total_pages,
                           search=search, fields=FIELDS, summary=summary,
                           f_vendor=vendor, f_status=status, f_region=region, f_city=city,
                           f_state=state_filter,
                           state_counts=state_counts, state_all_count=state_all_count,
                           state_extra=state_extra,
                           filter_options={'regions': region_options, 'cities': city_options},
                           vendor_options=vendor_options)


def _telemetry_note(metrics):
    """Why a live panel is empty, in words that are true.

    'ATM not reachable' was a claim the portal cannot support. When the Zabbix
    read fails, the portal knows one thing: it could not read Zabbix. The ATM
    may be reporting perfectly - a direct `zabbix_get` to the agent answered
    while the page said the machine was unreachable. Saying so is the same rule
    hardware_view.py already enforces for chips: never red without a value.
    """
    if metrics is None:
        return ('No live telemetry: the portal could not read Zabbix. This is a '
                'portal-side read failure, not evidence that the ATM is down.')
    return 'Zabbix has no collected reading for this component yet.'


def _fmt_epoch(value):
    """Zabbix epoch → EAT label, matching the rest of the portal."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(
            timezone(timedelta(hours=3))).strftime('%d %b %H:%M')
    except (TypeError, ValueError):
        return '—'


def _atm_hostid(atm_id, auth):
    """Zabbix hostid for an ATM, or None when it was never provisioned."""
    rows = _zbx_call('host.get', {'output': ['hostid'],
                                  'filter': {'host': [atm_id]}}, auth)
    return str(rows[0]['hostid']) if rows else None


def _atm_maintenance(atm_id):
    """(hostid, active_window, error) for the ATM detail page.

    Never raises: the page must render when Zabbix is down. A failed read is
    returned as an error, never as an empty window - "not in maintenance" is
    exactly the reading an operator would act on.
    """
    try:
        auth = _zbx_login()
        if not auth:
            return None, None, 'Cannot reach the Zabbix API.'
        hostid = _atm_hostid(atm_id, auth)
        if not hostid:
            return None, None, 'No Zabbix host is registered for this ATM.'
        rows = _zbx_call('maintenance.get', {
            'hostids': [hostid], 'output': 'extend',
            'selectHosts': ['hostid']}, auth)
        if rows is None:
            return hostid, None, 'Maintenance state is unavailable right now.'
        from maintenance import active_window
        active = active_window(rows, hostid, time.time())
        if active:
            active = dict(active)
            active['since_label'] = _fmt_epoch(active.get('active_since'))
            active['until_label'] = _fmt_epoch(active.get('active_till'))
        return hostid, active, None
    except Exception as e:
        logger.warning('maintenance lookup failed for %s: %s',
                       safe_log(atm_id), safe_log(e))
        return None, None, 'Maintenance state is unavailable right now.'


@bp.route('/atms/<atm_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_detail(atm_id):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT l.*,
                    COALESCE(s.state, 'UNKNOWN') as current_state,
                    s.state_changed_at,
                    s.last_seen AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa' as last_seen,
                    COALESCE({STATE_MANAGER_FRESH_SQL}, FALSE) AS state_checked_recently
                FROM atm_locations l
                LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id
                WHERE l.atm_id = %s
            """, (atm_id,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            cur.close()
        if not row:
            abort(404)
        atm = _strip_secrets(dict(zip(cols, row)))
        vendor = atm.get('vendor')

        metrics = fetch_metrics(atm_id, port=atm.get('sim_port')) if ENABLE_SIMULATORS \
            else fetch_zabbix_metrics(atm_id)
        if ENABLE_SIMULATORS:
            agent_status = {
                'state': 'unknown',
                'reason': 'Simulator mode: Zabbix agent reachability is not '
                          'checked for this host.',
            }
        else:
            try:
                agent_status = fetch_agent_status(atm_id)
            except Exception as exc:  # noqa: BLE001 - telemetry card is optional
                logger.warning('agent status read failed for %s: %s', safe_log(atm_id), safe_log(exc))  # NOSONAR: safe_log strips CR/LF
                agent_status = None
        cap_map = load_cassette_capacity()
        cassette_cap = cap_map.get(atm_id, CASSETTE_MAX)
        cassettes = build_cassettes(vendor, metrics, cassette_cap) if metrics else []
        hw, hw_source = build_hardware(vendor, metrics) if metrics else ([], None)
        part_rows = build_part_rows(metrics) if metrics else []
        device_rows = build_device_rows(metrics) if metrics else []
        # Agent-sourced hardware (XFS/WMI) is only "live" while the agent is
        # answering; otherwise these are the last values Zabbix received.  Do
        # not override the recorded state or paint a LIVE badge with them --
        # SNMP core items can still be fresh, which is why the two panels used
        # to disagree.
        data_stale = snapshot_is_stale(hw_source, agent_status)

        # The state manager owns the badge while it is alive: its confirmation
        # window is what stops a single transient part error from flipping the
        # UI. Recompute from raw telemetry only as a fallback when the state
        # manager itself has gone quiet.
        if (metrics and not data_stale
                and not atm.get('state_checked_recently')):
            live_state = determine_live_state(vendor, metrics)
            if live_state:
                atm['current_state'] = live_state
                atm['live'] = True
        else:
            atm['live'] = False
        atm['data_stale'] = data_stale

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*),
                    ROUND(100.0 * SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0), 1),
                    COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL'
                        THEN amount ELSE 0 END), 0)
                FROM atm_transactions_visible
                WHERE atm_id = %s AND recorded_at >= NOW() - INTERVAL '24 hours'
            """, (atm_id,))
            q = cur.fetchone()
            total_txns = int(q[0] or 0)
            success_rate = float(q[1] or 0)
            cash_today = float(q[2] or 0)
            cur.execute("""
                SELECT COUNT(*) FROM atm_transactions_visible
                WHERE atm_id = %s AND status = 'ERROR'
                AND recorded_at >= NOW() - INTERVAL '24 hours'
            """, (atm_id,))
            active_faults = int(cur.fetchone()[0] or 0)
            cur.execute("""
                SELECT
                    TO_CHAR(t.recorded_at AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa', 'HH24:MI:SS'),
                    t.txn_type, t.card_masked, t.amount, t.status, t.auth_code,
                    COALESCE(f.display_name, t.error_code) AS error_label
                FROM atm_transactions_visible t
                LEFT JOIN atm_locations l ON t.atm_id = l.atm_id
                LEFT JOIN fault_type_map f ON t.error_code = f.raw_fault_code AND f.vendor = l.vendor
                WHERE t.atm_id = %s
                ORDER BY t.recorded_at DESC
                LIMIT 20
            """, (atm_id,))
            txns = cur.fetchall()

            ej_runs = []
            ej_evidence_note = ''
            if session.get('role') == ROLE_ADMIN:
                try:
                    cur.execute("""
                        SELECT id, file_path, journal_date, parsed_at, sessions,
                               status, error
                          FROM ej_parse_runs
                         WHERE atm_id = %s
                         ORDER BY journal_date DESC NULLS LAST, parsed_at DESC
                         LIMIT 5
                    """, (atm_id,))
                    for r in cur.fetchall():
                        info = raw_info(r[1])
                        ej_runs.append({
                            'id': r[0],
                            'file_label': os.path.basename(
                                str(r[1]).replace('\\', '/')),
                            'journal_date': r[2].isoformat() if r[2] else '—',
                            'parsed_at': to_eat(r[3], '%Y-%m-%d %H:%M'),
                            'sessions': r[4] or 0,
                            'status': r[5] or '—',
                            'error': r[6] or '',
                            **info,
                        })
                except Exception as exc:  # noqa: BLE001 - EJ staging is optional here
                    logger.warning('ATM detail EJ evidence unavailable: %s',
                                   safe_log(exc))
                    ej_evidence_note = (
                        'EJ evidence is unavailable until the staging schema is applied.'
                    )
            cur.close()

        # BRD 5.3 depletion forecast. Best-effort: the page must render even
        # if the forecast cannot be computed - the card then says why.
        cash_outlook = None
        try:
            from cash_service import forecast_atm
            cash_outlook = forecast_atm(atm_id)
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning('cash forecast unavailable: %s', e)

        from maintenance import options as maintenance_options
        maintenance_hostid, maintenance_active, maintenance_error = _atm_maintenance(atm_id)
        maintenance_opts = maintenance_options(session.get('role') == ROLE_ADMIN)
        can_maintain = session.get('role') in (ROLE_OPERATOR, ROLE_ADMIN)

        return render_template('admin_atm_detail.html',
                               atm=atm, vendor=vendor, cassettes=cassettes,
                               hardware=hw, hw_source=hw_source,
                               part_rows=part_rows, device_rows=device_rows,
                               metrics_online=metrics is not None,
                               telemetry_note=_telemetry_note(metrics),
                               agent_status=agent_status,
                               total_txns=total_txns, success_rate=success_rate,
                               cash_today=cash_today, active_faults=active_faults,
                               txns=txns, cash_outlook=cash_outlook,
                               maintenance_hostid=maintenance_hostid,
                               maintenance_active=maintenance_active,
                               maintenance_error=maintenance_error,
                               maintenance_options=maintenance_opts,
                               can_maintain=can_maintain,
                               ej_runs=ej_runs,
                               ej_evidence_note=ej_evidence_note,
                               can_view_ej=session.get('role') == ROLE_ADMIN)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('ATM detail error: %s',
                                          safe_log(e), exc_info=True)
        return render_template('error_500.html'), 500


@bp.route('/atms/<atm_id>/maintenance/start', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_maintenance_start(atm_id):
    """One-host service window: data keeps flowing, alerts are hidden."""
    from maintenance import active_window, create_params, window
    back = redirect(url_for('admin.atm_detail', atm_id=atm_id))
    reason = (request.form.get('reason') or '').strip()
    duration = request.form.get('duration') or ''
    if len(reason) < 5:
        flash('Give a reason of at least 5 characters.', 'error')
        return back
    is_admin = session.get('role') == ROLE_ADMIN
    try:
        window(duration, time.time(), admin=is_admin)
    except ValueError as e:
        flash(str(e), 'error')
        return back

    auth = _zbx_login()
    if not auth:
        flash('Cannot reach the Zabbix API — no service window was started.', 'error')
        return back
    hostid = _atm_hostid(atm_id, auth)
    if not hostid:
        flash('No Zabbix host is registered for this ATM.', 'error')
        return back

    # Fail closed: without a readable current state we cannot rule out an
    # overlapping window, and duplicates are worse than a retry.
    existing = _zbx_call('maintenance.get', {
        'hostids': [hostid], 'output': 'extend',
        'selectHosts': ['hostid']}, auth)
    if existing is None:
        flash('Cannot read the current maintenance state — no service window was started.',
              'error')
        return back
    if active_window(existing, hostid, time.time()):
        flash('This ATM is already in a service window. End it first.', 'error')
        return back

    try:
        params = create_params(hostid, atm_id, reason, duration, time.time(),
                               admin=is_admin, actor=session.get('username'))
    except ValueError as e:
        flash(str(e), 'error')
        return back

    result = _zbx_call('maintenance.create', params, auth)
    if not result:
        log_action('MAINTENANCE_START_FAILED',
                   'atm=%s hostid=%s — %s' % (atm_id, hostid, reason))
        flash('Zabbix did not accept the service window.', 'error')
        return back
    mid = (result.get('maintenanceids') or [None])[0] \
        if isinstance(result, dict) else None
    log_action('MAINTENANCE_START',
               'atm=%s hostid=%s maintenanceid=%s until=%s — %s'
               % (atm_id, hostid, mid or '?', _fmt_epoch(params['active_till']), reason))
    flash('Service mode started until %s EAT. Problems for this ATM are hidden; '
          'data collection continues.' % _fmt_epoch(params['active_till']), 'success')
    # 303 is the correct POST/Redirect/GET status; the error paths keep the
    # default 302.  The distinct return value also makes the success/error
    # split explicit to readers and static analysis.
    return back, 303


@bp.route('/atms/<atm_id>/maintenance/end', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_maintenance_end(atm_id):
    """End this ATM's active window — never an id supplied by the client."""
    from maintenance import active_window
    back = redirect(url_for('admin.atm_detail', atm_id=atm_id))
    auth = _zbx_login()
    if not auth:
        flash('Cannot reach the Zabbix API — the service window was left unchanged.', 'error')
        return back
    hostid = _atm_hostid(atm_id, auth)
    if not hostid:
        flash('No Zabbix host is registered for this ATM.', 'error')
        return back
    rows = _zbx_call('maintenance.get', {
        'hostids': [hostid], 'output': 'extend',
        'selectHosts': ['hostid']}, auth)
    if rows is None:
        flash('Cannot read the current maintenance state — nothing was changed.', 'error')
        return back
    active = active_window(rows, hostid, time.time())
    if not active:
        flash('This ATM has no active service window.', 'error')
        return back
    mid = str(active.get('maintenanceid'))
    result = _zbx_call('maintenance.delete', [mid], auth)
    if not result:
        log_action('MAINTENANCE_END_FAILED',
                   'atm=%s hostid=%s maintenanceid=%s' % (atm_id, hostid, mid))
        flash('Zabbix did not end the service window.', 'error')
        return back
    log_action('MAINTENANCE_END',
               'atm=%s hostid=%s maintenanceid=%s' % (atm_id, hostid, mid))
    flash('Service mode ended for %s.' % atm_id, 'success')
    # 303 is the correct POST/Redirect/GET status; the error paths keep the
    # default 302.  The distinct return value also makes the success/error
    # split explicit to readers and static analysis.
    return back, 303


@bp.route('/atms/get')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_get():
    atm_id = request.args.get('atm_id', '').strip()
    if not atm_id:
        return jsonify({'error': True, 'message': 'ATM ID is required'}), 400
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (atm_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        cur.close()
    if not row:
        return jsonify({'error': True, 'message': f'ATM {atm_id} not found'}), 404
    return jsonify(_strip_secrets(dict(zip(cols, row))))


def _audit_val(v):
    if v is None or v == '':
        return '(empty)'
    return str(v)


def _norm_branch(v):
    """Owner-branch convention: single spaces, one trailing ' BRANCH'.
    Matches the ATM team's own template ('MAKIRA BRANCH', 'WOLDIA BRANCH').
    Idempotent — already-clean names pass through byte-identical."""
    if v is None:
        return None
    v = re.sub(r'\s+', ' ', str(v)).strip()
    if not v:
        return None
    if v.upper().endswith('BRANCH'):
        v = v[:-len('BRANCH')].rstrip() + ' BRANCH'
    else:
        v = v + ' BRANCH'
    return v


def _row_diff(old, new, skip=()):
    """Changed fields as ['field: old => new']. Secrets never leak:
    snmp_community shows only that it changed."""
    diffs = []
    for k in new:
        if k in skip or k not in old:
            continue
        o, n = _audit_val(old.get(k)), _audit_val(new.get(k))
        if o != n:
            if k == 'snmp_community':
                diffs.append('snmp_community: **** => **** (rotated)')
            else:
                diffs.append(f'{k}: {o} => {n}')
    for k in new:
        if k not in old and k not in skip and _audit_val(new.get(k)) != '(empty)':
            diffs.append(f'{k}: (new) {_audit_val(new.get(k))}')
    return diffs


def normalise_atm_form(raw):
    """Form dict -> canonical ATM facts. One set of rules for every entry
    point, so the Registration page and the onboarding wizard cannot disagree
    about defaults, blank handling, or the branch convention."""
    data = {}
    for f in FIELDS:
        v = (raw.get(f[0]) or '').strip()
        data[f[0]] = v if v else None
    # Name is optional in the form; default it so the Name column is never blank.
    if not data.get('atm_name'):
        data['atm_name'] = data.get('branch') or data.get('atm_id')
    # Owner-branch convention (validated + stored in canonical form).
    data['branch'] = _norm_branch(data.get('branch'))
    return data


def save_atm_record(data, source='registration'):
    """THE single DB writer for ATM facts.

    Validation, upsert, state seeding, sim-port assignment and the audit line
    all live here. Both /atms/save and the onboarding wizard call it, so a
    second writer cannot drift away from the first — which is how ATM records
    ended up half-saved in the past.

    Returns {'ok', 'errors', 'audit_detail', 'atm_id', 'created'}. Never
    raises: a database failure comes back as errors.
    """
    if not (data.get('atm_id') or '').strip():
        try:
            assign_atm_id(data)
        except Exception as e:
            logger.warning('ATM ID allocation failed: %s', e)
            message = (str(e) if isinstance(e, ValueError)
                       else 'Could not generate an ATM ID. Check the database and try again.')
            return {'ok': False,
                    'errors': {'_general': message},
                    'audit_detail': '', 'atm_id': None, 'created': False}
    errors = _validate(data)
    if errors:
        return {'ok': False, 'errors': errors, 'audit_detail': '',
                'atm_id': data.get('atm_id'), 'created': False}

    created = False
    try:
        with get_db() as conn:
            cur = conn.cursor()
            try:
                # Before-image for the audit trail (None = create, not update).
                cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (data['atm_id'],))
                _cols = [d[0] for d in cur.description]
                _fetched = cur.fetchone()
                old_row = dict(zip(_cols, _fetched)) if _fetched else None
                created = old_row is None
                cur.execute("""
                    INSERT INTO atm_locations
                        (atm_id, atm_name, branch, district, city, region, latitude, longitude,
                         terminal_id, vendor, model, install_date, status,
                         ip_address, snmp_community, snmp_version, ej_log_format, ej_hostname)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (atm_id) DO UPDATE SET
                        atm_name     = EXCLUDED.atm_name,
                        branch       = EXCLUDED.branch,
                        district     = EXCLUDED.district,
                        city         = EXCLUDED.city,
                        region       = EXCLUDED.region,
                        latitude     = EXCLUDED.latitude,
                        longitude    = EXCLUDED.longitude,
                        terminal_id  = EXCLUDED.terminal_id,
                        vendor       = EXCLUDED.vendor,
                        model        = EXCLUDED.model,
                        install_date = EXCLUDED.install_date,
                        status       = EXCLUDED.status,
                        -- Network fields: only overwrite when the form supplied a
                        -- value, so editing branch/city never wipes SNMP/IP data.
                        ip_address   = COALESCE(EXCLUDED.ip_address, atm_locations.ip_address),
                        snmp_community = COALESCE(EXCLUDED.snmp_community, atm_locations.snmp_community),
                        snmp_version = COALESCE(EXCLUDED.snmp_version, atm_locations.snmp_version),
                        ej_log_format = COALESCE(EXCLUDED.ej_log_format, atm_locations.ej_log_format),
                        -- Same COALESCE rule as the other network fields: a blank
                        -- form must not wipe a hostname the pull depends on.
                        ej_hostname   = COALESCE(EXCLUDED.ej_hostname, atm_locations.ej_hostname)
                """, (
                    data['atm_id'], data['atm_name'], data['branch'], data['district'], data['city'],
                    data['region'], data['latitude'], data['longitude'],
                    data['terminal_id'], data['vendor'], data['model'],
                    data['install_date'], data['status'],
                    data.get('ip_address'), data.get('snmp_community'),
                    data.get('snmp_version'), data.get('ej_log_format'),
                    data.get('ej_hostname')
                ))
                # Seed initial state; inactive ATMs get a dedicated state
                if data['status'] == 'inactive':
                    cur.execute("""
                        INSERT INTO atm_current_state (atm_id, state, last_seen, updated_at)
                        VALUES (%s, 'INACTIVE', NOW(), NOW())
                        ON CONFLICT (atm_id) DO UPDATE SET state = 'INACTIVE', updated_at = NOW()
                    """, (data['atm_id'],))
                else:
                    cur.execute("""
                        INSERT INTO atm_current_state (atm_id, state, last_seen, updated_at)
                        VALUES (%s, 'UNKNOWN', NOW(), NOW())
                        ON CONFLICT (atm_id) DO UPDATE
                            SET state = CASE WHEN atm_current_state.state = 'INACTIVE' THEN 'UNKNOWN' ELSE atm_current_state.state END,
                                updated_at = NOW()
                    """, (data['atm_id'],))
                conn.commit()
                # After-image: re-read the effective row (COALESCE logic means
                # the stored row can differ from what was submitted) and diff.
                cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (data['atm_id'],))
                _ncols = [d[0] for d in cur.description]
                _nrow = cur.fetchone()
                new_row = dict(zip(_ncols, _nrow)) if _nrow else {}
                if old_row is None:
                    audit_detail = (f'ATM {data["atm_id"]} created: ' +
                                    ', '.join(f'{k}={_audit_val(new_row.get(k, v))}'
                                              for k, v in data.items()
                                              if k != 'snmp_community' and v) +
                                    ('; snmp_community set' if data.get('snmp_community') else ''))
                else:
                    diffs = _row_diff(old_row, new_row, skip=('sim_port',))
                    audit_detail = (f'ATM {data["atm_id"]} updated: ' + '; '.join(diffs)
                                    if diffs else f'ATM {data["atm_id"]} saved (no changes)')
                # Auto-allocate a simulator port so the new ATM behaves like the others
                assign_sim_port(data['atm_id'], data['vendor'])
            except Exception as e:
                conn.rollback()
                err_msg = str(e)
                if 'duplicate key' in err_msg.lower() or 'unique' in err_msg.lower():
                    db_err = {'atm_id': 'This ATM ID already exists'}
                else:
                    db_err = {'_general': f'Database error: {err_msg}'}
                return {'ok': False, 'errors': db_err, 'audit_detail': '',
                        'atm_id': data.get('atm_id'), 'created': False}
            finally:
                cur.close()
    except Exception as e:
        return {'ok': False, 'errors': {'_general': f'Database error: {e}'},
                'audit_detail': '', 'atm_id': data.get('atm_id'), 'created': False}

    return {'ok': True, 'errors': {}, 'audit_detail': audit_detail,
            'atm_id': data['atm_id'], 'created': created, 'source': source}


@bp.route('/atms/save', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_save():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    data = normalise_atm_form(request.form)
    res = save_atm_record(data, source='registration')
    if not res['ok']:
        if is_ajax:
            return jsonify({'error': True, 'errors': res['errors'],
                            'message': 'Please fix the highlighted fields.'})
        if '_general' in res['errors']:
            flash(res['errors']['_general'], 'error')
        else:
            flash('Validation failed. Please correct the highlighted fields.', 'error')
        return redirect('/atms')

    log_action('ATM_SAVE', res['audit_detail'])
    threading.Thread(target=_bg_zabbix_sync, args=([dict(data)],), daemon=True).start()
    if is_ajax:
        return jsonify({'success': True, 'atm_id': data['atm_id']})
    return _guide_page(data)


def _archive_atm(cur, atm_id, deleted_by):
    """Snapshot one ATM into atm_delete_archive (same transaction — rolls
    back if the delete fails). Captures the full location row, all cash
    loads (financial evidence otherwise lost to ON DELETE CASCADE), and
    volume counts. Never raises: archival must not block a delete, but a
    failure is logged loudly. Returns (archived_bool, location_dict)."""
    try:
        cur.execute("CREATE TABLE IF NOT EXISTS atm_delete_archive ("
                    "id SERIAL PRIMARY KEY, atm_id VARCHAR(20) NOT NULL,"
                    " deleted_at TIMESTAMP DEFAULT NOW(), deleted_by VARCHAR(100) DEFAULT '',"
                    " location JSONB, cash_loads JSONB,"
                    " txn_count INT DEFAULT 0, anomaly_count INT DEFAULT 0,"
                    " state_history_count INT DEFAULT 0)")
        cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (atm_id,))
        cols = [d[0] for d in cur.description]
        loc = cur.fetchone()
        if not loc:
            return False, None
        location = dict(zip(cols, loc))
        cur.execute("SELECT * FROM cash_replenishments WHERE atm_id = %s ORDER BY id", (atm_id,))
        cash_cols = [d[0] for d in cur.description]
        cash = [dict(zip(cash_cols, r)) for r in cur.fetchall()]
        counts = {}
        cur.execute("SAVEPOINT _arch")
        for key, tbl in (('txn', 'atm_transactions_visible'), ('anom', 'atm_anomalies'),
                         ('hist', 'atm_state_history')):
            try:
                cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE atm_id = %s", (atm_id,))
                counts[key] = int(cur.fetchone()[0] or 0)
            except Exception:
                counts[key] = -1  # table may not exist on older DBs
                cur.execute("ROLLBACK TO SAVEPOINT _arch")
                cur.execute("SAVEPOINT _arch")
        cur.execute(
            "INSERT INTO atm_delete_archive (atm_id, deleted_by, location, cash_loads,"
            " txn_count, anomaly_count, state_history_count)"
            " VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
            (atm_id, deleted_by, json.dumps(location, default=str),
             json.dumps(cash, default=str),
             counts.get('txn', -1), counts.get('anom', -1), counts.get('hist', -1)))
        # Strip the credential before handing the row to the audit log —
        # the archive keeps it (recovery), the log must not (backups).
        location = {k: v for k, v in location.items() if k != 'snmp_community'}
        return True, location
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).error('Archive of %s failed: %s', atm_id, e)
        try:
            cur.execute("ROLLBACK TO SAVEPOINT _arch")
        except Exception:
            pass
        return False, None


@bp.route('/atms/delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def atm_delete():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    atm_id = request.form.get('atm_id', '').strip()
    if not atm_id:
        if is_ajax:
            return jsonify({'error': True, 'message': 'ATM ID is required.'})
        flash('ATM ID is required for deletion.', 'error')
        return redirect('/atms')

    with get_db() as conn:
        cur = conn.cursor()
        try:
            archived, old_loc = _archive_atm(cur, atm_id, session.get('username', ''))
            cur.execute("DELETE FROM atm_current_state WHERE atm_id = %s", (atm_id,))
            cur.execute("DELETE FROM atm_transactions WHERE atm_id = %s", (atm_id,))
            cur.execute("DELETE FROM atm_anomalies WHERE atm_id = %s", (atm_id,))
            cur.execute("DELETE FROM atm_network_correlation WHERE atm_id = %s", (atm_id,))
            cur.execute("DELETE FROM atm_network_events WHERE atm_id = %s", (atm_id,))
            cur.execute("DELETE FROM atm_network_metrics WHERE atm_id = %s", (atm_id,))
            cur.execute("DELETE FROM atm_locations WHERE atm_id = %s", (atm_id,))
            if cur.rowcount == 0:
                if is_ajax:
                    return jsonify({'error': True, 'message': f'ATM {atm_id} not found.'})
                flash(f'ATM {atm_id} not found.', 'error')
                return redirect('/atms')
            conn.commit()
        except Exception as e:
            conn.rollback()
            if is_ajax:
                return jsonify({'error': True, 'message': f'Database error: {str(e)}'})
            flash(f'Database error: {str(e)}', 'error')
            return redirect('/atms')
        finally:
            cur.close()

    if old_loc:
        del_detail = (f'ATM {atm_id} deleted (was: ' +
                      ', '.join(f'{k}={_audit_val(v)}' for k, v in old_loc.items()
                                if k in ('atm_name', 'branch', 'district', 'city',
                                         'vendor', 'model', 'ip_address', 'status')) +
                      '; full snapshot in atm_delete_archive)')
    else:
        del_detail = f'ATM {atm_id} deleted (no pre-delete row found)'
    if is_ajax:
        log_action('ATM_DELETE', del_detail)
        threading.Thread(target=_bg_zabbix_disable, args=([atm_id],), daemon=True).start()
        return jsonify({'success': True, 'atm_id': atm_id})
    log_action('ATM_DELETE', del_detail)
    threading.Thread(target=_bg_zabbix_disable, args=([atm_id],), daemon=True).start()
    return redirect('/atms')


@bp.route('/atms/bulk-delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def atm_bulk_delete():
    data = request.get_json(silent=True)
    if not data or 'ids' not in data or not isinstance(data['ids'], list):
        return jsonify({'error': True, 'message': 'Please provide a list of ATM IDs.'})

    ids = [i.strip() for i in data['ids'] if i.strip()]
    if not ids:
        return jsonify({'error': True, 'message': 'No ATM IDs provided.'})

    with get_db() as conn:
        cur = conn.cursor()
        try:
            placeholders = ','.join(['%s'] * len(ids))
            for _aid in ids:
                _archive_atm(cur, _aid, session.get('username', ''))
            cur.execute(f"DELETE FROM atm_current_state WHERE atm_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM atm_transactions WHERE atm_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM atm_anomalies WHERE atm_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM atm_network_correlation WHERE atm_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM atm_network_events WHERE atm_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM atm_network_metrics WHERE atm_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM atm_locations WHERE atm_id IN ({placeholders})", ids)
            deleted = cur.rowcount
            conn.commit()
        except Exception as e:
            conn.rollback()
            return jsonify({'error': True, 'message': f'Database error: {str(e)}'})
        finally:
            cur.close()

    log_action('ATM_BULK_DELETE', f'{deleted} ATMs deleted: {",".join(ids)}')
    threading.Thread(target=_bg_zabbix_disable, args=(list(ids),), daemon=True).start()
    return jsonify({'success': True, 'deleted': deleted})


@bp.route('/atms/check', methods=['POST'])
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_check():
    data = request.get_json(silent=True) or {}
    ids = [str(i).strip() for i in (data.get('ids') or []) if str(i).strip()]
    # Case-insensitive so an import preview recognises PATML001 and patml001
    # as the same terminal the way the allocator does.
    terminal_ids = [str(t).strip().upper()
                    for t in (data.get('terminal_ids') or []) if str(t).strip()]
    if not ids and not terminal_ids:
        return jsonify({'existing': [], 'terminal_matches': {},
                        'terminal_ambiguous': {}})
    existing = []
    terminal_matches = {}
    terminal_owners = {}
    with get_db() as conn:
        cur = conn.cursor()
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"SELECT atm_id FROM atm_locations WHERE atm_id IN ({placeholders})", ids)
            existing = [r[0] for r in cur.fetchall()]
        if terminal_ids:
            cur.execute("SELECT UPPER(TRIM(terminal_id)), atm_id FROM atm_locations "
                        "WHERE UPPER(TRIM(terminal_id)) = ANY(%s) ORDER BY atm_id",
                        (terminal_ids,))
            for tid, atm_id in cur.fetchall():
                terminal_matches.setdefault(tid, atm_id)
                terminal_owners.setdefault(tid, []).append(atm_id)
        cur.close()
    terminal_ambiguous = {tid: owners for tid, owners in terminal_owners.items()
                          if len(owners) > 1}
    return jsonify({'existing': existing, 'terminal_matches': terminal_matches,
                    'terminal_ambiguous': terminal_ambiguous})


@bp.route('/cash')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def cash_list():
    """Cash replenishment log (BRD 6.3): recent loads + log form data."""
    atm_filter = request.args.get('atm_id', '').strip()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT atm_id, atm_name FROM atm_locations "
                    "WHERE status = 'active' ORDER BY atm_id")
        atms = [{'atm_id': r[0], 'atm_name': r[1]} for r in cur.fetchall()]
        if atm_filter:
            cur.execute("SELECT id, atm_id, cassette, notes_added, "
                        "to_char(recorded_at, 'YYYY-MM-DD HH24:MI') AS recorded_at, "
                        "recorded_by, notes, source FROM cash_replenishments "
                        "WHERE atm_id = %s ORDER BY recorded_at DESC LIMIT 100",
                        (atm_filter,))
        else:
            cur.execute("SELECT id, atm_id, cassette, notes_added, "
                        "to_char(recorded_at, 'YYYY-MM-DD HH24:MI') AS recorded_at, "
                        "recorded_by, notes, source FROM cash_replenishments "
                        "ORDER BY recorded_at DESC LIMIT 100")
        cols = [d[0] for d in cur.description]
        loads = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
    return render_template('admin_cash.html', atms=atms, loads=loads,
                           atm_filter=atm_filter,
                           cash_token=_mint_cash_token())


def _mint_cash_token():
    """One-time submit token against double-click/retry duplicate loads."""
    toks = session.get('cash_tokens') or []
    toks = [t for t in toks if isinstance(t, str)][-19:]
    tok = secrets.token_urlsafe(16)
    session['cash_tokens'] = toks + [tok]
    session.modified = True
    return tok


def _take_cash_token(tok):
    """Consume a submit token; False means already used (duplicate POST)."""
    toks = session.get('cash_tokens') or []
    if tok and tok in toks:
        session['cash_tokens'] = [t for t in toks if t != tok]
        session.modified = True
        return True
    return False


@bp.route('/cash/log', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def cash_log():
    """Record one cassette load. Validated; audited."""
    atm_id = request.form.get('atm_id', '').strip()
    errors = []
    try:
        cassette = int(request.form.get('cassette', '0'))
    except (ValueError, TypeError):
        cassette = 0
    try:
        notes_added = int(request.form.get('notes_added', '0'))
    except (ValueError, TypeError):
        notes_added = 0
    notes = request.form.get('notes', '').strip()[:500]
    if not atm_id:
        errors.append('ATM is required.')
    if cassette not in (1, 2, 3, 4):
        errors.append('Cassette must be 1–4.')
    if notes_added <= 0:
        errors.append('Notes added must be a positive number.')
    if notes_added > 100000:
        errors.append('Notes added cannot exceed 100,000 per load.')
    if errors:
        flash('Not saved: ' + ' '.join(errors), 'error')
        return redirect('/cash')
    if not _take_cash_token(request.form.get('cash_token', '')):
        # Double-click, back-button resubmit, or retried POST: the first one
        # already committed. Never insert twice; say so plainly.
        flash('Already logged — duplicate submission ignored.', 'success')
        return redirect(f'/cash?atm_id={atm_id}' if atm_id else '/cash')
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM atm_locations WHERE atm_id = %s", (atm_id,))
            if not cur.fetchone():
                flash(f'Not saved: unknown ATM {atm_id}.', 'error')
                return redirect('/cash')
            cur.execute(
                "INSERT INTO cash_replenishments "
                "(atm_id, cassette, notes_added, recorded_by, notes, source) "
                "VALUES (%s, %s, %s, %s, %s, 'MANUAL')",
                (atm_id, cassette, notes_added,
                 session.get('username', 'system'), notes or None))
            conn.commit()
            log_action('CASH_LOAD',
                       f'{notes_added} notes -> {atm_id} cassette {cassette}')
        except Exception as e:
            conn.rollback()
            flash(f'Not saved: database error ({e}).', 'error')
            return redirect('/cash')
        finally:
            cur.close()
    flash(f'Logged: {notes_added} notes into {atm_id} cassette {cassette}.', 'success')
    return redirect(f'/cash?atm_id={atm_id}')


@bp.route('/atms/csv')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_csv_export():
    ids_param = request.args.get('ids', '').strip()

    with get_db() as conn:
        cur = conn.cursor()
        if ids_param:
            ids = [i.strip() for i in ids_param.split(',') if i.strip()]
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"SELECT * FROM atm_locations WHERE atm_id IN ({placeholders}) ORDER BY atm_id", ids)
        else:
            cur.execute("SELECT * FROM atm_locations ORDER BY atm_id")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()

    if session.get('role') == ROLE_VIEWER:
        keep = [i for i, c in enumerate(cols) if c not in _CRED_FIELDS]
        cols = [cols[i] for i in keep]
        rows = [tuple(r[i] for i in keep) for r in rows]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)

    log_action('EXPORT', f'ATM CSV export ({len(rows)} rows, filter={ids_param or "all"})')
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=atm_locations_{datetime.now().strftime("%Y%m%d")}.csv'}
    )


@bp.route('/atms/template')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_csv_template():
    """Blank import template (header + examples) — generated from the live
    required/optional column set so it can never drift from validation."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['terminal_id', 'atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
                'latitude', 'longitude', 'vendor', 'model',
                'install_date', 'status',
                'ip_address', 'snmp_community', 'snmp_version', 'ej_log_format',
                'ej_hostname'])
    # atm_id is blank on purpose: the portal generates ATM-<terminal_id>.
    # An explicit value is still accepted for existing integrations.
    w.writerow(['TID10001', '', 'Bole Medhanialem ATM', 'Bole Medhanialem Branch', 'Bole',
                'Addis Ababa', 'Addis Ababa', '8.9950', '38.7896',
                'NCR', 'SelfServ 22', '2023-03-15', 'active',
                '192.0.2.101', 'CHANGE_ME', '2', 'NCR_APTRA', 'BOLENATM01'])
    w.writerow(['TID10002', '', 'Piassa ATM', 'Piassa Branch', 'Arada',
                'Addis Ababa', 'Addis Ababa', '9.0356', '38.7521',
                'GRG', 'H68N', '2022-11-02', 'active',
                '192.0.2.102', 'CHANGE_ME', '2', 'GRG', ''])
    log_action('EXPORT', 'ATM import template downloaded')
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=atm_import_template.csv'}
    )


@bp.route('/atms/import', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_import():
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': True, 'message': 'Please select a CSV file to upload.'})

    try:
        content = file.stream.read().decode('utf-8-sig')
    except Exception:
        return jsonify({'error': True, 'message': 'Could not read file. Ensure it is a valid UTF-8 CSV.'})

    reader = csv.DictReader(io.StringIO(content))
    # atm_id is deliberately absent: the ATM team uploads terminal IDs only.
    required = ['branch', 'district', 'city', 'region',
                'terminal_id', 'vendor', 'model', 'status']
    missing_cols = [c for c in required if c not in reader.fieldnames]
    if missing_cols:
        return jsonify({'error': True, 'message': f'Missing required columns: {", ".join(missing_cols)}'})

    # One registry read for the whole file, then allocate in memory. A TID-only
    # re-import must resolve to the row that already owns that terminal ID, not
    # create a second key (ATM-PATML001-2) on every upload.
    try:
        with get_db() as conn:
            cur = conn.cursor()
            existing_by_terminal, used_ids, ambiguous_terminals = registry_key_index(cur)
            cur.close()
    except Exception as e:
        logger.warning('ATM import registry read failed: %s', e)
        return jsonify({'error': True,
                        'message': 'Could not read the ATM registry. Try again.'})

    rows = []
    errors = []
    seen_terminals = {}
    seen_ids = {}
    line = 1
    for row in reader:
        line += 1
        # CSV cells arrive as strings; trim them before validation so a stray
        # space cannot turn "NCR" into an unknown vendor or " " into a value.
        for key, value in list(row.items()):
            if isinstance(value, str):
                row[key] = value.strip()
        if 'branch' in row:
            row['branch'] = _norm_branch(row.get('branch'))
        row['atm_id'] = row.get('atm_id') or ''
        terminal_id = row.get('terminal_id') or ''
        row['terminal_id'] = terminal_id
        terminal = terminal_key(terminal_id)
        label = row.get('atm_id') or terminal_id or '?'
        if not row['atm_id'] and terminal in ambiguous_terminals:
            # The registry already has two rows claiming this terminal. Choosing
            # one silently is how a TID-only import overwrote the 4 Kilo test
            # ATM on UAT; refuse the row until a human resolves the duplicate.
            errors.append({
                'line': line, 'atm_id': label,
                'errors': {'terminal_id':
                           'Terminal ID matches multiple registered ATMs ('
                           + ', '.join(ambiguous_terminals[terminal])
                           + '); resolve the duplicate before importing'}})
            continue
        if not row['atm_id'] and terminal:
            try:
                row['atm_id'] = choose_atm_id(terminal_id, used_ids,
                                              existing_by_terminal)
            except ValueError as e:
                errors.append({'line': line, 'atm_id': label,
                               'errors': {'_general': str(e)}})
                continue
        errs = _validate(row)
        if errs:
            errors.append({'line': line, 'atm_id': label, 'errors': errs})
            continue
        # Duplicate checks happen only after a row is otherwise valid: a bad
        # row does not reserve a TID/ID, so a later corrected row can import.
        if terminal:
            if terminal in seen_terminals:
                errors.append({'line': line, 'atm_id': label,
                               'errors': {'terminal_id':
                                          f'Duplicate terminal ID in file '
                                          f'(first on line {seen_terminals[terminal]})'}})
                continue
            seen_terminals[terminal] = line
        atm_id = row['atm_id']
        if atm_id in seen_ids:
            errors.append({'line': line, 'atm_id': atm_id,
                           'errors': {'atm_id':
                                      f'Duplicate ATM ID in file '
                                      f'(first on line {seen_ids[atm_id]})'}})
            continue
        seen_ids[atm_id] = line
        used_ids.add(atm_id)
        if terminal:
            existing_by_terminal[terminal] = atm_id
        rows.append(row)

    if errors and not rows:
        return jsonify({'error': True, 'message': f'All {len(errors)} row(s) have errors. No data imported.',
                        'import_errors': errors})

    imported = 0
    db_errors = []

    def _nn(v):
        # CSV gives '' for empty cells — Postgres DATE/NUMERIC reject ''.
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    if rows:
        with get_db() as conn:
            cur = conn.cursor()
            for row in rows:
                cur.execute("SAVEPOINT atm_import_row")
                try:
                    name = _nn(row.get('atm_name')) or _nn(row.get('branch')) or _nn(row.get('atm_id'))
                    cur.execute("""
                        INSERT INTO atm_locations
                            (atm_id, atm_name, branch, district, city, region, latitude, longitude,
                             terminal_id, vendor, model, install_date, status,
                             ip_address, snmp_community, snmp_version, ej_log_format, ej_hostname)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (atm_id) DO UPDATE SET
                            atm_name     = EXCLUDED.atm_name,
                            branch       = EXCLUDED.branch,
                            district     = EXCLUDED.district,
                            city         = EXCLUDED.city,
                            region       = EXCLUDED.region,
                            latitude     = EXCLUDED.latitude,
                            longitude    = EXCLUDED.longitude,
                            terminal_id  = EXCLUDED.terminal_id,
                            vendor       = EXCLUDED.vendor,
                            model        = EXCLUDED.model,
                            install_date = EXCLUDED.install_date,
                            status       = EXCLUDED.status,
                            ip_address   = COALESCE(EXCLUDED.ip_address, atm_locations.ip_address),
                            snmp_community = COALESCE(EXCLUDED.snmp_community, atm_locations.snmp_community),
                            snmp_version = COALESCE(EXCLUDED.snmp_version, atm_locations.snmp_version),
                            ej_log_format = COALESCE(EXCLUDED.ej_log_format, atm_locations.ej_log_format),
                            ej_hostname   = COALESCE(EXCLUDED.ej_hostname, atm_locations.ej_hostname)
                    """, (
                        _nn(row.get('atm_id')), name, _nn(row.get('branch')), _nn(row.get('district')),
                        _nn(row.get('city')), _nn(row.get('region')), _nn(row.get('latitude')),
                        _nn(row.get('longitude')), _nn(row.get('terminal_id')),
                        _nn(row.get('vendor')), _nn(row.get('model')),
                        _nn(row.get('install_date')), _nn(row.get('status')),
                        _nn(row.get('ip_address')), _nn(row.get('snmp_community')),
                        _nn(row.get('snmp_version')), _nn(row.get('ej_log_format')),
                        _nn(row.get('ej_hostname'))
                    ))
                    # Seed state like single-save does (parity: imported ATMs
                    # must appear in the in-service summary, not just UNKNOWN
                    # via LEFT JOIN). Never clobbers a live state.
                    _st = (_nn(row.get('status')) or 'active')
                    if _st == 'inactive':
                        cur.execute("""
                            INSERT INTO atm_current_state (atm_id, state, last_seen, updated_at)
                            VALUES (%s, 'INACTIVE', NOW(), NOW())
                            ON CONFLICT (atm_id) DO UPDATE SET state = 'INACTIVE', updated_at = NOW()
                        """, (_nn(row.get('atm_id')),))
                    else:
                        cur.execute("""
                            INSERT INTO atm_current_state (atm_id, state, last_seen, updated_at)
                            VALUES (%s, 'UNKNOWN', NOW(), NOW())
                            ON CONFLICT (atm_id) DO UPDATE
                                SET state = CASE WHEN atm_current_state.state = 'INACTIVE' THEN 'UNKNOWN' ELSE atm_current_state.state END,
                                    updated_at = NOW()
                        """, (_nn(row.get('atm_id')),))
                    cur.execute("RELEASE SAVEPOINT atm_import_row")
                    imported += 1
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT atm_import_row")
                    cur.execute("RELEASE SAVEPOINT atm_import_row")
                    db_errors.append({'atm_id': row.get('atm_id'), 'error': str(e)})
            conn.commit()
            # Auto-allocate simulator ports so imported ATMs behave like the others
            for row in rows:
                assign_sim_port(row['atm_id'], row.get('vendor'))
            cur.close()

    log_action('ATM_IMPORT', f'Imported {imported}, skipped {len(errors)}, db_errors {len(db_errors)}')
    if imported:
        failed_ids = {d.get('atm_id') for d in db_errors}
        ok_rows = [dict(r) for r in rows if r.get('atm_id') not in failed_ids]
        if ok_rows:
            threading.Thread(target=_bg_zabbix_sync, args=(ok_rows,), daemon=True).start()
    return jsonify({
        'success': True,
        'imported': imported,
        'skipped': len(errors),
        'db_errors': db_errors,
        'import_errors': errors
    })


@bp.route('/atms/zabbix-sync', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_zabbix_sync():
    """On-demand server-side sync: create missing Zabbix hosts for given (or all) ATMs.

    Routed through provision.provision_atms() so a manual sync also updates the
    durable state table. The old direct sync_hosts() call left the state badge
    saying "retry" after a successful manual repair.
    """
    from zabbix_sync import MAX_BATCH
    from provision import provision_atms
    data = request.get_json(silent=True) or {}
    ids = [str(i).strip() for i in (data.get('ids') or []) if str(i).strip()][:MAX_BATCH]
    with get_db() as conn:
        cur = conn.cursor()
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"""SELECT atm_id FROM atm_locations
                            WHERE atm_id IN ({placeholders})""", ids)
        else:
            cur.execute(f"""SELECT atm_id FROM atm_locations
                            WHERE status = 'active'
                            ORDER BY atm_id LIMIT {MAX_BATCH}""")
        ids = [r[0] for r in cur.fetchall()]
        cur.close()
    if not ids:
        return jsonify({'success': True, 'checked': 0, 'created': 0, 'updated': 0,
                        'skipped': 0, 'no_ip': 0, 'failed': 0})
    out = provision_atms(ids, dry_run=False, actor='manual-sync')
    summary = out.get('summary') or {}
    log_action('ZABBIX_SYNC', f'manual: created={summary.get("created", 0)} '
                              f'reenabled={summary.get("reenabled", 0)} '
                              f'macros={summary.get("macros_fixed", 0)} '
                              f'updated={summary.get("updated", 0)} '
                              f'skipped={summary.get("skipped", 0)} '
                              f'no_ip={summary.get("no_ip", 0)} '
                              f'failed={summary.get("failed", 0)} (n={len(ids)})')
    return jsonify({'success': not out.get('error'), 'checked': len(ids),
                    'error': out.get('error') or '', **summary})


@bp.route('/atms/zabbix-status')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_zabbix_status():
    """Existence map {atm_id: bool} so the UI can badge sync state."""
    from zabbix_sync import host_exists_map
    ids = [i.strip() for i in request.args.get('ids', '').split(',') if i.strip()][:200]
    return jsonify(host_exists_map(ids))


def _validate(data):
    errors = {}
    required = ['atm_id', 'branch', 'district', 'city', 'region',
                'terminal_id', 'vendor', 'model', 'status']
    for field in required:
        if not data.get(field):
            errors[field] = ('Required (or provide a Terminal ID to generate one)'
                             if field == 'atm_id' else 'This field is required')
    if data.get('atm_id') and len(data['atm_id']) < 3:
        errors['atm_id'] = 'ATM ID must be at least 3 characters'
    if data.get('atm_id') and not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', data['atm_id']):
        errors['atm_id'] = 'Letters, digits, dash and underscore only'
    maxlens = {'atm_id': 20, 'branch': 100, 'district': 100, 'city': 100,
               'region': 100, 'terminal_id': 20, 'vendor': 50, 'model': 50,
               'ip_address': 45, 'snmp_community': 100, 'ej_log_format': 20,
               'ej_hostname': 64}
    for field, maxlen in maxlens.items():
        val = data.get(field)
        if val and len(val) > maxlen:
            errors[field] = f'Max {maxlen} characters'
    # Network fields are optional (older rows lack them) — validate shape only.
    if data.get('ip_address'):
        import re as _re
        if not _re.match(r'^(\d{1,3}\.){3}\d{1,3}$', data['ip_address']) or \
                any(int(p) > 255 for p in data['ip_address'].split('.')):
            errors['ip_address'] = 'Must be a valid IPv4 address'
    if data.get('snmp_version') and str(data['snmp_version']) not in ('2', '3'):
        errors['snmp_version'] = 'Must be 2 or 3'
    if data.get('ej_log_format') and data['ej_log_format'] not in ('NCR_APTRA', 'GRG'):
        errors['ej_log_format'] = 'Must be NCR_APTRA or GRG'
    if data.get('latitude'):
        try:
            n = float(data['latitude'])
            if n < -90 or n > 90:
                errors['latitude'] = 'Must be between -90 and 90'
        except (ValueError, TypeError):
            errors['latitude'] = 'Must be a valid number'
    if data.get('longitude'):
        try:
            n = float(data['longitude'])
            if n < -180 or n > 180:
                errors['longitude'] = 'Must be between -180 and 180'
        except (ValueError, TypeError):
            errors['longitude'] = 'Must be a valid number'
    if data.get('install_date'):
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', data['install_date']):
            errors['install_date'] = 'Use YYYY-MM-DD format'
    if data.get('status') and data['status'] not in ('active', 'inactive'):
        errors['status'] = 'Must be active or inactive'
    KNOWN_VENDORS = ('NCR', 'GRG')
    if data.get('vendor') and data['vendor'] not in KNOWN_VENDORS:
        errors['vendor'] = f'Must be one of {", ".join(KNOWN_VENDORS)} (Dashen only deploys NCR and GRG)'
    return errors


# ─── AUDIT LOG ───────────────────────────────────────────────

@bp.route('/admin/audit')
@login_required
@role_required(ROLE_ADMIN)
def audit_log():
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    search = request.args.get('search', '').strip()
    action_filter = request.args.get('action', '').strip()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            where_clauses = []
            params = []
            if search:
                where_clauses.append("(username ILIKE %s OR detail ILIKE %s)")
                like = f'%{search}%'
                params.extend([like, like])
            if action_filter:
                where_clauses.append("action = %s")
                params.append(action_filter)
            where_sql = ' AND '.join(where_clauses) if where_clauses else 'TRUE'

            cur.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where_sql}", params)
            total = cur.fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)

            cur.execute(f"""
                SELECT id, performed_at, username, action, detail, ip_address
                FROM audit_log WHERE {where_sql}
                ORDER BY performed_at DESC LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page])
            rows = cur.fetchall()

            cur.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
            actions = [r[0] for r in cur.fetchall()]
            cur.close()
        rows = [list(r) for r in rows]
        for r in rows:
            r[1] = to_eat(r[1], '%Y-%m-%d %H:%M')
    except Exception as e:
        rows = []
        total = 0
        total_pages = 1
        actions = []

    return render_template('admin_audit.html', logs=rows, total=total,
                           page=page, total_pages=total_pages, search=search,
                           action_filter=action_filter, actions=actions)


# ─── SCHEDULED REPORTS ─────────────────────────────────────────

@bp.route('/admin/schedules')
@login_required
@role_required(ROLE_ADMIN)
def schedule_list():
    custom_reports = []
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, report_type, format, schedule, recipients, "
                "enabled, last_run, last_status, last_error, params "
                "FROM scheduled_reports ORDER BY id")
            rows = cur.fetchall()
            cur.execute(
                "SELECT id, name FROM report_definitions ORDER BY name")
            custom_reports = cur.fetchall()
            cur.close()
        rows = [list(r) for r in rows]
        custom_names = {str(report_id): name
                        for report_id, name in custom_reports}
        for r in rows:
            r[7] = to_eat(r[7], '%Y-%m-%d %H:%M')
            if r[2] == 'custom':
                match = re.search(r'definition_id\s*=\s*(\d+)',
                                  r[10] or '')
                saved_name = custom_names.get(
                    match.group(1)) if match else None
                r[2] = f'Custom: {saved_name or "saved report"}'
    except Exception:
        rows = []
        failed = []
        custom_reports = []
    failed = [r for r in rows if len(r) > 8 and r[8] == 'failed']
    return render_template('admin_schedules.html', schedules=rows, failed=failed,
                           type_labels=REPORT_TYPE_LABELS,
                           custom_reports=custom_reports)


def _valid_cron(expr):
    """Validate a 5-field cron using the scheduler's own parser."""
    return validate_cron(expr)


@bp.route('/admin/schedules/save', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_save():
    name = request.form.get('name', '').strip()
    report_type = request.form.get('report_type', '').strip()
    fmt = request.form.get('format', 'pdf').strip()
    schedule = request.form.get('schedule', '').strip()
    recipients = request.form.get('recipients', '').strip()
    params = request.form.get('params', '').strip()
    definition_id = request.form.get('definition_id', '').strip()
    schedule_id = request.form.get('id', '').strip()

    if not all([name, report_type, schedule, recipients]):
        flash('Name, report type, schedule, and recipients are required.', 'error')
        return redirect('/admin/schedules')

    if fmt not in ('pdf', 'excel', 'csv'):
        flash('Choose a valid report format.', 'error')
        return redirect('/admin/schedules')

    bad = [r.strip() for r in recipients.split(',') if r.strip() and not _valid_email(r.strip())]
    if bad:
        flash(f'Invalid email address(es): {", ".join(bad)}', 'error')
        return redirect('/admin/schedules')

    cron_err = _valid_cron(schedule)
    if cron_err:
        flash(f'Invalid schedule: {cron_err}', 'error')
        return redirect('/admin/schedules')

    if report_type == 'custom':
        if not definition_id.isdigit():
            flash('Choose a saved custom report for this schedule.', 'error')
            return redirect('/admin/schedules')
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM report_definitions WHERE id = %s",
                    (int(definition_id),))
                found = cur.fetchone()
                cur.close()
            if not found:
                flash('That saved custom report no longer exists.', 'error')
                return redirect('/admin/schedules')
        except Exception as e:
            flash(f'Database error: {e}', 'error')
            return redirect('/admin/schedules')
        # The scheduler reads the saved definition id from params.
        params = f'definition_id={int(definition_id)}'

    try:
        with get_db() as conn:
            cur = conn.cursor()
            if schedule_id and schedule_id.isdigit():
                cur.execute("""
                    UPDATE scheduled_reports SET name=%s, report_type=%s, format=%s,
                        schedule=%s, recipients=%s, params=%s
                    WHERE id=%s
                """, (name, report_type, fmt, schedule, recipients, params or None, schedule_id))
            else:
                cur.execute("""
                    INSERT INTO scheduled_reports (name, report_type, format, schedule, recipients, params)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (name, report_type, fmt, schedule, recipients, params or None))
            conn.commit()
            cur.close()
        log_action('SCHEDULE_SAVE', f'Schedule "{name}" ({report_type}/{fmt}) saved')
        flash('Schedule saved.', 'success')
    except Exception as e:
        flash(f'Database error: {e}', 'error')

    return redirect('/admin/schedules')


@bp.route('/admin/schedules/toggle', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_toggle():
    data = request.get_json(silent=True)
    sid = data.get('id') if data else None
    if not sid:
        return jsonify({'error': True, 'message': 'Missing id'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE scheduled_reports SET enabled = NOT enabled WHERE id = %s RETURNING enabled, name", (sid,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        if row:
            log_action('SCHEDULE_TOGGLE', f'Schedule "{row[1]}" {"enabled" if row[0] else "disabled"}')
            return jsonify({'success': True, 'enabled': row[0]})
        return jsonify({'error': True, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/admin/schedules/delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_delete():
    data = request.get_json(silent=True)
    sid = data.get('id') if data else None
    if not sid:
        return jsonify({'error': True, 'message': 'Missing id'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM scheduled_reports WHERE id = %s RETURNING name", (sid,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        if row:
            log_action('SCHEDULE_DELETE', f'Schedule "{row[0]}" deleted')
            sched = current_app.config.get('SCHEDULER')
            if sched:
                try:
                    sched.remove_job(f'report_{sid}')
                except Exception:
                    pass
            return jsonify({'success': True})
        return jsonify({'error': True, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


@bp.route('/admin/schedules/send-now', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_send_now():
    """Manually fire a schedule immediately (testing + resends). Runs in a
    background thread like the poller; works whether or not enabled."""
    data = request.get_json(silent=True) or {}
    sid = str(data.get('id', ''))
    if not sid.isdigit():
        return jsonify({'success': False, 'message': 'Missing schedule id.'})
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, report_type, format, schedule, recipients, params"
                        " FROM scheduled_reports WHERE id = %s", (sid,))
            row = cur.fetchone()
            cur.close()
        if not row:
            return jsonify({'success': False, 'message': 'Schedule not found.'}), 404
        rid, name, report_type, fmt, cron_expr, recipients, params = row
        from scheduler import _dispatch, _running, _running_lock
        with _running_lock:
            if rid in _running:
                return jsonify({'success': False,
                                'message': 'This schedule is already sending — wait a minute.'})
            _running.add(rid)
        threading.Thread(target=_dispatch,
                         args=(rid, report_type, fmt, cron_expr, recipients, params),
                         daemon=True).start()
        log_action('SCHEDULE_SEND_NOW', f'Manually triggered "{name}" ({report_type}/{fmt})')
        return jsonify({'success': True,
                        'message': f'Sending "{name}" now — watch Last Run for the outcome.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/admin/schedules/get')
@login_required
@role_required(ROLE_ADMIN)
def schedule_get():
    sid = request.args.get('id', '').strip()
    if not sid or not sid.isdigit():
        return jsonify({'error': True, 'message': 'Missing id'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, report_type, format, schedule, recipients, params, enabled FROM scheduled_reports WHERE id = %s", (sid,))
            row = cur.fetchone()
            cur.close()
        if row:
            return jsonify({
                'id': row[0], 'name': row[1], 'report_type': row[2],
                'format': row[3], 'schedule': row[4], 'recipients': row[5],
                'params': row[6], 'enabled': row[7]
            })
        return jsonify({'error': True, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': True, 'message': str(e)}), 500


def _valid_email(v):
    value = (v or '').strip()
    if value.count('@') != 1 or any(ch.isspace() for ch in value):
        return False
    local, _, domain = value.partition('@')
    if not local or not domain or '.' not in domain:
        return False
    return all(label for label in domain.split('.'))


@bp.route('/admin/users')
@login_required
@role_required(ROLE_ADMIN)
def user_list():
    search = request.args.get('search', '').strip()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if search:
                cur.execute("SELECT username, role, created_at, last_login, email FROM app_users WHERE username ILIKE %s ORDER BY created_at DESC", (f'%{search}%',))
            else:
                cur.execute("SELECT username, role, created_at, last_login, email FROM app_users ORDER BY created_at DESC")
            users = cur.fetchall()
            cur.close()
        users = [list(u) for u in users]
        for u in users:
            u[2] = to_eat(u[2], '%Y-%m-%d')
            u[3] = to_eat(u[3], '%Y-%m-%d %H:%M')
        return render_template('admin_users.html', users=users, roles=ROLES, search=search)
    except Exception as e:
        flash(str(e), 'error')
        return render_template('admin_users.html', users=[], roles=ROLES, search=search)


@bp.route('/admin/users/emails')
@login_required
@role_required(ROLE_ADMIN)
def user_emails():
    """Directory autocomplete for report recipients (hybrid with free text)."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT username, email FROM app_users
                           WHERE email IS NOT NULL AND email <> ''
                             AND (username ILIKE %s OR email ILIKE %s)
                           ORDER BY username LIMIT 10""", (f'%{q}%', f'%{q}%'))
            out = [{'username': r[0], 'email': r[1]} for r in cur.fetchall()]
            cur.close()
        return jsonify(out)
    except Exception:
        return jsonify([])


def _password_error(password, username=None):
    # Single source of truth lives in blueprints.auth (shared with
    # self-service change); kept as a thin wrapper for existing callers.
    return password_error(password, username)


def _email_taken(cur, email, exclude_username=None):
    """Case-insensitive duplicate check (NULL/blank grandfathered)."""
    if exclude_username:
        cur.execute("SELECT 1 FROM app_users WHERE email IS NOT NULL AND LOWER(email) = LOWER(%s) AND username <> %s",
                    (email, exclude_username))
    else:
        cur.execute("SELECT 1 FROM app_users WHERE email IS NOT NULL AND LOWER(email) = LOWER(%s)",
                    (email,))
    return cur.fetchone() is not None


@bp.route('/admin/users/create', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def user_create():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    password_confirm = request.form.get('password_confirm', '').strip()
    role = request.form.get('role', ROLE_VIEWER).strip()
    email = request.form.get('email', '').strip()
    if not username or len(username) < 3:
        flash('Username must be at least 3 characters', 'error')
        return redirect('/admin/users')
    import re as _re
    if not _re.match(r'^[A-Za-z0-9_]+$', username):
        flash('Username: letters, numbers and underscore only', 'error')
        return redirect('/admin/users')
    pw_err = _password_error(password, username)
    if pw_err:
        flash(pw_err, 'error')
        return redirect('/admin/users')
    if password != password_confirm:
        flash('Passwords do not match.', 'error')
        return redirect('/admin/users')
    if email and not _valid_email(email):
        flash('Email address looks invalid', 'error')
        return redirect('/admin/users')
    if not email:
        flash('Email is required (report delivery and account recovery).', 'error')
        return redirect('/admin/users')
    if role not in ROLES:
        flash('Invalid role selected', 'error')
        return redirect('/admin/users')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT username FROM app_users WHERE username = %s", (username,))
            if cur.fetchone():
                cur.close()
                flash(f'User "{username}" already exists', 'error')
                return redirect('/admin/users')
            if _email_taken(cur, email):
                cur.close()
                flash(f'Email "{email}" is already used by another account.', 'error')
                return redirect('/admin/users')
            cur.execute(
                "INSERT INTO app_users (username, password_hash, role, email, must_change_pw) VALUES (%s, %s, %s, %s, TRUE)",
                (username, generate_password_hash(password), role, email or None)
            )
            conn.commit()
            cur.close()
        log_action('USER_CREATE', f'Created {role} user "{username}"')
        flash(f'User "{username}" created successfully', 'success')
        return redirect('/admin/users')
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect('/admin/users')


def _admin_count(cur):
    cur.execute("SELECT COUNT(*) FROM app_users WHERE role = 'admin'")
    return int(cur.fetchone()[0] or 0)


@bp.route('/admin/users/edit', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def user_edit():
    username = request.form.get('username', '').strip()
    role = request.form.get('role', '').strip()
    password = request.form.get('password', '').strip()
    email = request.form.get('email', '').strip()
    if not username:
        flash('No username provided', 'error')
        return redirect('/admin/users')
    if role not in ROLES:
        flash('Invalid role selected', 'error')
        return redirect('/admin/users')
    if not email:
        flash('Email is required (report delivery and account recovery).', 'error')
        return redirect('/admin/users')
    if not _valid_email(email):
        flash('Email address looks invalid', 'error')
        return redirect('/admin/users')
    if password:
        if password != request.form.get('password_confirm', '').strip():
            flash('Passwords do not match.', 'error')
            return redirect('/admin/users')
        pw_err = _password_error(password, username)
        if pw_err:
            flash(pw_err, 'error')
            return redirect('/admin/users')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT role FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            if not row:
                cur.close()
                flash(f'User "{username}" not found', 'error')
                return redirect('/admin/users')
            if row[0] == ROLE_ADMIN and role != ROLE_ADMIN and _admin_count(cur) <= 1:
                cur.close()
                flash('Cannot demote the last admin user.', 'error')
                return redirect('/admin/users')
            if _email_taken(cur, email, exclude_username=username):
                cur.close()
                flash(f'Email "{email}" is already used by another account.', 'error')
                return redirect('/admin/users')
            if password:
                # Forced change is for passwords GIVEN to someone else. Setting
                # your OWN password this way already proves knowledge of it —
                # forcing another change next login is a pointless loop.
                force_change = username != session.get('username', '')
                cur.execute("UPDATE app_users SET role = %s, password_hash = %s, email = %s, must_change_pw = %s WHERE username = %s",
                            (role, generate_password_hash(password), email or None, force_change, username))
            else:
                cur.execute("UPDATE app_users SET role = %s, email = %s WHERE username = %s",
                            (role, email or None, username))
            conn.commit()
            cur.close()
        log_action('USER_EDIT', f'Updated {username} (role={role})')
        flash(f'User "{username}" updated', 'success')
        return redirect('/admin/users')
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect('/admin/users')


@bp.route('/admin/users/delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def user_delete():
    username = request.form.get('username', '').strip()
    if not username:
        flash('No username provided', 'error')
        return redirect('/admin/users')
    if username == 'admin':
        flash('Cannot delete the default admin user', 'error')
        return redirect('/admin/users')
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT role FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            if not row:
                cur.close()
                flash(f'User "{username}" not found', 'error')
                return redirect('/admin/users')
            if row[0] == ROLE_ADMIN and _admin_count(cur) <= 1:
                cur.close()
                flash('Cannot delete the last admin user.', 'error')
                return redirect('/admin/users')
            cur.execute("DELETE FROM app_users WHERE username = %s", (username,))
            conn.commit()
            cur.close()
        log_action('USER_DELETE', f'Deleted user "{username}"')
        flash(f'User "{username}" deleted', 'success')
        return redirect('/admin/users')
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect('/admin/users')


def _guide_page(data):
    atm_id_val = data.get('atm_id') or 'ATM-XXX'

    return f"""<!DOCTYPE html><html><head><title>ATM Registered</title>
<style>
  body {{font-family:sans-serif;background:#f5f7fa;padding:40px;}}
  .card {{background:#fff;border-radius:12px;padding:32px;max-width:800px;margin:0 auto;box-shadow:0 2px 12px rgba(0,0,0,.08);}}
  h1 {{color:#012169;margin-top:0;}}
  h2 {{color:#374151;font-size:16px;margin-top:28px;}}
  pre {{background:#1e293b;color:#e2e8f0;padding:20px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.6;}}
  .badge-ok {{display:inline-block;background:#d1fae5;color:#065f46;padding:4px 10px;border-radius:20px;font-size:13px;font-weight:600;}}
  .badge-warn {{display:inline-block;background:#fef3c7;color:#92400e;padding:4px 10px;border-radius:20px;font-size:13px;font-weight:600;}}
  .btn {{display:inline-block;margin-top:24px;background:#012169;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;}}
  ul {{line-height:2;}}
</style></head><body>
<div class="card">
  <h1>&#10003; {atm_id_val} Registered Successfully</h1>
  <p>The ATM has been saved to the database and will appear in the Grafana map, portal dropdowns, and EJ Search filter immediately.</p>

  <h2>What happens automatically <span class="badge-ok">&#10003; No action needed</span></h2>
  <ul>
    <li><strong>Simulation engines</strong> — the multi-tenant engines (<code>atm-sim-engine</code>, <code>atm-txn-engine</code>, <code>atm-ej-engine</code>, <code>state-manager</code>) auto-discover every ATM in <code>atm_locations</code>. No per-ATM compose service is required.</li>
    <li><strong>ATM dropdown</strong> — already shows {atm_id_val} in all search filters.</li>
  </ul>

  <h2>Real ATM (production) <span class="badge-warn">&#9888; Action required</span></h2>
  <p>Journals are fetched hourly through the Zabbix agent. The pull list comes
  from this record, so set <strong>EJ Hostname</strong> to the machine name used
  in the local evidence filename — no script editing.</p>
  <p>NCR: <code>scripts/pull_ej_logs.sh</code> reads
  <code>E:\\EJLogs\\&lt;date&gt;_&lt;HOST&gt;_EJDATA.log</code>. GRG:
  <code>scripts/pull_grg_ej_logs.py</code> reads
  <code>D:\\Program Files\\DTATMW\\Bin\\ATMAPP\\Log\\EJ&lt;yymmdd&gt;.log</code>
  and stores it under the same canonical local name.</p>

  <a href="/atms" class="btn">&#8592; Back to ATM Management</a>
</div></body></html>"""
