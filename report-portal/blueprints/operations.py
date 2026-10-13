"""
blueprints/operations.py
In-portal Operations dashboards — Transaction Monitoring + KPI essentials
rendered natively from Postgres (Phase 2 of single-login UX).

Same queries Grafana runs today (config/grafana/dashboards/*), so the
numbers match what admins see in Grafana. Every query is defensive:
a missing table yields zeros, never a 500.
"""
import logging
import json
import os
import decimal
import datetime as _dt
from flask import Blueprint, render_template, request, jsonify
import livecache
from blueprints.auth import login_required, role_required, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN
from db import get_db

logger = logging.getLogger(__name__)

bp = Blueprint('operations', __name__, url_prefix='/operations')

# Fleet tables render 50 rows per page. The map and stat cards always use
# the FULL fleet — only the HTML tables are sliced, so scaling to 1200
# ATMs shrinks the page without changing any number on it.
FLEET_PER_PAGE = 50


def _page_param(name):
    try:
        return max(1, int(request.args.get(name, 1)))
    except (ValueError, TypeError):
        return 1


def _paginate(rows, page, per_page=FLEET_PER_PAGE):
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    return rows[start:start + per_page], page, total_pages

# Offline Ethiopia outline (simplified GADM, vendored in static/) so the fleet
# map works on the bank network with zero tile/CDN dependencies.
_ETH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'static', 'ethiopia.json')
_ETH_GEO = None
try:
    with open(_ETH_PATH) as _f:
        _ETH_GEO = json.load(_f)
except Exception as _e:
    logger.warning('Ethiopia outline unavailable: %s', _e)


def _project(lng, lat, w=640.0, h=460.0, pad=24.0):
    """Equirectangular fit of the country bbox into a w×h viewBox."""
    import math
    xs = [p[0] for poly in _ETH_GEO['coordinates'] for ring in poly for p in ring]
    ys = [p[1] for poly in _ETH_GEO['coordinates'] for ring in poly for p in ring]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    meany = math.radians((miny + maxy) / 2.0)
    kx = math.cos(meany)
    sx = (w - 2 * pad) / ((maxx - minx) * kx)
    sy = (h - 2 * pad) / (maxy - miny)
    s = min(sx, sy)
    ox = (w - (maxx - minx) * kx * s) / 2.0
    oy = (h - (maxy - miny) * s) / 2.0
    return ox + (lng - minx) * kx * s, oy + (maxy - lat) * s


def _ethiopia_svg_path(w=640.0, h=460.0, pad=24.0):
    if not _ETH_GEO:
        return ''
    parts = []
    for poly in _ETH_GEO['coordinates']:
        for ring in poly:
            pts = [_project(p[0], p[1], w, h, pad) for p in ring]
            parts.append('M' + 'L'.join(f'{x:.1f},{y:.1f}' for x, y in pts) + 'Z')
    return ' '.join(parts)


def _one(sql, params=()):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            cur.close()
            return row
    except Exception as e:
        logger.warning('operations query failed: %s', e)
        return None


def _all(sql, params=()):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
            return rows
    except Exception as e:
        logger.warning('operations query failed: %s', e)
        return []


@bp.route('')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def dashboard():
    d = _collect_cached()
    return render_template('operations.html',
                           cash_today=d['cash_today'], success_rate=d['success_rate'],
                           unacked=d['unacked'], fleet=d['fleet_page'], feed=d['feed'],
                           summary=d['summary_page'], chart=d['chart'],
                           fleet_pg=d['fleet_pg'], fleet_pages=d['fleet_pages'],
                           fleet_total=d['fleet_total'],
                           sum_pg=d['sum_pg'], sum_pages=d['sum_pages'],
                           map_svg=_ethiopia_svg_path(), map_dots=d['map_dots'],
                           map_missing=d['map_missing'])


@bp.route('/api/live')
@login_required
@role_required(ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
def api_live():
    """Pollable twin of dashboard() for the 30s autosync (same shaping)."""
    d = _collect_cached()
    return jsonify({
        'cash_today': d['cash_today'], 'success_rate': d['success_rate'],
        'unacked': d['unacked'],
        'fleet': _jclean(d['fleet_page']), 'fleet_pg': d['fleet_pg'],
        'fleet_pages': d['fleet_pages'], 'fleet_total': d['fleet_total'],
        'feed': _jclean(d['feed']),
        'summary': _jclean(d['summary_page']),
        'sum_pg': d['sum_pg'], 'sum_pages': d['sum_pages'],
        'chart': d['chart'], 'map_dots': d['map_dots'],
    })


def _collect_cached():
    # Shared across viewers (25s TTL under the 30s frontend poll). Keyed by
    # page: different viewers on different pages must not share slices.
    return livecache.get_or_compute(
        f'ops:{_page_param("page")}:{_page_param("spage")}', 25, _collect)


def _jclean(rows):
    """Decimal/datetime → JSON-safe (Flask jsonify chokes on Decimal)."""
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if isinstance(v, decimal.Decimal):
                v = float(v)
            elif isinstance(v, (_dt.datetime, _dt.date)):
                v = str(v)
            d[k] = v
        out.append(d)
    return out


def _collect():
    # ── Stat row ──
    # EAT day boundary: CURRENT_DATE is DB (UTC) midnight = 03:00 EAT, which
    # misattributes the 00:00–03:00 window. EAT midnight as a UTC instant is
    # what "today" means everywhere else in the UI.
    cash = _one("""SELECT COALESCE(SUM(amount), 0) FROM atm_transactions
                   WHERE status = 'APPROVED' AND txn_type = 'WITHDRAWAL'
                   AND recorded_at >= date_trunc('day', NOW() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'""")
    cash_today = float(cash[0]) if cash and cash[0] is not None else 0.0

    rate = _one("""SELECT ROUND(100.0 * SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END)
                   / NULLIF(COUNT(*), 0), 2) FROM atm_transactions
                   WHERE recorded_at >= NOW() - INTERVAL '1 hour'""")
    success_rate = float(rate[0]) if rate and rate[0] is not None else None

    unacked = _one("""SELECT COUNT(*) FROM atm_anomalies
                      WHERE acknowledged = FALSE
                      AND detected_at >= NOW() - INTERVAL '60 minutes'""")
    unacked_count = int(unacked[0]) if unacked else 0

    # ── Fleet overview (health per ATM, last 1h) ──
    fleet = _all("""SELECT l.atm_id, l.branch, l.city, l.vendor,
                           l.latitude, l.longitude,
                           COALESCE(s.state, 'UNKNOWN') AS state,
                           COALESCE(t.total, 0) AS txns,
                           COALESCE(t.approved, 0) AS approved,
                           COALESCE(t.success_rate, 0) AS success_rate,
                           COALESCE(t.cash_dispensed, 0) AS cash,
                           CASE WHEN t.atm_id IS NULL THEN 'No Data'
                                WHEN t.success_rate >= 95 THEN 'Healthy'
                                WHEN t.success_rate >= 80 THEN 'Degraded'
                                ELSE 'Critical' END AS health
                    FROM atm_locations l LEFT JOIN (
                      SELECT atm_id, COUNT(*) AS total,
                        SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                        ROUND(100.0 * SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END)
                          / NULLIF(COUNT(*), 0), 1) AS success_rate,
                        SUM(CASE WHEN status = 'APPROVED' AND txn_type = 'WITHDRAWAL'
                          THEN amount ELSE 0 END) AS cash_dispensed
                      FROM atm_transactions
                      WHERE recorded_at >= NOW() - INTERVAL '1 hour'
                      GROUP BY atm_id) t ON t.atm_id = l.atm_id
                    LEFT JOIN atm_current_state s ON s.atm_id = l.atm_id
                    ORDER BY l.atm_id""")

    # ── Live feed + 24h summary ──
    feed = _all("""SELECT recorded_at, atm_id, branch, txn_type, card_masked,
                          amount, status FROM atm_transactions
                   ORDER BY recorded_at DESC LIMIT 50""")
    summary = _all("""SELECT atm_id, COUNT(*) AS total,
                        SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                        SUM(CASE WHEN status = 'DECLINED' THEN 1 ELSE 0 END) AS declined,
                        SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS errors
                      FROM atm_transactions
                      WHERE recorded_at >= NOW() - INTERVAL '24 hours'
                      GROUP BY atm_id ORDER BY atm_id""")

    # ── Per-hour chart data (last 12h) ──
    # EAT buckets: date_trunc on raw timestamptz groups by DB-session (UTC)
    # hours, shifting every bar 3h behind wall time. Convert first (same
    # pattern as the cash_today EAT-midnight boundary above) — bare UTC
    # labels here were the reported bug (2026-10-12).
    hourly = _all("""SELECT date_trunc('hour', recorded_at AT TIME ZONE 'Africa/Addis_Ababa') AS h,
                        SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                        SUM(CASE WHEN status != 'APPROVED' THEN 1 ELSE 0 END) AS failed
                     FROM atm_transactions
                     WHERE recorded_at >= NOW() - INTERVAL '12 hours'
                     GROUP BY 1 ORDER BY 1""")
    chart = {
        'labels': [str(r['h'])[11:16] for r in hourly],
        'approved': [int(r['approved'] or 0) for r in hourly],
        'failed': [int(r['failed'] or 0) for r in hourly],
    }

    for r in feed:
        if r.get('recorded_at') is not None:
            r['recorded_at'] = str(r['recorded_at'])[:19]

    # Paginate the per-ATM tables only — map_dots below still uses the full
    # fleet, and all stat cards/summary counts are computed above.
    fleet_page, fleet_pg, fleet_pages = _paginate(fleet, _page_param('page'))
    summary_page, sum_pg, sum_pages = _paginate(summary, _page_param('spage'))

    # ── Fleet map dots (same health coloring as Grafana's geomap) ──
    map_dots = []
    map_missing = 0
    if _ETH_GEO:
        _order = {'Critical': 0, 'Degraded': 1, 'No Data': 2, 'Healthy': 3}
        for r in sorted(fleet, key=lambda x: _order.get(x.get('health'), 4)):
            try:
                lat = float(r['latitude']) if r.get('latitude') is not None else None
                lng = float(r['longitude']) if r.get('longitude') is not None else None
            except (ValueError, TypeError):
                lat, lng = None, None
            if lat is None or lng is None:
                map_missing += 1
                continue
            x, y = _project(lng, lat)
            map_dots.append({
                'atm_id': r['atm_id'], 'branch': r.get('branch') or r['atm_id'],
                'city': r.get('city') or '', 'health': r.get('health'),
                'state': r.get('state'), 'txns': r.get('txns', 0),
                'lat': lat, 'lng': lng,
                'x': round(x, 1), 'y': round(y, 1),
            })

    return {'cash_today': cash_today, 'success_rate': success_rate,
            'unacked': unacked_count, 'fleet_page': fleet_page, 'feed': feed,
            'summary_page': summary_page, 'chart': chart,
            'fleet_pg': fleet_pg, 'fleet_pages': fleet_pages, 'fleet_total': len(fleet),
            'sum_pg': sum_pg, 'sum_pages': sum_pages,
            'map_dots': map_dots, 'map_missing': map_missing}
