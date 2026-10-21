"""Reports use an explicit EAT period and a custom picker, not native date inputs."""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
PORTAL = REPO / 'report-portal'


def _read(*parts):
    return PORTAL.joinpath(*parts).read_text()


ROUTES = _read('routes.py')
HELPERS = _read('helpers.py')
PAGE = _read('templates', 'reports.html')
PORTAL_BP = _read('blueprints', 'portal.py')


def test_every_historical_report_uses_a_window():
    assert ROUTES.count('win = _window()') == 7
    for name in ('transaction', 'cash', 'error', 'performance', 'vendor',
                 'availability', 'full'):
        assert f'def report_{name}(fmt):' in ROUTES


def test_historical_sql_uses_absolute_utc_bounds():
    assert 'NOW() - INTERVAL %s' not in ROUTES
    assert 't.recorded_at >= %s' in ROUTES
    assert 't.recorded_at < %s' in ROUTES


def test_availability_is_coverage_aware():
    assert 'first_state' in ROUTES
    assert 'coverage_pct' in ROUTES
    assert '"Coverage %"' in ROUTES
    assert 'No data' in ROUTES


def test_helpers_can_show_the_selected_period():
    assert 'period=None' in HELPERS
    assert 'period or' in HELPERS
    assert 'Period: {period_label}' in HELPERS


def test_outage_is_still_a_current_snapshot():
    assert "period='Current snapshot'" in ROUTES


def test_reports_page_uses_a_shared_custom_picker():
    assert (PORTAL / 'static' / 'flatpickr.min.js').is_file()
    assert (PORTAL / 'static' / 'flatpickr.min.css').is_file()
    assert 'flatpickr.min.js' in PAGE
    assert 'id="report-start"' in PAGE
    assert 'id="report-end"' in PAGE
    assert 'data-range="7d"' in PAGE
    assert 'type="date"' not in PAGE
    for old in ('full-days', 'txn-days', 'cash-days', 'error-days',
                'perf-days', 'vendor-days', 'avail-days'):
        assert old not in PAGE


def test_generated_url_carries_start_and_end():
    assert "params.set('start'" in PAGE
    assert "params.set('end'" in PAGE
    assert '?days=' not in PAGE


def test_exports_carry_the_period_into_the_filename():
    assert '_period_slug(period)' in HELPERS
    assert 'period=win.label' in ROUTES
    assert '_period_slug(win.label)' in ROUTES


def test_reports_route_supplies_eat_defaults():
    assert 'default_start' in PORTAL_BP
    assert 'default_end' in PORTAL_BP
    assert 'timedelta(days=6)' in PORTAL_BP
