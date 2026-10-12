"""
blueprints/admin.py
ATM Admin registration routes.
"""
import csv, io, re, os, json, logging, secrets, threading, concurrent.futures, urllib.request
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, redirect, jsonify, flash, Response, current_app, abort, session
from werkzeug.security import generate_password_hash
from blueprints.auth import login_required, role_required, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, password_error
from db import get_db
from audit import log_action
from scheduler import REPORT_TYPE_LABELS

logger = logging.getLogger(__name__)


def _bg_zabbix_sync(rows):
    """Best-effort background host creation — never fails the request."""
    try:
        from zabbix_sync import sync_hosts
        s = sync_hosts(rows)
        log_action('ZABBIX_SYNC', f'auto: created={s["created"]} reenabled={s.get("reenabled", 0)} macros={s.get("macros_fixed", 0)} updated={s.get("updated", 0)} skipped={s["skipped"]} no_ip={s["no_ip"]} failed={s["failed"]}')
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

bp = Blueprint('admin', __name__, url_prefix='/admin')

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
        r = requests.get(f"http://{GATEWAY_IP}:{port}/metrics", timeout=3)
        if r.status_code == 200:
            return _sim_to_zbx(r.json())
    except Exception:
        return None
    return None


# ─── Zabbix API (real-ATM live telemetry) ────────────────────────────────────
ZABBIX_URL = os.environ.get('ZABBIX_URL', 'http://zabbix-web:8080/api_jsonrpc.php')
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


def _zbx_call(method, params, auth):
    try:
        res = _zbx_post({'jsonrpc': '2.0', 'method': method, 'params': params,
                         'auth': auth, 'id': 1})
        return res.get('result')
    except Exception as e:
        print(f"[ZABBIX] call {method} failed: {e}")
        return None


def fetch_zabbix_metrics(atm_id):
    """Return {item_key: lastvalue} for the ATM's Zabbix host, or None if unavailable."""
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
                                       'output': ['key_', 'lastvalue', 'lastclock'],
                                       'filter': {'status': 0}}, auth)
        if not items:
            return None
        # lastclock "0" = never collected (Zabbix still reports lastvalue "0"):
        # map to None so the detail page shows grey no-data instead of fake zeros.
        return {it['key_']: (it.get('lastvalue') if str(it.get('lastclock')) != '0' else None)
                for it in items}
    except Exception:
        return None


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
        metrics = None
        if ENABLE_SIMULATORS:
            port = atm.get('sim_port')
            if not port:
                return (atm_id, None, None)
            try:
                r = requests.get(f"http://{GATEWAY_IP}:{port}/metrics", timeout=2)
                metrics = _sim_to_zbx(r.json()) if r.status_code == 200 else None
            except Exception:
                metrics = None
        else:
            metrics = fetch_zabbix_metrics(atm_id)
        if metrics:
            state = determine_live_state(vendor, metrics)
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
    else:  # GRG — three module bays side by side
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


def _wmi_chip(m, *part_names):
    """Worst-of chip status across WMI part instances.

    Vendor legend (NCR_DevDevicePart qualifiers): 0 OK, 1 Warning, 2 Error,
    other nonzero Unknown; missing data renders grey 'unknown' — never red.
    No-data-red was the portal's oldest lie (Card Reader showed Fault while
    WMI read OK); this helper makes that impossible by construction.
    """
    worst, rank, seen = 'unknown', -1, False
    for p in part_names:
        v = m.get(f'atm.part.part_status[{p}]')
        if v is None or v == '':
            continue
        seen = True
        try:
            code = int(float(v))
        except (TypeError, ValueError):
            continue
        if code == 2:
            r, s = 3, 'error'
        elif code == 1:
            r, s = 2, 'warn'
        elif code == 0:
            r, s = 0, 'ok'
        else:
            r, s = 1, 'unknown'
        if r > rank:
            worst, rank = s, r
    return worst if seen else 'unknown'


def build_hardware(vendor, m):
    """Hardware component status pills. ok=green, warn=amber, error=red.

    The components shown here MUST mirror the OIDs that drive the overall
    ATM state in `state_manager.determine_state` (cash_jam + card_reader),
    otherwise the detail page disagrees with the HARDWARE_FAULT badge.

    NCR agent hosts: WMI dependents are authoritative (SNMP OIDs are
    unmapped simulator roots on real hardware) — returns (chips, 'WMI').
    Otherwise the legacy SNMP mapping — returns (chips, 'SNMP').
    """
    if m.get('atm.parts') not in (None, ""):
        comps = [
            ('Card reader', _wmi_chip(m, 'Card Reader', 'Smart Card Reader')),
            ('Printer', _wmi_chip(m, 'Print Head', 'Paper')),
            ('Pin pad', _wmi_chip(m, 'PinPad')),
            ('Cash jam', _wmi_chip(m, 'Mechanism', 'Single Note Transport')),
            ('Retract bin', _wmi_chip(m, 'Retract Area', 'Purge Bin', 'Reject Area')),
            ('Network', 'unknown'),  # no WMI source — grey, never red
            ('UPS', 'unknown'),      # no WMI source — grey, never red
        ]
        return ([{'name': n, 'status': s} for n, s in comps], 'WMI')
    def num(v, default=0):
        # Zabbix API returns every value as a string (or None when an item
        # has never collected). Coerce once so comparisons below are safe
        # for live strings, dataless hosts, and real ints alike.
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    cash_jam = num(m.get('atm.cash.jam'))
    card = num(m.get('atm.card.reader'))
    comps = [
        ('Card reader', 'ok' if card == 1 else ('error' if card == 2 else 'error')),
        ('Printer', 'ok' if num(m.get('atm.printer.paper'), None) == 1 else 'error'),
        ('Pin pad', 'ok'),
        ('Cash jam', 'error' if cash_jam == 1 else 'ok'),
        ('Retract bin', 'ok' if num(m.get('atm.cash.rejectbin')) < 80 else 'warn'),
        ('Network', 'ok' if num(m.get('atm.net.link'), None) == 1 else 'error'),
        ('UPS', 'ok' if num(m.get('atm.power.ups')) > 0 else 'error'),
    ]
    return ([{'name': n, 'status': s} for n, s in comps], 'SNMP')


FIELDS = [
    ('atm_id',      'ATM ID',       'text',   20),
    ('atm_name',    'Name',         'text',   100),
    ('branch',      'Branch',       'text',   100),
    ('district',    'District',     'text',   100),
    ('city',        'City',         'text',   100),
    ('region',      'Region',       'text',   100),
    ('latitude',    'Latitude',     'number', None),
    ('longitude',   'Longitude',    'number', None),
    ('terminal_id', 'Terminal ID',  'text',   20),
    ('vendor',      'Vendor',       'text',   50),
    ('model',       'Model',        'text',   50),
    ('install_date','Install Date', 'date',   None),
    ('status',      'Status',       'select', None),
    ('ip_address',  'IP Address',   'text',   45),
    ('snmp_community', 'SNMP Community', 'password', 100),
    ('snmp_version','SNMP Version', 'select', None),
    ('ej_log_format','EJ Format',   'select', None),
]


@bp.route('/atm')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_list():
    search = request.args.get('search', '').strip()
    vendor = request.args.get('vendor', '').strip()
    status = request.args.get('status', '').strip()
    region = request.args.get('region', '').strip()
    city = request.args.get('city', '').strip()
    per_page = 50

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1

    with get_db() as conn:
        cur = conn.cursor()

        clauses = []
        params = []
        if search:
            like = f'%{search}%'
            clauses.append("(l.atm_id ILIKE %s OR l.branch ILIKE %s "
                           "OR l.district ILIKE %s OR l.city ILIKE %s OR l.region ILIKE %s)")
            params.extend([like, like, like, like, like])
        if vendor:
            clauses.append("l.vendor = %s")
            params.append(vendor)
        if status:
            clauses.append("l.status = %s")
            params.append(status)
        if region:
            clauses.append("l.region = %s")
            params.append(region)
        if city:
            clauses.append("l.city = %s")
            params.append(city)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        cur.execute(f"SELECT COUNT(*) FROM atm_locations l {where}", params)
        total = cur.fetchone()[0]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        cur.execute(f"""SELECT l.*, s.state, s.last_seen AT TIME ZONE 'Africa/Addis_Ababa' as last_seen,
                               s.state_changed_at
                        FROM atm_locations l
                        LEFT JOIN atm_current_state s ON l.atm_id = s.atm_id
                        {where}
                        ORDER BY l.atm_id LIMIT %s OFFSET %s""",
                    params + [per_page, (page - 1) * per_page])

        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

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

    return render_template('admin_atm.html', atms=atms, atm_count=total,
                           page=page, total_pages=total_pages,
                           search=search, fields=FIELDS, summary=summary,
                           f_vendor=vendor, f_status=status, f_region=region, f_city=city,
                           filter_options={'regions': region_options, 'cities': city_options},
                           vendor_options=vendor_options)


@bp.route('/atm/<atm_id>')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_detail(atm_id):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT l.*,
                    COALESCE(s.state, 'UNKNOWN') as current_state,
                    s.state_changed_at,
                    s.last_seen AT TIME ZONE 'Africa/Addis_Ababa' as last_seen
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
        cap_map = load_cassette_capacity()
        cassette_cap = cap_map.get(atm_id, CASSETTE_MAX)
        cassettes = build_cassettes(vendor, metrics, cassette_cap) if metrics else []
        hw, hw_source = build_hardware(vendor, metrics) if metrics else ([], None)

        # Override current_state with live data for consistency
        if metrics:
            live_state = determine_live_state(vendor, metrics)
            if live_state:
                atm['current_state'] = live_state
                atm['live'] = True
        elif 'live' not in atm:
            atm['live'] = False

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*),
                    ROUND(100.0 * SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0), 1),
                    COALESCE(SUM(CASE WHEN status='APPROVED' AND txn_type='WITHDRAWAL'
                        THEN amount ELSE 0 END), 0)
                FROM atm_transactions
                WHERE atm_id = %s AND recorded_at >= NOW() - INTERVAL '24 hours'
            """, (atm_id,))
            q = cur.fetchone()
            total_txns = int(q[0] or 0)
            success_rate = float(q[1] or 0)
            cash_today = float(q[2] or 0)
            cur.execute("""
                SELECT COUNT(*) FROM atm_transactions
                WHERE atm_id = %s AND status = 'ERROR'
                AND recorded_at >= NOW() - INTERVAL '24 hours'
            """, (atm_id,))
            active_faults = int(cur.fetchone()[0] or 0)
            cur.execute("""
                SELECT
                    TO_CHAR(t.recorded_at AT TIME ZONE 'Africa/Addis_Ababa', 'HH24:MI:SS'),
                    t.txn_type, t.card_masked, t.amount, t.status, t.auth_code,
                    COALESCE(f.display_name, t.error_code) AS error_label
                FROM atm_transactions t
                LEFT JOIN atm_locations l ON t.atm_id = l.atm_id
                LEFT JOIN fault_type_map f ON t.error_code = f.raw_fault_code AND f.vendor = l.vendor
                WHERE t.atm_id = %s
                ORDER BY t.recorded_at DESC
                LIMIT 20
            """, (atm_id,))
            txns = cur.fetchall()
            cur.close()

        return render_template('admin_atm_detail.html',
                               atm=atm, vendor=vendor, cassettes=cassettes,
                               hardware=hw, hw_source=hw_source,
                               metrics_online=metrics is not None,
                               total_txns=total_txns, success_rate=success_rate,
                               cash_today=cash_today, active_faults=active_faults,
                               txns=txns)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('ATM detail error: %s', e, exc_info=True)
        return render_template('error_500.html'), 500


@bp.route('/atm/get')
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


@bp.route('/atm/save', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_save():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    field_names = [f[0] for f in FIELDS]
    data = {}
    for f in field_names:
        v = request.form.get(f, '').strip()
        data[f] = v if v else None

    # Name is optional in the form; default it so the Name column is never blank.
    if not data.get('atm_name'):
        data['atm_name'] = data.get('branch') or data.get('atm_id')

    # Owner-branch convention (validated + stored in canonical form).
    data['branch'] = _norm_branch(data.get('branch'))

    errors = _validate(data)
    if errors:
        if is_ajax:
            return jsonify({'error': True, 'errors': errors, 'message': 'Please fix the highlighted fields.'})
        flash('Validation failed. Please correct the highlighted fields.', 'error')
        return redirect('/admin/atm')

    with get_db() as conn:
        cur = conn.cursor()
        try:
            # Before-image for the audit trail (None = create, not update).
            cur.execute("SELECT * FROM atm_locations WHERE atm_id = %s", (data['atm_id'],))
            _cols = [d[0] for d in cur.description]
            _fetched = cur.fetchone()
            old_row = dict(zip(_cols, _fetched)) if _fetched else None
            cur.execute("""
                INSERT INTO atm_locations
                    (atm_id, atm_name, branch, district, city, region, latitude, longitude,
                     terminal_id, vendor, model, install_date, status,
                     ip_address, snmp_community, snmp_version, ej_log_format)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    ej_log_format = COALESCE(EXCLUDED.ej_log_format, atm_locations.ej_log_format)
            """, (
                data['atm_id'], data['atm_name'], data['branch'], data['district'], data['city'],
                data['region'], data['latitude'], data['longitude'],
                data['terminal_id'], data['vendor'], data['model'],
                data['install_date'], data['status'],
                data.get('ip_address'), data.get('snmp_community'),
                data.get('snmp_version'), data.get('ej_log_format')
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
            # After-image: re-read the effective row (COALESCE logic means the
            # stored row can differ from the submitted form) and diff it.
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
            if is_ajax:
                return jsonify({'error': True, 'errors': db_err, 'message': 'Database error. Please try again.'})
            flash(f'Database error: {err_msg}', 'error')
            return redirect('/admin/atm')
        finally:
            cur.close()

    if is_ajax:
        log_action('ATM_SAVE', audit_detail)
        threading.Thread(target=_bg_zabbix_sync, args=([dict(data)],), daemon=True).start()
        return jsonify({'success': True, 'atm_id': data['atm_id']})

    log_action('ATM_SAVE', audit_detail)
    threading.Thread(target=_bg_zabbix_sync, args=([dict(data)],), daemon=True).start()
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
        for key, tbl in (('txn', 'atm_transactions'), ('anom', 'atm_anomalies'),
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


@bp.route('/atm/delete', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def atm_delete():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    atm_id = request.form.get('atm_id', '').strip()
    if not atm_id:
        if is_ajax:
            return jsonify({'error': True, 'message': 'ATM ID is required.'})
        flash('ATM ID is required for deletion.', 'error')
        return redirect('/admin/atm')

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
                return redirect('/admin/atm')
            conn.commit()
        except Exception as e:
            conn.rollback()
            if is_ajax:
                return jsonify({'error': True, 'message': f'Database error: {str(e)}'})
            flash(f'Database error: {str(e)}', 'error')
            return redirect('/admin/atm')
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
    return redirect('/admin/atm')


@bp.route('/atm/bulk-delete', methods=['POST'])
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


@bp.route('/atm/check', methods=['POST'])
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_check():
    data = request.get_json(silent=True)
    if not data or 'ids' not in data:
        return jsonify({'existing': []})
    ids = [i.strip() for i in data['ids'] if i.strip()]
    if not ids:
        return jsonify({'existing': []})
    with get_db() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(ids))
        cur.execute(f"SELECT atm_id FROM atm_locations WHERE atm_id IN ({placeholders})", ids)
        existing = [r[0] for r in cur.fetchall()]
        cur.close()
    return jsonify({'existing': existing})


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
        return redirect('/admin/cash')
    if not _take_cash_token(request.form.get('cash_token', '')):
        # Double-click, back-button resubmit, or retried POST: the first one
        # already committed. Never insert twice; say so plainly.
        flash('Already logged — duplicate submission ignored.', 'success')
        return redirect(f'/admin/cash?atm_id={atm_id}' if atm_id else '/admin/cash')
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM atm_locations WHERE atm_id = %s", (atm_id,))
            if not cur.fetchone():
                flash(f'Not saved: unknown ATM {atm_id}.', 'error')
                return redirect('/admin/cash')
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
            return redirect('/admin/cash')
        finally:
            cur.close()
    flash(f'Logged: {notes_added} notes into {atm_id} cassette {cassette}.', 'success')
    return redirect(f'/admin/cash?atm_id={atm_id}')


@bp.route('/atm/csv')
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


@bp.route('/atm/template')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def atm_csv_template():
    """Blank import template (header + examples) — generated from the live
    required/optional column set so it can never drift from validation."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['atm_id', 'atm_name', 'branch', 'district', 'city', 'region',
                'latitude', 'longitude', 'terminal_id', 'vendor', 'model',
                'install_date', 'status',
                'ip_address', 'snmp_community', 'snmp_version', 'ej_log_format'])
    w.writerow(['ATM-1001', 'Bole Medhanialem ATM', 'Bole Medhanialem Branch', 'Bole',
                'Addis Ababa', 'Addis Ababa', '8.9950', '38.7896', 'TID10001',
                'NCR', 'SelfServ 22', '2023-03-15', 'active',
                '192.168.1.101', 'CHANGE_ME', '2', 'NCR_APTRA'])
    w.writerow(['ATM-1002', 'Piassa ATM', 'Piassa Branch', 'Arada',
                'Addis Ababa', 'Addis Ababa', '9.0356', '38.7521', 'TID10002',
                'GRG', 'H68N', '2022-11-02', 'active',
                '192.168.1.102', 'CHANGE_ME', '2', 'GRG'])
    log_action('EXPORT', 'ATM import template downloaded')
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=atm_import_template.csv'}
    )


@bp.route('/atm/import', methods=['POST'])
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
    required = ['atm_id', 'branch', 'district', 'city', 'region',
                'terminal_id', 'vendor', 'model', 'status']
    missing_cols = [c for c in required if c not in reader.fieldnames]
    if missing_cols:
        return jsonify({'error': True, 'message': f'Missing required columns: {", ".join(missing_cols)}'})

    rows = []
    errors = []
    line = 1
    for row in reader:
        line += 1
        if 'branch' in row:
            row['branch'] = _norm_branch(row.get('branch'))
        errs = _validate(row)
        if errs:
            errors.append({'line': line, 'atm_id': row.get('atm_id', '?'), 'errors': errs})
        else:
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
                             ip_address, snmp_community, snmp_version, ej_log_format)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                            ej_log_format = COALESCE(EXCLUDED.ej_log_format, atm_locations.ej_log_format)
                    """, (
                        _nn(row.get('atm_id')), name, _nn(row.get('branch')), _nn(row.get('district')),
                        _nn(row.get('city')), _nn(row.get('region')), _nn(row.get('latitude')),
                        _nn(row.get('longitude')), _nn(row.get('terminal_id')),
                        _nn(row.get('vendor')), _nn(row.get('model')),
                        _nn(row.get('install_date')), _nn(row.get('status')),
                        _nn(row.get('ip_address')), _nn(row.get('snmp_community')),
                        _nn(row.get('snmp_version')), _nn(row.get('ej_log_format'))
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


@bp.route('/atm/zabbix-sync', methods=['POST'])
@login_required
@role_required(ROLE_OPERATOR, ROLE_ADMIN)
def atm_zabbix_sync():
    """On-demand server-side sync: create missing Zabbix hosts for given (or all) ATMs."""
    from zabbix_sync import MAX_BATCH, sync_hosts
    data = request.get_json(silent=True) or {}
    ids = [str(i).strip() for i in (data.get('ids') or []) if str(i).strip()][:MAX_BATCH]
    with get_db() as conn:
        cur = conn.cursor()
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"""SELECT atm_id, atm_name, ip_address, vendor,
                                   snmp_community, snmp_version, district
                            FROM atm_locations WHERE atm_id IN ({placeholders})""", ids)
        else:
            cur.execute(f"""SELECT atm_id, atm_name, ip_address, vendor,
                                   snmp_community, snmp_version, district
                            FROM atm_locations WHERE status = 'active'
                            ORDER BY atm_id LIMIT {MAX_BATCH}""")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
    summary = sync_hosts(rows)
    log_action('ZABBIX_SYNC', f'manual: created={summary["created"]} reenabled={summary.get("reenabled", 0)} macros={summary.get("macros_fixed", 0)} updated={summary.get("updated", 0)} '
                           f'skipped={summary["skipped"]} no_ip={summary["no_ip"]} failed={summary["failed"]} (n={len(rows)})')
    return jsonify({'success': True, 'checked': len(rows), **summary})


@bp.route('/atm/zabbix-status')
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
            errors[field] = 'This field is required'
    if data.get('atm_id') and len(data['atm_id']) < 3:
        errors['atm_id'] = 'ATM ID must be at least 3 characters'
    if data.get('atm_id') and not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', data['atm_id']):
        errors['atm_id'] = 'Letters, digits, dash and underscore only'
    maxlens = {'atm_id': 20, 'branch': 100, 'district': 100, 'city': 100,
               'region': 100, 'terminal_id': 20, 'vendor': 50, 'model': 50,
               'ip_address': 45, 'snmp_community': 100, 'ej_log_format': 20}
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

@bp.route('/audit')
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

@bp.route('/schedules')
@login_required
@role_required(ROLE_ADMIN)
def schedule_list():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, report_type, format, schedule, recipients, enabled, last_run, last_status, last_error FROM scheduled_reports ORDER BY id")
            rows = cur.fetchall()
            cur.close()
        rows = [list(r) for r in rows]
        for r in rows:
            r[7] = to_eat(r[7], '%Y-%m-%d %H:%M')
    except Exception:
        rows = []
        failed = []
    failed = [r for r in rows if len(r) > 8 and r[8] == 'failed']
    return render_template('admin_schedules.html', schedules=rows, failed=failed,
                           type_labels=REPORT_TYPE_LABELS)


def _valid_cron(expr):
    """Validate a 5-field cron (minute hour dom month dow). Returns an error
    string or None. Mirrors scheduler._cron_field semantics so anything saved
    here can actually fire there."""
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    parts = (expr or '').split()
    if len(parts) != 5:
        return 'Cron must have 5 fields: minute hour day month weekday.'
    for part, (lo, hi) in zip(parts, bounds):
        if part == '*':
            continue
        for item in part.split(','):
            step = None
            if '/' in item:
                item, step = item.split('/', 1)
                if not step.isdigit() or int(step) < 1:
                    return f'Bad cron step in {part!r}.'
            if item in ('*', ''):
                if not step and item == '':
                    return f'Bad cron field {part!r}.'
                continue
            if '-' in item:
                a, _, b = item.partition('-')
                if not (a.isdigit() and b.isdigit() and lo <= int(a) <= int(b) <= hi):
                    return f'Bad cron range {item!r} (allowed {lo}-{hi}).'
            elif not item.isdigit() or not (lo <= int(item) <= hi):
                return f'Bad cron value {item!r} (allowed {lo}-{hi}).'
    return None


@bp.route('/schedules/save', methods=['POST'])
@login_required
@role_required(ROLE_ADMIN)
def schedule_save():
    name = request.form.get('name', '').strip()
    report_type = request.form.get('report_type', '').strip()
    fmt = request.form.get('format', 'pdf').strip()
    schedule = request.form.get('schedule', '').strip()
    recipients = request.form.get('recipients', '').strip()
    params = request.form.get('params', '').strip()
    schedule_id = request.form.get('id', '').strip()

    if not all([name, report_type, schedule, recipients]):
        flash('Name, report type, schedule, and recipients are required.', 'error')
        return redirect('/admin/schedules')

    bad = [r.strip() for r in recipients.split(',') if r.strip() and not _valid_email(r.strip())]
    if bad:
        flash(f'Invalid email address(es): {", ".join(bad)}', 'error')
        return redirect('/admin/schedules')

    cron_err = _valid_cron(schedule)
    if cron_err:
        flash(f'Invalid schedule: {cron_err}', 'error')
        return redirect('/admin/schedules')

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


@bp.route('/schedules/toggle', methods=['POST'])
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


@bp.route('/schedules/delete', methods=['POST'])
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


@bp.route('/schedules/send-now', methods=['POST'])
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


@bp.route('/schedules/get')
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
    import re
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v or ''))


@bp.route('/users')
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


@bp.route('/users/emails')
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


@bp.route('/users/create', methods=['POST'])
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


@bp.route('/users/edit', methods=['POST'])
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


@bp.route('/users/delete', methods=['POST'])
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
    <li><strong>Filebeat</strong> — already watches <code>ej-logs/ATM-*.log</code> with a wildcard.<br>
        The moment a file named <code>ej-logs/{atm_id_val}.log</code> exists, Filebeat ships it to OpenSearch automatically.</li>
    <li><strong>EJ Search</strong> — will show {atm_id_val} data as soon as logs are indexed.</li>
    <li><strong>ATM dropdown</strong> — already shows {atm_id_val} in all search filters.</li>
  </ul>

  <h2>Real ATM (production) <span class="badge-warn">&#9888; Action required</span></h2>
  <p>Configure the ATM's EJ log delivery (agent, SFTP, or shared folder) to write files into <code>ej-logs/{atm_id_val}.log</code>.
  Filebeat will detect and ship the file automatically — no further configuration needed.</p>

  <a href="/admin/atm" class="btn">&#8592; Back to ATM Management</a>
</div></body></html>"""
