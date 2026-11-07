"""Reports use an explicit EAT period and a custom picker, not native date inputs."""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
PORTAL = REPO / 'report-portal'


def _read(*parts):
    return PORTAL.joinpath(*parts).read_text()


ROUTES = _read('routes.py')
HELPERS = _read('helpers.py')
PAGE = _read('templates', 'reports.html')
BASE = _read('templates', 'base.html')
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
    assert '"Coverage %"' not in ROUTES
    assert "'Coverage': 'Coverage (%)'" in ROUTES
    assert 'No data' in ROUTES


def test_no_literal_percent_signs_in_parameterised_sql():
    import ast
    tree = ast.parse(ROUTES)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, 'id', None) == '_AVAIL_SQL' for t in node.targets):
            sql = ast.literal_eval(node.value)
            assert sql.count('%') == sql.count('%s')
            break
    else:
        raise AssertionError('_AVAIL_SQL not found')


def test_helpers_can_show_the_selected_period():
    assert 'period=None' in HELPERS
    assert 'period or' in HELPERS
    assert 'Period: {period_label}' in HELPERS


def test_outage_is_still_a_current_snapshot():
    assert "period='Current snapshot'" in ROUTES


def test_outage_report_bounds_both_per_atm_lookups():
    """Each down ATM used to scan its whole transaction history twice.

    With ~1,200 imported ATMs that exceeded the 30s statement timeout, so the
    scheduled Daily ATM Outage PDF failed every run. Both laterals must stay
    inside a time window the (atm_id, recorded_at) index can serve.
    """
    start = ROUTES.index('def report_outage(fmt):')
    block = ROUTES[start:ROUTES.index('headers = [', start)]

    assert block.count("NOW() - INTERVAL '7 days'") == 2
    err = block.index("t.status = 'ERROR'")
    wd = block.index("t.status = 'APPROVED'")
    assert 'NOW() - INTERVAL' in block[err:wd]
    assert 'NOW() - INTERVAL' in block[wd:block.index(') w ON true', wd)]


def test_outage_report_has_a_matching_composite_index():
    script = (REPO / 'scripts' / 'add_perf_indexes.py').read_text()
    assert 'idx_atm_txn_atm_status_type_time' in script
    assert 'ON atm_transactions (atm_id, status, txn_type, recorded_at DESC)' in script
    assert 'CREATE INDEX CONCURRENTLY' in script


def test_the_report_atm_pickers_are_labelled_atm_not_branch():
    """The value sent to the backend is an ATM id, not a branch."""
    assert '<label for="txn-atm">ATM</label>' in PAGE
    assert '<label for="avail-atm">ATM</label>' in PAGE
    assert PAGE.count('<option value="all" selected>All ATMs</option>') == 2


def test_the_report_atm_list_goes_through_the_shared_searchable_select():
    # The page used to rebuild .cust-opt by hand, which bypassed the shared
    # enhancer and left the 1,200-item ATM list without a search box.
    assert 'window.refreshCustomSelect(sel)' in PAGE
    assert "drop.innerHTML = ''" not in PAGE


def test_a_select_populated_after_boot_gains_the_search_box():
    start = BASE.index('function refreshCustomSelect(sel){')
    refresh = BASE[start:BASE.index('window.refreshCustomSelect = refreshCustomSelect;', start)]
    assert 'custEnsureSearch(sel);' in refresh

    start = BASE.index('function initCustomSelects(){')
    init = BASE[start:BASE.index('initCustomSelects();', start)]
    assert 'custEnsureSearch(sel);' in init
    assert "searchInput.placeholder='Search…'" in BASE


def test_range_presets_apply_bounds_before_dates():
    """Clicking Today after Last month used to blank the start date.

    flatpickr filters a date against the *old* maxDate and rewrites the input
    when it falls outside. Bounds must therefore move first; the values are
    written last so the pickers cannot clear them.
    """
    start = PAGE.index('function setReportRange(start, end) {')
    fn = PAGE[start:PAGE.index('if (typeof flatpickr', start)]

    assert fn.index("startPicker.set('maxDate'") < fn.index('startPicker.setDate')
    assert fn.index("endPicker.set('minDate'") < fn.index('endPicker.setDate')
    assert fn.rindex('reportStart.value = start') > fn.index('startPicker.setDate')
    assert fn.rindex('reportEnd.value = end') > fn.index('endPicker.setDate')


def test_sync_bounds_cannot_blank_a_manually_picked_date():
    start = PAGE.index('function syncPickerBounds() {')
    fn = PAGE[start:PAGE.index('function rangeDays()', start)]

    assert 'if (s) reportStart.value = s;' in fn
    assert 'if (e) reportEnd.value = e;' in fn
    assert fn.index('if (s) reportStart.value = s;') > fn.index("startPicker.set('maxDate'")


def test_every_report_card_has_a_description():
    """A card with no body next to a card with a paragraph reads unfinished."""
    assert PAGE.count('class="card-desc"') == 7
    assert PAGE.count('class="card-note"') == 1


def test_report_titles_drop_the_redundant_suffix():
    for title in ('Daily ATM Outage', 'Transaction Summary', 'Cash Levels',
                  'Errors &amp; Incidents', 'ATM Performance',
                  'Vendor Performance', 'ATM Availability'):
        assert f'<h3>{title}</h3>' in PAGE
    assert 'Vendor Performance Report</h3>' not in PAGE


def test_card_actions_bottom_align_across_a_row():
    assert '.card-body-inner { display: flex; flex-direction: column; flex: 1; }' in PAGE
    actions = PAGE[PAGE.index('.card-actions {'):PAGE.index('}', PAGE.index('.card-actions {'))]
    assert 'margin-top: auto;' in actions


def test_date_presets_show_which_one_is_active():
    assert '.range-chip.active {' in PAGE
    assert 'setActivePreset(this);' in PAGE
    assert 'clearActivePreset(); syncPickerBounds();' in PAGE
    assert 'aria-pressed' in PAGE


def test_the_page_links_to_scheduled_reports():
    assert 'href="/admin/schedules"' in PAGE
    assert 'Schedule reports' in PAGE


def test_an_open_dropdown_keeps_its_card_above_the_next_one():
    """The shared select list is inside the card's stacking context.

    Without raising the card while the list is open, the next card painted
    over the lower options as soon as the pointer left the trigger.
    """
    assert '.report-card.select-open' in PAGE
    assert 'custRaise(sel, true);' in BASE
    assert 'custLowerAll();' in BASE


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


def test_scheduled_reports_support_calendar_presets():
    sched = (PORTAL / 'scheduler.py').read_text()
    sched_page = (PORTAL / 'templates' / 'admin_schedules.html').read_text()
    assert 'schedule_period_window' in sched
    assert "query['end_now'] = '1'" in sched
    assert 'urlencode(query)' in sched
    for period in ('today', 'yesterday', 'this_week', 'last_week',
                   'this_month', 'last_month'):
        assert f'value="{period}"' in sched_page
    assert 'value="1">Last 24 hours' in sched_page
    assert 'period=' in sched_page
    assert 'periodParam' in sched_page


def test_report_routes_accept_end_now():
    assert "request.args.get('end_now')" in ROUTES


def test_report_timestamps_are_generated_in_eat():
    assert 'datetime.now().strftime' not in HELPERS
    assert 'datetime.now().strftime' not in ROUTES
    assert 'datetime.now(EAT).strftime' in HELPERS
    assert 'datetime.now(EAT).strftime' in ROUTES
    assert 'EAT = timezone(timedelta(hours=3))' in HELPERS
    assert 'EAT = timezone(timedelta(hours=3))' in ROUTES


def test_admin_schedules_can_target_a_saved_custom_report():
    """The old schedule form only knew the eight fixed report types.

    A custom schedule stores ``report_type='custom'`` with the definition id
    in ``params``; the admin form must be able to pick that definition rather
    than silently falling back to Transaction Summary when an admin edits it.
    """
    sched_page = _read('templates', 'admin_schedules.html')
    assert '<option value="custom">Saved custom report</option>' in sched_page
    assert 'id="schedDefinitionWrap"' in sched_page
    assert 'id="schedDefinition"' in sched_page
    assert 'definition_id=' in sched_page
    assert "type === 'custom'" in sched_page
