"""The custom-report engine is a security boundary and a semantics boundary.

These tests pin both:

* only catalog identifiers survive validation, and values are always bound;
* one fact grain per report, one date grain at a time, and rates are computed
  over the group rather than averaged from other rows;
* a state segment that crosses midnight is split for a daily availability
  report instead of being attributed to the day it started.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'report-portal'))

import report_engine as engine  # noqa: E402


def definition(**overrides):
    base = {
        'dataset': 'transactions',
        'group_by': ['district'],
        'measures': ['txn_count', 'success_rate'],
        'filters': {'date': {'preset': 'last_7_days'}},
    }
    base.update(overrides)
    return base


def compile_definition(**overrides):
    parsed = engine.validate_definition(definition(**overrides))
    return parsed, engine.compile_report(parsed)


# ── Catalog ────────────────────────────────────────────────────────────────

def test_catalog_exposes_no_sql_and_documents_the_grain():
    payload = engine.catalog_payload()
    blob = json.dumps(payload)
    assert 'SELECT' not in blob
    assert 'FROM ' not in blob
    assert {d['id'] for d in payload['datasets']} == {
        'transactions', 'problems', 'availability', 'fleet'}
    for dataset in payload['datasets']:
        assert dataset['grain']
        assert dataset['default_measures']


def test_foreign_dimensions_are_not_silently_accepted():
    """A field from another subject is not a harmless no-op; it is a mistake."""
    for dataset in engine.DATASETS.values():
        if 'install_year' in dataset.dimensions:
            continue
        with pytest.raises(engine.ReportDefinitionError):
            engine.validate_definition({
                'dataset': dataset.id, 'group_by': ['install_year'],
                'measures': list(dataset.measures)[:1],
                'filters': {'date': {'preset': 'last_7_days'}}
                if dataset.requires_period else {},
            })


def test_fleet_is_a_snapshot_and_defaults_to_the_whole_registry():
    parsed = engine.validate_definition({
        'dataset': 'fleet', 'group_by': ['status'],
        'measures': ['atm_count', 'active_count'],
    })
    assert parsed.window is None
    assert parsed.filters['atm_scope'] == 'all'
    assert 'date' not in parsed.filters


# ── Validation ─────────────────────────────────────────────────────────────

def test_unknown_subject_is_rejected():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(dataset='sql'))


def test_unknown_dimension_is_rejected():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(group_by=['district', 'sql']))


def test_unknown_measure_is_rejected():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(measures=['txn_count', 'drop']))
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(measures=['drop']))


def test_unknown_filter_is_rejected():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(
            filters={'date': {'preset': 'last_7_days'}, 'sql': ['1=1']}))


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(query='DELETE FROM atm_locations'))


def test_at_least_one_measure_is_required():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(measures=[]))


def test_grouping_and_measure_limits_are_enforced():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(
            group_by=['district', 'region', 'city', 'vendor']))
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(measures=[
            'txn_count', 'approved', 'declined', 'errors', 'success_rate',
            'amount', 'cash_dispensed', 'avg_ticket', 'atms']))


def test_only_one_date_grain_at_a_time():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(group_by=['day', 'month']))


def test_a_single_filter_accepts_one_value():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'},
            'atm_id': ['ATM-1', 'ATM-2'],
        }))


def test_period_is_required_for_historical_subjects():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={}))


def test_period_cannot_exceed_ninety_days():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'start': '2026-01-01', 'end': '2026-06-30'}}))


def test_preset_and_custom_dates_are_mutually_exclusive():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days',
                     'start': '2026-01-01', 'end': '2026-01-07'}}))


def test_custom_dates_are_inclusive_and_end_at_eat_midnight():
    parsed = engine.validate_definition(definition(filters={
        'date': {'start': '2026-10-01', 'end': '2026-10-31'}}))
    assert parsed.window.days == 31
    assert parsed.window.start_utc == dt.datetime(2026, 9, 30, 21, 0)
    assert parsed.window.end_utc == dt.datetime(2026, 10, 31, 21, 0)


def test_filter_values_are_bound_not_interpolated():
    hostile = "Kebele'); DROP TABLE atm_locations; --"
    parsed, compiled = compile_definition(filters={
        'date': {'preset': 'last_7_days'}, 'district': [hostile]})
    assert hostile not in compiled.sql
    assert [hostile] in compiled.params
    assert compiled.sql.count('%s') == len(compiled.params)


def test_conditions_are_compiled_from_the_catalog_and_bound():
    hostile = "Bole'); DROP TABLE atm_locations; --"
    parsed, compiled = compile_definition(filters={
        'date': {'preset': 'last_7_days'},
        'conditions': [
            {'field': 'status', 'op': 'in', 'value': ['APPROVED']},
            {'field': 'district', 'op': 'contains', 'value': hostile},
            {'field': 'amount', 'op': 'gte', 'value': 1000},
        ],
        'condition_logic': 'and',
    })
    assert hostile not in compiled.sql
    assert hostile in compiled.params
    assert 'POSITION(LOWER(%s)' in compiled.sql
    assert 't.amount >= %s' in compiled.sql
    assert compiled.sql.count('%s') == len(compiled.params)
    assert parsed.filters['conditions'][2]['value'] == 1000


def test_condition_or_logic_is_one_parenthesised_group():
    parsed, compiled = compile_definition(filters={
        'date': {'preset': 'last_7_days'},
        'conditions': [
            {'field': 'vendor', 'op': 'eq', 'value': 'NCR'},
            {'field': 'vendor', 'op': 'eq', 'value': 'GRG'},
        ],
        'condition_logic': 'or',
    })
    assert "(COALESCE(NULLIF(TRIM(l.vendor), ''), 'Unknown') = %s OR " \
           "COALESCE(NULLIF(TRIM(l.vendor), ''), 'Unknown') = %s)" in \
        compiled.sql
    assert parsed.filters['condition_logic'] == 'or'


def test_condition_operators_are_type_checked():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'},
            'conditions': [{'field': 'amount', 'op': 'contains',
                            'value': '100'}],
        }))
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'},
            'conditions': [{'field': 'amount', 'op': 'gte',
                            'value': 'not-a-number'}],
        }))


def test_any_dimension_can_be_used_as_a_condition():
    parsed, compiled = compile_definition(filters={
        'date': {'preset': 'last_7_days'},
        'conditions': [{'field': 'source', 'op': 'eq', 'value': 'NCR'}],
    })
    assert "COALESCE(NULLIF(TRIM(t.source), ''), 'Unknown') = %s" \
        in compiled.sql
    assert 'NCR' in compiled.params
    assert parsed.filters['conditions'][0]['field'] == 'source'


def test_conditions_are_capped_and_unknown_keys_rejected():
    conditions = [{'field': 'vendor', 'op': 'eq', 'value': 'NCR'}] * \
                 (engine.MAX_CONDITIONS + 1)
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'}, 'conditions': conditions}))
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'},
            'conditions': [{'field': 'vendor', 'op': 'eq',
                            'value': 'NCR', 'sql': '1=1'}],
        }))


def test_catalog_exposes_condition_metadata_without_sql():
    payload = engine.catalog_payload()
    transactions = next(d for d in payload['datasets']
                        if d['id'] == 'transactions')
    fields = {f['id']: f for f in transactions['condition_fields']}
    assert 'source' in fields
    assert fields['source']['operators']
    assert fields['amount']['data_type'] == 'number'
    assert fields['status']['data_type'] == 'enum'
    categories = {d['category'] for d in transactions['dimensions']}
    assert {'Location', 'Time', 'Transaction'} <= categories
    assert fields['district']['category'] == 'Location'
    assert fields['status']['category'] == 'Transaction'
    assert not {'day', 'week', 'month'} & set(fields)
    assert payload['limits']['max_conditions'] == engine.MAX_CONDITIONS
    assert payload['limits']['max_filter_values'] == engine.MAX_FILTER_VALUES


def test_fleet_install_year_keeps_missing_dates_visible():
    _, compiled = compile_definition(
        dataset='fleet', group_by=['install_year'], measures=['atm_count'],
        filters={})
    assert "COALESCE(TO_CHAR(l.install_date, 'YYYY'), 'Unknown')" \
        in compiled.sql


def test_filter_options_include_inactive_registry_values():
    """Choosing All ATMs must not leave inactive branches unselectable."""
    class _Cursor:
        def __init__(self, conn):
            self.conn = conn
            self.rows = []

        def execute(self, sql):
            self.conn.statements.append(sql)
            if 'SELECT atm_id,' in sql:
                self.rows = [('ATM-1', 'Bole Branch')]
            else:
                self.rows = [('Bole',)]

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class _Conn:
        def __init__(self):
            self.statements = []

        def cursor(self):
            return _Cursor(self)

    conn = _Conn()
    engine._OPTIONS_CACHE.clear()
    try:
        payload = engine.filter_options(conn, force=True)
    finally:
        engine._OPTIONS_CACHE.clear()
    assert payload['atms'] == [
        {'value': 'ATM-1', 'label': 'ATM-1 — Bole Branch'}]
    assert conn.statements
    assert not any('status' in statement.lower()
                   for statement in conn.statements)


def test_legacy_saved_filters_are_still_supported():
    """Definitions saved by the chip-based builder must keep their meaning."""
    parsed, compiled = compile_definition(filters={
        'date': {'preset': 'last_7_days'},
        'district': ['Bole', 'Kirkos'],
        'problem_type': ['Cash jam'],
    })
    assert "COALESCE(NULLIF(TRIM(l.district), ''), 'Unassigned') = ANY(%s)" \
        in compiled.sql
    assert "'Unmapped') END = ANY(%s)" in compiled.sql
    assert ['Bole', 'Kirkos'] in compiled.params
    assert ['Cash jam'] in compiled.params
    assert parsed.filters['district'] == ['Bole', 'Kirkos']


def test_filter_summary_uses_labels_and_limits_long_value_lists():
    parsed = engine.validate_definition(definition(filters={
        'date': {'preset': 'last_7_days'},
        'conditions': [
            {'field': 'status', 'op': 'in',
             'value': ['APPROVED', 'DECLINED', 'ERROR']},
            {'field': 'amount', 'op': 'between', 'value': [1000, 2000]},
            {'field': 'district', 'op': 'in',
             'value': ['A', 'B', 'C', 'D', 'E']},
        ],
    }))
    summary = engine._filter_summary(parsed)
    assert 'Transaction status is any of Approved, Declined, Error' in summary
    assert 'Transaction amount (ETB) between 1000 and 2000' in summary
    assert 'District is any of A, B, C +2 more' in summary


def test_condition_values_are_capped():
    values = ['value-' + str(i) for i in range(engine.MAX_FILTER_VALUES + 1)]
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'},
            'conditions': [{'field': 'vendor', 'op': 'in',
                            'value': values}],
        }))


def test_filter_value_length_and_count_are_capped():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'}, 'district': ['x' * 201]}))
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(filters={
            'date': {'preset': 'last_7_days'},
            'district': [f'D{i}' for i in range(engine.MAX_FILTER_VALUES + 1)]}))


def test_definition_hash_is_stable_and_sensitive_to_filters():
    one = engine.validate_definition(definition())
    same = engine.validate_definition(definition())
    other = engine.validate_definition(definition(filters={
        'date': {'preset': 'last_7_days'}, 'vendor': ['NCR']}))
    assert one.definition_hash == same.definition_hash
    assert one.definition_hash != other.definition_hash


# ── Scope and compilation ──────────────────────────────────────────────────

def test_historical_subjects_default_to_active_atms():
    parsed, compiled = compile_definition()
    assert parsed.filters['atm_scope'] == 'active'
    assert "LOWER(COALESCE(l.status, '')) = 'active'" in compiled.sql


def test_all_scope_is_an_explicit_choice_and_drops_the_clause():
    parsed, compiled = compile_definition(filters={
        'date': {'preset': 'last_7_days'}, 'atm_scope': 'all'})
    assert parsed.filters['atm_scope'] == 'all'
    assert 'l.status' not in compiled.sql


def test_grouping_uses_ordinals_and_binds_the_limit():
    parsed, compiled = compile_definition(
        group_by=['district', 'vendor'],
        measures=['txn_count', 'amount'],
        sort={'id': 'amount', 'direction': 'desc'})
    assert 'GROUP BY 1, 2' in compiled.sql
    assert 'ORDER BY 4 DESC' in compiled.sql
    assert compiled.params[-1] == engine.MAX_PREVIEW_LIMIT


def test_export_validation_allows_the_larger_row_cap():
    parsed = engine.validate_definition(
        definition(), max_limit=engine.MAX_EXPORT_LIMIT)
    compiled = engine.compile_report(parsed)
    assert parsed.limit == engine.DEFAULT_LIMIT
    assert compiled.params[-1] == engine.DEFAULT_LIMIT


def test_sort_must_be_a_selected_group_or_measure():
    with pytest.raises(engine.ReportDefinitionError):
        engine.validate_definition(definition(
            sort={'id': 'amount', 'direction': 'desc'}))


def test_missing_sort_falls_back_to_a_selected_measure():
    parsed = engine.validate_definition(definition(sort={}))
    assert parsed.sort_id == 'txn_count'
    assert parsed.sort_dir == 'desc'


def test_totals_select_only_measures_and_has_no_limit_or_group():
    parsed = engine.validate_definition(definition(
        group_by=['district', 'vendor'],
        measures=['txn_count', 'success_rate']))
    compiled = engine.compile_report(parsed, totals=True)
    assert 'GROUP BY' not in compiled.sql
    assert 'LIMIT' not in compiled.sql
    assert 'ORDER BY' not in compiled.sql
    assert 'COUNT(*)' in compiled.sql
    assert 'AS "district"' not in compiled.sql


# ── Availability semantics ─────────────────────────────────────────────────

def test_availability_splits_segments_at_eat_midnight():
    parsed, compiled = compile_definition(
        dataset='availability',
        group_by=['day'],
        measures=['uptime_pct', 'downtime_hours'],
        filters={'date': {'start': '2026-10-01', 'end': '2026-10-31'}})
    assert 'generate_series' in compiled.sql
    assert "AT TIME ZONE 'Africa/Addis_Ababa'" in compiled.sql
    assert "TO_CHAR(seg.day_start" in compiled.sql
    assert 'GROUP BY 1' in compiled.sql
    # start, end, end, end, start, limit
    assert len(compiled.params) == 6
    assert compiled.params[0] == dt.datetime(2026, 9, 30, 21, 0)
    assert compiled.params[1] == dt.datetime(2026, 10, 31, 21, 0)


def test_downtime_episodes_are_distinct_not_day_pieces():
    parsed = engine.validate_definition({
        'dataset': 'availability',
        'group_by': ['day'],
        'measures': ['downtime_episodes'],
        'filters': {'date': {'preset': 'last_30_days'}},
    })
    compiled = engine.compile_report(parsed)
    assert 'COUNT(DISTINCT seg.segment_id)' in compiled.sql


def test_availability_uptime_is_time_weighted_not_an_average_of_states():
    parsed = engine.validate_definition({
        'dataset': 'availability',
        'group_by': ['district'],
        'measures': ['uptime_pct'],
        'filters': {'date': {'preset': 'last_30_days'}},
    })
    compiled = engine.compile_report(parsed)
    assert 'SUM(seg.hours) FILTER' in compiled.sql
    assert 'AVG(' not in compiled.sql


def test_availability_state_and_location_filters_are_parameterised():
    parsed, compiled = compile_definition(
        dataset='availability',
        group_by=['district'],
        measures=['uptime_pct'],
        filters={'date': {'preset': 'last_7_days'},
                 'state': ['OFFLINE'], 'vendor': ['NCR']})
    assert 'seg.state = ANY(%s)' in compiled.sql
    assert "l.vendor" in compiled.sql
    assert ['OFFLINE'] in compiled.params
    assert ['NCR'] in compiled.params


# ── Problem taxonomy ───────────────────────────────────────────────────────

def test_problem_dimensions_keep_unmapped_codes_visible():
    parsed = engine.validate_definition({
        'dataset': 'problems',
        'group_by': ['problem_type'],
        'measures': ['occurrences'],
        'filters': {'date': {'preset': 'last_30_days'}},
    })
    compiled = engine.compile_report(parsed)
    assert "COALESCE(f.display_name, 'Unmapped')" in compiled.sql
    assert 'LEFT JOIN fault_type_map f' in compiled.sql


def test_transactions_problem_type_separates_no_fault_from_unmapped():
    parsed = engine.validate_definition(definition(group_by=['problem_type']))
    compiled = engine.compile_report(parsed)
    assert "'No fault'" in compiled.sql
    assert "'Unmapped'" in compiled.sql


def test_cash_dispensed_does_not_guess_that_transfers_dispense_cash():
    parsed = engine.validate_definition(definition(measures=['cash_dispensed']))
    compiled = engine.compile_report(parsed)
    assert "t.txn_type = 'WITHDRAWAL'" in compiled.sql
    assert "t.txn_type = 'TRANSFER'" not in compiled.sql


# ── Formatting ─────────────────────────────────────────────────────────────

def test_state_labels_are_applied_to_dimension_values():
    parsed = engine.validate_definition({
        'dataset': 'availability', 'group_by': ['state'],
        'measures': ['covered_hours'],
        'filters': {'date': {'preset': 'last_7_days'}},
    })
    compiled = engine.compile_report(parsed)
    row = engine._format_rows(compiled, [('UNREACHABLE', 4.5)], parsed)
    assert row == [['Agent Disconnected', 4.5]]


def test_numeric_rows_are_json_safe():
    parsed = engine.validate_definition(definition(
        measures=['amount']))
    compiled = engine.compile_report(parsed)
    from decimal import Decimal
    rows = engine._format_rows(compiled, [('Bole', Decimal('123.45'))], parsed)
    assert rows == [['Bole', 123.45]]
