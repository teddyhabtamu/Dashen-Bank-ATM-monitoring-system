"""Service-mode windows for one ATM: payload, routes, and UI.

The API call itself is not mocked — the parts that can be wrong without a
live Zabbix are the window arithmetic, the one-host scope, the fail-closed
read before a write, and the route that ends a window. Those are covered here.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORTAL = ROOT / 'report-portal'
sys.path.insert(0, str(PORTAL))

from maintenance import (active_by_host, active_window, create_params,  # noqa: E402
                         options, window)


def _read(*parts):
    return PORTAL.joinpath(*parts).read_text()


ADMIN = _read('blueprints', 'admin.py')
MON = _read('blueprints', 'monitoring.py')
DETAIL = _read('templates', 'admin_atm_detail.html')
MC = _read('templates', 'monitoring_center.html')

START = ADMIN[ADMIN.index('def atm_maintenance_start'):ADMIN.index('def atm_maintenance_end')]
END = ADMIN[ADMIN.index('def atm_maintenance_end'):ADMIN.index("@bp.route('/atms/get')")]


# ── Window arithmetic ─────────────────────────────────────────────────────

def test_window_is_minute_aligned_and_ends_after_the_period():
    since, till, period = window('30', 1700000123)
    assert since == 1700000100
    assert till - since == 30 * 60 == period


def test_operator_cannot_schedule_a_day():
    with pytest.raises(ValueError):
        window('1440', 1700000000, admin=False)
    since, till, _ = window('1440', 1700000000, admin=True)
    assert till - since == 24 * 60 * 60


def test_the_form_never_offers_an_operator_a_day():
    assert '1440' not in [k for k, _ in options(False)]
    assert '1440' in [k for k, _ in options(True)]


def test_unknown_duration_is_refused():
    with pytest.raises(ValueError):
        window('999', 1700000000)


# ── maintenance.create payload ────────────────────────────────────────────

def test_create_scopes_exactly_one_host_and_keeps_collecting():
    p = create_params('1042', 'ATM-001', 'Cash replenishment', '120',
                      1700000123, admin=False, actor='operator1')
    assert p['hosts'] == [{'hostid': '1042'}]
    assert p['maintenance_type'] == 0  # with data collection
    assert p['timeperiods'][0]['timeperiod_type'] == 0  # one time only
    assert p['timeperiods'][0]['start_date'] == p['active_since']
    assert p['timeperiods'][0]['period'] == 120 * 60
    assert p['active_till'] - p['active_since'] == 120 * 60
    assert 'operator1' in p['description']
    assert 'ATM-001' in p['name'] and 'Cash replenishment' in p['name']


def test_reason_is_required_and_flattened():
    with pytest.raises(ValueError):
        create_params('1', 'ATM', '  no  ', '30', 1700000000)
    p = create_params('1', 'ATM', '  disk\nswap  ', '30', 1700000000)
    assert p['name'].endswith('disk swap')
    assert '\n' not in p['name']


def test_name_cannot_overflow_the_zabbix_column():
    p = create_params('1', 'ATM-001', 'x' * 300, '30', 1700000000)
    assert len(p['name']) <= 128


# ── Reading the current state ─────────────────────────────────────────────

def test_active_window_checks_time_and_host():
    m = {'maintenanceid': '7', 'active_since': '100', 'active_till': '200',
         'hosts': [{'hostid': '42'}]}
    assert active_window([m], '42', 100) is m
    assert active_window([m], '42', 199) is m
    assert active_window([m], '42', 200) is None
    assert active_window([m], '42', 99) is None
    assert active_window([m], '43', 150) is None
    assert active_window([], '42', 150) is None


def test_active_by_host_maps_every_host():
    m = {'maintenanceid': '7', 'active_since': '100', 'active_till': '200',
         'hosts': [{'hostid': '42'}, {'hostid': '43'}]}
    out = active_by_host([m], 150)
    assert out['42'] is m and out['43'] is m
    assert active_by_host([m], 250) == {}


# ── Routes ────────────────────────────────────────────────────────────────

def test_service_mode_routes_are_post_and_operator_gated():
    for path in ('/atms/<atm_id>/maintenance/start',
                 '/atms/<atm_id>/maintenance/end'):
        i = ADMIN.index(path)
        block = ADMIN[i:i + 260]
        assert "methods=['POST']" in block
        assert '@role_required(ROLE_OPERATOR, ROLE_ADMIN)' in block


def test_start_fails_closed_when_the_current_state_is_unreadable():
    assert 'if existing is None:' in START
    assert 'no service window was started' in START


def test_start_refuses_a_duplicate_window():
    assert 'if active_window(existing, hostid, time.time()):' in START
    assert 'already in a service window' in START


def test_start_revalidates_the_duration_on_the_server():
    # The select is a convenience; the cap is enforced here.
    assert 'window(duration, time.time(), admin=is_admin)' in START


def test_start_creates_one_host_window_and_audits_it():
    assert "'maintenance.create', params" in START
    assert "log_action('MAINTENANCE_START'" in START
    assert "'MAINTENANCE_START_FAILED'" in START


def test_end_deletes_the_resolved_window_not_a_posted_id():
    assert "'maintenance.delete', [mid]" in END
    assert "request.form.get('maintenanceid')" not in END
    assert 'active_window(rows, hostid, time.time())' in END
    assert "log_action('MAINTENANCE_END'" in END


def test_the_detail_page_uses_the_role_aware_duration_list():
    assert "maintenance_options(session.get('role') == ROLE_ADMIN)" in ADMIN


# ── UI ────────────────────────────────────────────────────────────────────

def test_detail_page_offers_start_and_end_only_to_operators():
    assert 'Service mode' in DETAIL
    assert 'name="csrf_token"' in DETAIL
    assert 'action="/atms/{{ atm.atm_id }}/maintenance/start"' in DETAIL
    assert 'action="/atms/{{ atm.atm_id }}/maintenance/end"' in DETAIL
    assert '{% if can_maintain %}' in DETAIL
    assert '{% elif can_maintain and maintenance_hostid %}' in DETAIL
    assert 'minlength="5"' in DETAIL
    assert 'Problems for this ATM are hidden, not resolved' in DETAIL
    assert 'Only operators and admins can start service mode.' in DETAIL


def test_a_failed_read_is_not_rendered_as_no_maintenance():
    assert '{% if maintenance_error %}' in DETAIL
    assert "_atm_maintenance" in ADMIN
    assert "'Maintenance state is unavailable right now.'" in ADMIN


# ── Monitoring Center ────────────────────────────────────────────────────

def test_maintenance_is_read_in_parallel_and_cannot_discard_the_page():
    fn = MON[MON.index('def _cached_zabbix'):MON.index('def _problem_get')]
    assert 'ThreadPoolExecutor(max_workers=3)' in fn
    assert "'maintenance.get'" in fn
    assert 'fm.result(timeout=12) or []' in fn
    assert 'maintenance.get exceeded the 12s budget' in fn
    assert 'maintenances=maintenances' in fn
    assert "return _cache['hosts'], _cache['problems'], _cache['maintenances']" in fn


def test_hosts_show_an_active_window():
    assert '<th>Maintenance</th>' in MC
    assert 'h.maintenance_until' in MC
    assert 'h.maintenance_name' in MC
    assert 'active_by_host(raw_maintenances' in MON
    assert "maintenance_until': _fmt_short(m.get('active_till')) if m else ''" in MON
    assert 'colspan="6"' in MC
