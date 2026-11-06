"""Governed self-service reporting engine.

This module is deliberately **not** a generic SQL builder.  A caller sends a
report definition that names a catalog dataset, catalog dimensions, catalog
measures and catalog filters.  Every identifier is resolved through the
catalog below; every value is bound as a query parameter.  No caller-supplied
SQL fragment, table name or column name ever reaches PostgreSQL.

Why the catalog exists
----------------------
The Reports page used to be a fixed set of endpoints.  "Add a report by
district" therefore meant "write another SQL block".  A free-form builder
would replace that with a worse problem: bank staff composing joins between
transactions, state history and cash loads by hand, producing numbers that are
authoritative-looking and wrong.  The catalog fixes the grain and the metric
formula in one place, and the compiler only combines expressions that were
designed to be combined.

Grain rules
-----------
* ``transactions`` — one row per visible transaction.
* ``problems``     — one row per ERROR transaction that carries a fault code.
* ``availability`` — one row per clipped ATM state segment, in hours.
* ``fleet``        — one row per ATM in the registry.

A report always uses exactly one dataset.  Mixing facts (for example adding
transactions and downtime hours to one table) is not a supported operation;
that is what the curated composite report remains for.

Time semantics
--------------
``recorded_at`` is stored as a naive UTC timestamp.  Day boundaries for
grouping and filtering are EAT (fixed +3, no DST).  The reporting window is
inclusive of the end date, matching ``report_dates.parse_report_window``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from report_dates import ReportWindow, parse_report_window

EAT = dt.timezone(dt.timedelta(hours=3))

MAX_RANGE_DAYS = 90
MAX_GROUP_BY = 3
MAX_MEASURES = 8
MAX_FILTER_VALUES = 50
# ``limit`` is the export row cap.  The browser preview is deliberately
# smaller: rendering 100,000 rows helps nobody, and the preview tells the user
# when it is showing a subset.  This keeps one definition authoritative while
# letting a 90-day ATM/day export actually complete.
DEFAULT_LIMIT = 100000
PREVIEW_ROW_LIMIT = 500
MAX_PREVIEW_LIMIT = 2000
MAX_EXPORT_LIMIT = 100000

# One date grain at a time.  Day + week + month in one table is a pivot, not a
# report, and it makes the total row meaningless.
TIME_DIMENSIONS = frozenset({'day', 'week', 'month'})

# Display labels for state codes.  Kept here rather than importing helpers so
# the engine remains importable outside a Flask process (tests, CLI audits).
STATE_LABELS = {
    'IN_SERVICE': 'In Service',
    'UNREACHABLE': 'Agent Disconnected',
    'UNKNOWN': 'Unknown',
    'IN_SUPERVISOR': 'In Supervisor',
    'HARDWARE_FAULT': 'Hardware Faults',
    'OUT_OF_CASH': 'Out Of Cash',
    'OFFLINE': 'Offline',
    'OUT_OF_SERVICE': 'Out of Service',
    'NOT_PROVISIONED': 'Not Provisioned',
    'INACTIVE': 'Inactive',
}

STATE_ORDER = (
    'IN_SERVICE',
    'UNREACHABLE',
    'UNKNOWN',
    'IN_SUPERVISOR',
    'HARDWARE_FAULT',
    'OUT_OF_CASH',
    'OFFLINE',
    'OUT_OF_SERVICE',
)


class ReportDefinitionError(ValueError):
    """The caller sent a definition the catalog cannot satisfy."""


class ReportExecutionError(RuntimeError):
    """The query was valid but could not be executed in the time budget."""


# ── Definition objects ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Dimension:
    id: str
    label: str
    expr: str
    type: str = 'text'
    description: str = ''
    joins: tuple[str, ...] = ()
    value_labels: dict[str, str] | None = None


@dataclass(frozen=True)
class Measure:
    id: str
    label: str
    expr: str
    type: str = 'number'
    description: str = ''
    joins: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilterSpec:
    id: str
    label: str
    kind: str  # 'multi' | 'single'
    expr: str = ''
    description: str = ''
    joins: tuple[str, ...] = ()
    group: str = 'location'  # 'transaction' | 'location' | 'availability'
    options_source: str | None = None
    static_options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Dataset:
    id: str
    label: str
    description: str
    grain: str
    requires_period: bool
    base_from: str
    base_where: str = ''
    time_column: str | None = None
    base_param_order: tuple[str, ...] = ()
    joins: dict[str, str] = field(default_factory=dict)
    join_order: tuple[str, ...] = ()
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    filters: dict[str, FilterSpec] = field(default_factory=dict)
    default_group_by: tuple[str, ...] = ()
    default_measures: tuple[str, ...] = ()
    default_period: str = 'last_30_days'
    default_atm_scope: str = 'active'
    default_sort_id: str = ''
    default_sort_dir: str = 'desc'


@dataclass(frozen=True)
class Definition:
    dataset: Dataset
    group_by: tuple[str, ...]
    measures: tuple[str, ...]
    filters: dict[str, Any]
    sort_id: str
    sort_dir: str
    limit: int
    window: ReportWindow | None
    raw: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        """The normalized definition, safe to persist and replay."""
        return {
            'dataset': self.dataset.id,
            'group_by': list(self.group_by),
            'measures': list(self.measures),
            'filters': self.filters,
            'sort': {'id': self.sort_id, 'direction': self.sort_dir},
            'limit': self.limit,
        }

    @property
    def definition_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True,
                          separators=(',', ':'), default=str)
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


@dataclass(frozen=True)
class CompiledReport:
    sql: str
    params: list[Any]
    dimensions: tuple[Dimension, ...]
    measures: tuple[Measure, ...]
    totals: bool = False


# ── Catalog helpers ─────────────────────────────────────────────────────────

def _location_dimensions() -> dict[str, Dimension]:
    return {
        'district': Dimension(
            'district', 'District',
            "COALESCE(NULLIF(TRIM(l.district), ''), 'Unassigned')",
            description='ATM district from the registry. Missing values are '
                        'kept as Unassigned, never dropped.'),
        'region': Dimension(
            'region', 'Region',
            "COALESCE(NULLIF(TRIM(l.region), ''), 'Unassigned')"),
        'city': Dimension(
            'city', 'City',
            "COALESCE(NULLIF(TRIM(l.city), ''), 'Unassigned')"),
        'branch': Dimension(
            'branch', 'Branch',
            "COALESCE(NULLIF(TRIM(l.branch), ''), 'Unassigned')"),
        'vendor': Dimension(
            'vendor', 'Vendor',
            "COALESCE(NULLIF(TRIM(l.vendor), ''), 'Unknown')"),
        'model': Dimension(
            'model', 'Model',
            "COALESCE(NULLIF(TRIM(l.model), ''), 'Unknown')"),
        'atm_id': Dimension('atm_id', 'ATM', 'l.atm_id'),
    }


def _location_filters(*, include_status: bool = False) -> dict[str, FilterSpec]:
    filters = {
        'vendor': FilterSpec(
            'vendor', 'Vendor', 'multi',
            "COALESCE(NULLIF(TRIM(l.vendor), ''), 'Unknown') = ANY(%s)",
            options_source='vendors'),
        'district': FilterSpec(
            'district', 'District', 'multi',
            "COALESCE(NULLIF(TRIM(l.district), ''), 'Unassigned') = ANY(%s)",
            options_source='districts'),
        'region': FilterSpec(
            'region', 'Region', 'multi',
            "COALESCE(NULLIF(TRIM(l.region), ''), 'Unassigned') = ANY(%s)",
            options_source='regions'),
        'city': FilterSpec(
            'city', 'City', 'multi',
            "COALESCE(NULLIF(TRIM(l.city), ''), 'Unassigned') = ANY(%s)",
            options_source='cities'),
        'branch': FilterSpec(
            'branch', 'Branch', 'multi',
            "COALESCE(NULLIF(TRIM(l.branch), ''), 'Unassigned') = ANY(%s)",
            options_source='branches'),
        'model': FilterSpec(
            'model', 'Model', 'multi',
            "COALESCE(NULLIF(TRIM(l.model), ''), 'Unknown') = ANY(%s)",
            options_source='models'),
        'atm_id': FilterSpec(
            'atm_id', 'ATM', 'single', 'l.atm_id = %s',
            options_source='atms'),
    }
    if include_status:
        filters['status'] = FilterSpec(
            'status', 'ATM status', 'multi',
            "COALESCE(NULLIF(TRIM(l.status), ''), 'Unknown') = ANY(%s)",
            options_source='atm_statuses')
    return filters


def _transaction_dimensions() -> dict[str, Dimension]:
    dims = _location_dimensions()
    dims.update({
        'day': Dimension(
            'day', 'Day',
            "TO_CHAR(t.recorded_at AT TIME ZONE 'UTC' "
            "AT TIME ZONE 'Africa/Addis_Ababa', 'YYYY-MM-DD')",
            type='date'),
        'week': Dimension(
            'week', 'Week starting',
            "TO_CHAR(DATE_TRUNC('week', t.recorded_at AT TIME ZONE 'UTC' "
            "AT TIME ZONE 'Africa/Addis_Ababa'), 'YYYY-MM-DD')",
            type='date'),
        'month': Dimension(
            'month', 'Month',
            "TO_CHAR(DATE_TRUNC('month', t.recorded_at AT TIME ZONE 'UTC' "
            "AT TIME ZONE 'Africa/Addis_Ababa'), 'YYYY-MM')",
            type='text'),
        'txn_type': Dimension(
            'txn_type', 'Transaction type',
            "COALESCE(NULLIF(TRIM(t.txn_type), ''), 'Unknown')"),
        'status': Dimension(
            'status', 'Status',
            "COALESCE(NULLIF(TRIM(t.status), ''), 'Unknown')",
            value_labels={'APPROVED': 'Approved', 'DECLINED': 'Declined',
                          'ERROR': 'Error'}),
        'source': Dimension(
            'source', 'Source',
            "COALESCE(NULLIF(TRIM(t.source), ''), 'Unknown')"),
        'problem_type': Dimension(
            'problem_type', 'Problem type',
            "CASE WHEN t.error_code IS NULL OR TRIM(t.error_code) = '' "
            "THEN 'No fault' ELSE COALESCE(f.display_name, 'Unmapped') END",
            description='Management-facing fault category mapped from the '
                        'vendor error code. Unmapped codes stay visible.',
            joins=('fault_map',)),
        'error_code': Dimension(
            'error_code', 'Fault code',
            "COALESCE(NULLIF(TRIM(t.error_code), ''), 'None')",
            description='Raw vendor error code. Use Problem type for a '
                        'management view.'),
    })
    return dims


def _transaction_measures() -> dict[str, Measure]:
    return {
        'txn_count': Measure(
            'txn_count', 'Transactions', 'COUNT(*)',
            description='Every visible transaction in the period.'),
        'approved': Measure(
            'approved', 'Approved',
            "SUM(CASE WHEN t.status = 'APPROVED' THEN 1 ELSE 0 END)"),
        'declined': Measure(
            'declined', 'Declined',
            "SUM(CASE WHEN t.status = 'DECLINED' THEN 1 ELSE 0 END)"),
        'errors': Measure(
            'errors', 'Errors',
            "SUM(CASE WHEN t.status = 'ERROR' THEN 1 ELSE 0 END)"),
        'success_rate': Measure(
            'success_rate', 'Success rate',
            "ROUND(100.0 * SUM(CASE WHEN t.status = 'APPROVED' THEN 1 ELSE 0 "
            "END) / NULLIF(COUNT(*), 0), 2)",
            type='percent',
            description='Approved transactions divided by all transactions in '
                        'the same group. Not an average of ATM rates.'),
        'amount': Measure(
            'amount', 'Approved amount (ETB)',
            "COALESCE(SUM(CASE WHEN t.status = 'APPROVED' THEN t.amount "
            "ELSE 0 END), 0)",
            type='currency',
            description='Sum of approved transaction amounts.'),
        'cash_dispensed': Measure(
            'cash_dispensed', 'Cash dispensed (ETB)',
            "COALESCE(SUM(CASE WHEN t.status = 'APPROVED' AND t.txn_type = "
            "'WITHDRAWAL' THEN t.amount ELSE 0 END), 0)",
            type='currency',
            description='Approved withdrawal amounts only. Transfers and '
                        'cardless transactions are excluded until the business '
                        'definition says otherwise.'),
        'avg_ticket': Measure(
            'avg_ticket', 'Avg approved amount (ETB)',
            "ROUND(AVG(CASE WHEN t.status = 'APPROVED' THEN t.amount END), 2)",
            type='currency'),
        'atms': Measure(
            'atms', 'ATMs with transactions', 'COUNT(DISTINCT t.atm_id)'),
        'txns_per_atm': Measure(
            'txns_per_atm', 'Transactions per ATM',
            "ROUND(COUNT(*)::numeric / NULLIF(COUNT(DISTINCT t.atm_id), 0), 1)",
            description='Transaction count divided by ATMs that transacted. '
                        'ATMs with zero transactions do not affect this '
                        'measure.'),
    }


def _transaction_filters() -> dict[str, FilterSpec]:
    filters = _location_filters()
    filters.update({
        'status': FilterSpec(
            'status', 'Transaction status', 'multi',
            "COALESCE(NULLIF(TRIM(t.status), ''), 'Unknown') = ANY(%s)",
            group='transaction',
            static_options=(('APPROVED', 'Approved'),
                            ('DECLINED', 'Declined'),
                            ('ERROR', 'Error'))),
        'txn_type': FilterSpec(
            'txn_type', 'Transaction type', 'multi',
            "COALESCE(NULLIF(TRIM(t.txn_type), ''), 'Unknown') = ANY(%s)",
            group='transaction', options_source='txn_types'),
        'problem_type': FilterSpec(
            'problem_type', 'Problem type', 'multi',
            "CASE WHEN t.error_code IS NULL OR TRIM(t.error_code) = '' "
            "THEN 'No fault' ELSE COALESCE(f.display_name, 'Unmapped') END "
            "= ANY(%s)",
            group='transaction', options_source='problem_types',
            joins=('fault_map',)),
    })
    return filters


def _problem_filters() -> dict[str, FilterSpec]:
    filters = _location_filters()
    filters.update({
        'problem_type': FilterSpec(
            'problem_type', 'Problem type', 'multi',
            "COALESCE(f.display_name, 'Unmapped') = ANY(%s)",
            group='transaction', options_source='problem_types',
            joins=('fault_map',)),
    })
    return filters


def _availability_filters() -> dict[str, FilterSpec]:
    filters = _location_filters()
    filters['state'] = FilterSpec(
        'state', 'State', 'multi', 'seg.state = ANY(%s)',
        group='availability',
        static_options=tuple((s, STATE_LABELS[s]) for s in STATE_ORDER))
    return filters


# ── The catalog ─────────────────────────────────────────────────────────────

_FAULT_JOIN = (
    'LEFT JOIN fault_type_map f ON f.vendor = l.vendor '
    'AND f.raw_fault_code = t.error_code'
)

# A state segment can cross any number of EAT midnights.  Summing it into the
# day it started would put Sunday's uptime in Saturday's row, so each segment
# is split at local midnight.  The split changes only the grain; the hours in
# each piece still add up to the clipped segment length, so uptime and covered
# hours are identical whether or not a date grouping is used.
AVAILABILITY_FROM = """
FROM (
    SELECT p.atm_id, p.state, p.segment_id, d.day_start,
           EXTRACT(EPOCH FROM (
               LEAST(p.piece_end, d.day_start + INTERVAL '1 day')
               - GREATEST(p.piece_start, d.day_start)
           )) / 3600.0 AS hours
    FROM (
        SELECT h.id AS segment_id, h.atm_id, h.state,
               GREATEST(h.started_at, %s) AS piece_start,
               LEAST(COALESCE(h.ended_at, %s), %s) AS piece_end
        FROM atm_state_history h
        WHERE h.started_at < %s
          AND (h.ended_at IS NULL OR h.ended_at > %s)
    ) p
    CROSS JOIN LATERAL generate_series(
        DATE_TRUNC('day', p.piece_start AT TIME ZONE 'UTC'
                   AT TIME ZONE 'Africa/Addis_Ababa')
            AT TIME ZONE 'Africa/Addis_Ababa' AT TIME ZONE 'UTC',
        DATE_TRUNC('day', (p.piece_end - INTERVAL '1 microsecond')
                   AT TIME ZONE 'UTC'
                   AT TIME ZONE 'Africa/Addis_Ababa')
            AT TIME ZONE 'Africa/Addis_Ababa' AT TIME ZONE 'UTC',
        INTERVAL '1 day'
    ) AS d(day_start)
) seg
JOIN atm_locations l ON l.atm_id = seg.atm_id
"""


def _build_datasets() -> dict[str, Dataset]:
    transaction_from = (
        'FROM atm_transactions_visible t\n'
        'JOIN atm_locations l ON l.atm_id = t.atm_id'
    )
    return {
        'transactions': Dataset(
            id='transactions',
            label='Transactions',
            description='Volume, success rate, amounts and cash dispensed.',
            grain='One row per visible transaction.',
            requires_period=True,
            base_from=transaction_from,
            time_column='t.recorded_at',
            joins={'fault_map': _FAULT_JOIN},
            join_order=('fault_map',),
            dimensions=_transaction_dimensions(),
            measures=_transaction_measures(),
            filters=_transaction_filters(),
            default_group_by=('district',),
            default_measures=('txn_count', 'success_rate', 'cash_dispensed'),
            default_sort_id='txn_count',
            default_sort_dir='desc',
        ),
        'problems': Dataset(
            id='problems',
            label='Problems',
            description='Failed transactions that carry a vendor fault code.',
            grain='One row per ERROR transaction with a fault code.',
            requires_period=True,
            base_from=transaction_from,
            base_where=("t.status = 'ERROR' AND t.error_code IS NOT NULL "
                        "AND TRIM(t.error_code) <> ''"),
            time_column='t.recorded_at',
            joins={'fault_map': _FAULT_JOIN},
            join_order=('fault_map',),
            # Status is constant (ERROR) on this subject.  Leaving it in the
            # catalog would invite a meaningless one-row grouping.
            dimensions={k: v for k, v in _transaction_dimensions().items()
                        if k != 'status'},
            measures={
                'occurrences': Measure(
                    'occurrences', 'Occurrences', 'COUNT(*)',
                    description='Error transactions in the period.'),
                'affected_atms': Measure(
                    'affected_atms', 'ATMs affected',
                    'COUNT(DISTINCT t.atm_id)'),
                'first_seen': Measure(
                    'first_seen', 'First seen',
                    "TO_CHAR(MIN(t.recorded_at) AT TIME ZONE 'UTC' "
                    "AT TIME ZONE 'Africa/Addis_Ababa', "
                    "'YYYY-MM-DD HH24:MI')",
                    type='datetime'),
                'last_seen': Measure(
                    'last_seen', 'Last seen',
                    "TO_CHAR(MAX(t.recorded_at) AT TIME ZONE 'UTC' "
                    "AT TIME ZONE 'Africa/Addis_Ababa', "
                    "'YYYY-MM-DD HH24:MI')",
                    type='datetime'),
            },
            filters=_problem_filters(),
            default_group_by=('problem_type',),
            default_measures=('occurrences', 'affected_atms', 'last_seen'),
            default_sort_id='occurrences',
            default_sort_dir='desc',
        ),
        'availability': Dataset(
            id='availability',
            label='Availability',
            description='Time-weighted uptime, downtime and coverage.',
            grain='One row per ATM state segment per EAT day, clipped to the '
                  'period.',
            requires_period=True,
            base_from=AVAILABILITY_FROM,
            base_param_order=('start', 'end', 'end', 'end', 'start'),
            joins={},
            join_order=(),
            dimensions={
                **_location_dimensions(),
                'day': Dimension(
                    'day', 'Day',
                    "TO_CHAR(seg.day_start AT TIME ZONE 'UTC' "
                    "AT TIME ZONE 'Africa/Addis_Ababa', 'YYYY-MM-DD')",
                    type='date',
                    description='EAT day. Segments crossing midnight are '
                                'split, so an hour is counted once.'),
                'week': Dimension(
                    'week', 'Week starting',
                    "TO_CHAR(DATE_TRUNC('week', seg.day_start "
                    "AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa'), "
                    "'YYYY-MM-DD')",
                    type='date'),
                'month': Dimension(
                    'month', 'Month',
                    "TO_CHAR(DATE_TRUNC('month', seg.day_start "
                    "AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Addis_Ababa'), "
                    "'YYYY-MM')",
                    type='text'),
                'state': Dimension(
                    'state', 'State', 'seg.state',
                    value_labels=dict(STATE_LABELS),
                    description='ATM state as recorded in state history.'),
            },
            measures={
                'uptime_pct': Measure(
                    'uptime_pct', 'Uptime',
                    "ROUND(100.0 * COALESCE(SUM(seg.hours) FILTER "
                    "(WHERE seg.state = 'IN_SERVICE'), 0) / "
                    "NULLIF(SUM(seg.hours), 0), 2)",
                    type='percent',
                    description='Time-weighted IN_SERVICE hours divided by '
                                'covered hours. Pre-history is not counted as '
                                'downtime.'),
                'downtime_hours': Measure(
                    'downtime_hours', 'Downtime (hrs)',
                    "ROUND(COALESCE(SUM(seg.hours) FILTER "
                    "(WHERE seg.state <> 'IN_SERVICE'), 0), 1)",
                    type='hours'),
                'covered_hours': Measure(
                    'covered_hours', 'Covered (hrs)',
                    "ROUND(COALESCE(SUM(seg.hours), 0), 1)",
                    type='hours',
                    description='Hours covered by retained state history.'),
                'atms': Measure(
                    'atms', 'ATMs with history',
                    'COUNT(DISTINCT seg.atm_id)'),
                'downtime_episodes': Measure(
                    'downtime_episodes', 'Downtime episodes',
                    "COUNT(DISTINCT seg.segment_id) FILTER "
                    "(WHERE seg.state <> 'IN_SERVICE')",
                    description='State segments outside IN_SERVICE. A segment '
                                'split across midnight is still one episode.'),
            },
            filters=_availability_filters(),
            default_group_by=('district',),
            default_measures=('uptime_pct', 'downtime_hours', 'atms'),
            default_sort_id='uptime_pct',
            default_sort_dir='asc',
        ),
        'fleet': Dataset(
            id='fleet',
            label='Fleet profile',
            description='ATM counts and registry attributes.',
            grain='One row per ATM in the registry.',
            requires_period=False,
            base_from='FROM atm_locations l',
            joins={},
            join_order=(),
            dimensions={
                **_location_dimensions(),
                'status': Dimension(
                    'status', 'ATM status',
                    "COALESCE(NULLIF(TRIM(l.status), ''), 'Unknown')",
                    value_labels={'active': 'Active', 'inactive': 'Inactive'}),
                'install_year': Dimension(
                    'install_year', 'Install year',
                    "TO_CHAR(l.install_date, 'YYYY')"),
            },
            measures={
                'atm_count': Measure('atm_count', 'ATMs', 'COUNT(*)'),
                'active_count': Measure(
                    'active_count', 'Active',
                    "SUM(CASE WHEN LOWER(COALESCE(l.status, '')) = 'active' "
                    "THEN 1 ELSE 0 END)"),
                'inactive_count': Measure(
                    'inactive_count', 'Inactive',
                    "SUM(CASE WHEN LOWER(COALESCE(l.status, '')) <> 'active' "
                    "THEN 1 ELSE 0 END)"),
                'avg_age_years': Measure(
                    'avg_age_years', 'Avg age (years)',
                    "ROUND(AVG(EXTRACT(YEAR FROM AGE(CURRENT_DATE, "
                    "l.install_date))), 1)",
                    type='number'),
                'with_coordinates': Measure(
                    'with_coordinates', 'With coordinates',
                    "COUNT(*) FILTER (WHERE l.latitude IS NOT NULL AND "
                    "l.longitude IS NOT NULL)"),
            },
            filters=_location_filters(include_status=True),
            default_group_by=('district',),
            default_measures=('atm_count', 'active_count'),
            default_period='',
            # A fleet profile that silently hides inactive ATMs is not a
            # profile.  The status dimension and the active/inactive measures
            # are the point of this subject.
            default_atm_scope='all',
            default_sort_id='atm_count',
            default_sort_dir='desc',
        ),
    }


DATASETS: dict[str, Dataset] = _build_datasets()

PERIOD_PRESETS: tuple[tuple[str, str], ...] = (
    ('today', 'Today'),
    ('yesterday', 'Yesterday'),
    ('last_7_days', 'Last 7 days'),
    ('last_30_days', 'Last 30 days'),
    ('last_90_days', 'Last 90 days'),
    ('this_week', 'This week'),
    ('last_week', 'Last week'),
    ('this_month', 'This month'),
    ('last_month', 'Last month'),
    ('custom', 'Custom dates'),
)


# ── Period resolution ───────────────────────────────────────────────────────

def _eat_today(now: dt.datetime | None = None) -> dt.date:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(EAT).date()


def preset_dates(preset: str, today: dt.date | None = None) -> tuple[dt.date, dt.date] | None:
    """Resolve a saved-report period preset to inclusive EAT dates."""
    if today is None:
        today = _eat_today()
    if preset == 'today':
        return today, today
    if preset == 'yesterday':
        day = today - dt.timedelta(days=1)
        return day, day
    if preset == 'last_7_days':
        return today - dt.timedelta(days=6), today
    if preset == 'last_30_days':
        return today - dt.timedelta(days=29), today
    if preset == 'last_90_days':
        return today - dt.timedelta(days=89), today
    if preset == 'this_week':
        return today - dt.timedelta(days=today.weekday()), today
    if preset == 'last_week':
        monday = today - dt.timedelta(days=today.weekday())
        return monday - dt.timedelta(days=7), monday - dt.timedelta(days=1)
    if preset == 'this_month':
        return today.replace(day=1), today
    if preset == 'last_month':
        last_day = today.replace(day=1) - dt.timedelta(days=1)
        return last_day.replace(day=1), last_day
    return None


def _resolve_window(date_filter: Any) -> ReportWindow | None:
    if date_filter in (None, {}, ''):
        return None
    if not isinstance(date_filter, dict):
        raise ReportDefinitionError('The date filter must be an object.')

    preset = str(date_filter.get('preset') or '').strip()
    start = str(date_filter.get('start') or '').strip()
    end = str(date_filter.get('end') or '').strip()

    if preset and preset != 'custom':
        if start or end:
            raise ReportDefinitionError(
                'Choose either a period preset or custom dates, not both.')
        dates = preset_dates(preset)
        if dates is None:
            raise ReportDefinitionError(f'Unknown reporting period {preset!r}.')
        start, end = dates[0].isoformat(), dates[1].isoformat()
    elif preset == 'custom' and not (start and end):
        raise ReportDefinitionError('Choose both a start date and an end date.')

    if not (start and end):
        return None
    try:
        return parse_report_window(start, end)
    except ValueError as exc:
        raise ReportDefinitionError(str(exc)) from exc


# ── Validation ──────────────────────────────────────────────────────────────

def _as_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ''):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ReportDefinitionError(f'{field_name} must be a list.')
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _validate_values(values: list[str], field_name: str) -> list[str]:
    if len(values) > MAX_FILTER_VALUES:
        raise ReportDefinitionError(
            f'Too many values for {field_name} (maximum {MAX_FILTER_VALUES}).')
    for value in values:
        if len(value) > 200:
            raise ReportDefinitionError(
                f'A value for {field_name} is too long.')
    return values


def validate_definition(raw: Any, *, max_limit: int = MAX_PREVIEW_LIMIT) -> Definition:
    """Validate and normalize a report definition.

    This is the security boundary.  Only ids present in the catalog survive;
    unknown keys are rejected rather than silently ignored, so a typo cannot
    produce a report that quietly omits a filter.
    """
    if not isinstance(raw, dict):
        raise ReportDefinitionError('The report definition must be an object.')
    allowed_top = {'dataset', 'group_by', 'measures', 'filters', 'sort',
                   'limit'}
    unknown_top = sorted(set(raw) - allowed_top)
    if unknown_top:
        raise ReportDefinitionError(
            f'Unknown definition field: {unknown_top[0]}.')

    dataset_id = str(raw.get('dataset') or '').strip()
    dataset = DATASETS.get(dataset_id)
    if dataset is None:
        raise ReportDefinitionError('Choose a valid report subject.')

    group_by = _as_list(raw.get('group_by'), 'Group by')
    if len(group_by) > MAX_GROUP_BY:
        raise ReportDefinitionError(
            f'Group by at most {MAX_GROUP_BY} fields.')
    for dim_id in group_by:
        if dim_id not in dataset.dimensions:
            raise ReportDefinitionError(
                f'{dim_id!r} is not a valid grouping for {dataset.label}.')
    if len([d for d in group_by if d in TIME_DIMENSIONS]) > 1:
        raise ReportDefinitionError('Choose one date grouping at a time.')

    measures = _as_list(raw.get('measures'), 'Measures')
    if not measures:
        raise ReportDefinitionError('Choose at least one measure.')
    if len(measures) > MAX_MEASURES:
        raise ReportDefinitionError(
            f'Choose at most {MAX_MEASURES} measures.')
    for measure_id in measures:
        if measure_id not in dataset.measures:
            raise ReportDefinitionError(
                f'{measure_id!r} is not a valid measure for {dataset.label}.')

    raw_filters = raw.get('filters') or {}
    if not isinstance(raw_filters, dict):
        raise ReportDefinitionError('Filters must be an object.')
    # Never mutate the request payload; normalize a private copy instead.
    raw_filters = dict(raw_filters)

    allowed_filter_keys = set(dataset.filters) | {'date', 'atm_scope'}
    unknown = sorted(set(raw_filters) - allowed_filter_keys)
    if unknown:
        raise ReportDefinitionError(
            f'Unknown filter: {unknown[0]}.')
    for key, value in raw_filters.items():
        if key in ('date', 'atm_scope'):
            continue
        spec = dataset.filters[key]
        values = _validate_values(_as_list(value, spec.label), spec.label)
        if spec.kind == 'single':
            if len(values) > 1:
                raise ReportDefinitionError(
                    f'{spec.label} accepts one value.')
        raw_filters[key] = values

    scope = str(raw_filters.get('atm_scope')
                or dataset.default_atm_scope).strip().lower()
    if scope not in ('active', 'all'):
        raise ReportDefinitionError('ATM scope must be active or all.')
    raw_filters['atm_scope'] = scope

    window = None
    date_filter = raw_filters.get('date')
    if dataset.requires_period:
        window = _resolve_window(date_filter)
        if window is None:
            raise ReportDefinitionError('Choose a reporting period.')
        if window.days > MAX_RANGE_DAYS:
            raise ReportDefinitionError(
                f'Reporting period cannot exceed {MAX_RANGE_DAYS} days.')
        raw_filters['date'] = _canonical_date_filter(date_filter, window)
    else:
        # A snapshot dataset ignores the period, but the saved definition should
        # not carry a misleading date spec.
        raw_filters.pop('date', None)

    sort = raw.get('sort') or {}
    if sort and not isinstance(sort, dict):
        raise ReportDefinitionError('Sort must be an object.')
    sort_id = str(sort.get('id') or '').strip()
    sort_dir = str(sort.get('direction') or 'desc').strip().lower()
    if sort_dir not in ('asc', 'desc'):
        raise ReportDefinitionError('Sort direction must be asc or desc.')
    if not sort_id:
        if dataset.default_sort_id in group_by \
                or dataset.default_sort_id in measures:
            sort_id = dataset.default_sort_id
            sort_dir = dataset.default_sort_dir
        elif measures:
            sort_id, sort_dir = measures[0], 'desc'
        else:
            sort_id, sort_dir = group_by[0], 'asc'
    if sort_id not in group_by and sort_id not in measures:
        raise ReportDefinitionError(
            'Sort by a selected grouping or measure.')

    try:
        limit = int(raw.get('limit') or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise ReportDefinitionError('Row limit must be a whole number.')
    limit = max(1, min(limit, max_limit))

    return Definition(
        dataset=dataset,
        group_by=tuple(group_by),
        measures=tuple(measures),
        filters=raw_filters,
        sort_id=sort_id,
        sort_dir=sort_dir,
        limit=limit,
        window=window,
        raw=raw,
    )


def _canonical_date_filter(date_filter: Any, window: ReportWindow) -> dict[str, str]:
    if isinstance(date_filter, dict):
        preset = str(date_filter.get('preset') or '').strip()
        if preset and preset != 'custom':
            return {'preset': preset}
        start = str(date_filter.get('start') or '').strip()
        end = str(date_filter.get('end') or '').strip()
        if start and end:
            return {'start': start, 'end': end}
    # The preset resolved cleanly but the caller did not name it; keep the
    # resolved window reproducible.
    return {'start': window.start_utc.date().isoformat(),
            'end': (window.end_utc - dt.timedelta(microseconds=1)).date().isoformat()}


# ── Compilation ─────────────────────────────────────────────────────────────

FILTER_ORDER = (
    'vendor', 'district', 'region', 'city', 'branch', 'model',
    'status', 'txn_type', 'problem_type', 'state', 'atm_id',
)


def _base_params(dataset: Dataset, window: ReportWindow | None) -> list[Any]:
    if not dataset.base_param_order:
        return []
    if window is None:
        raise ReportDefinitionError('This report requires a reporting period.')
    return [window.end_utc if marker == 'end' else window.start_utc
            for marker in dataset.base_param_order]


def compile_report(definition: Definition, *, totals: bool = False,
                   fetch_limit: int | None = None) -> CompiledReport:
    """Compile a validated definition to parameterized SQL."""
    dataset = definition.dataset
    joins: set[str] = set()
    dimensions: list[Dimension] = []
    measures: list[Measure] = []

    for dim_id in definition.group_by:
        dim = dataset.dimensions[dim_id]
        if not totals:
            # A totals row has no group columns.  Keeping the dimensions in
            # the compiled shape made the renderer label the first measure as
            # a district and shifted every total one column to the left.
            dimensions.append(dim)
            joins.update(dim.joins)
    for measure_id in definition.measures:
        measure = dataset.measures[measure_id]
        measures.append(measure)
        joins.update(measure.joins)

    params: list[Any] = _base_params(dataset, definition.window)
    where: list[str] = []
    if dataset.base_where:
        where.append(dataset.base_where)
    if definition.filters.get('atm_scope', 'active') == 'active':
        where.append("LOWER(COALESCE(l.status, '')) = 'active'")

    for key in FILTER_ORDER:
        value = definition.filters.get(key)
        if value in (None, [], ''):
            continue
        spec = dataset.filters.get(key)
        if spec is None:
            continue
        joins.update(spec.joins)
        if spec.kind == 'multi':
            where.append(spec.expr)
            params.append(value)
        elif spec.kind == 'single':
            where.append(spec.expr)
            params.append(value[0])

    if dataset.time_column and definition.window is not None:
        where.append(f"{dataset.time_column} >= %s")
        where.append(f"{dataset.time_column} < %s")
        params.extend([definition.window.start_utc, definition.window.end_utc])

    join_sql = ''
    if joins:
        join_sql = '\n'.join(dataset.joins[j] for j in dataset.join_order
                             if j in joins)

    select_parts: list[str] = []
    if not totals:
        select_parts.extend(f'{dim.expr} AS "{dim.id}"' for dim in dimensions)
    select_parts.extend(f'{measure.expr} AS "{measure.id}"'
                        for measure in measures)

    sql = 'SELECT ' + ', '.join(select_parts) + '\n' + dataset.base_from + '\n'
    if join_sql:
        sql += join_sql + '\n'
    if where:
        sql += 'WHERE ' + ' AND '.join(f'({clause})' for clause in where) + '\n'

    if not totals:
        if dimensions:
            sql += 'GROUP BY ' + ', '.join(
                str(i + 1) for i in range(len(dimensions))) + '\n'
        sql += f'ORDER BY {_sort_ordinal(definition, dimensions)} ' \
               f'{definition.sort_dir.upper()}\n'
        limit = fetch_limit if fetch_limit is not None else definition.limit
        sql += 'LIMIT %s'
        params.append(limit)

    return CompiledReport(
        sql=sql.rstrip(),
        params=params,
        dimensions=tuple(dimensions),
        measures=tuple(measures),
        totals=totals,
    )


def _sort_ordinal(definition: Definition,
                  dimensions: list[Dimension]) -> int:
    if definition.sort_id in definition.group_by:
        return definition.group_by.index(definition.sort_id) + 1
    return len(dimensions) + definition.measures.index(definition.sort_id) + 1


# ── Execution ───────────────────────────────────────────────────────────────

def _json_value(value: Any, column_type: str,
                value_labels: dict[str, str] | None = None) -> Any:
    if value is None:
        return None
    if column_type == 'datetime':
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(EAT).strftime('%Y-%m-%d %H:%M')
        return str(value)
    if column_type == 'date':
        return value.isoformat() if hasattr(value, 'isoformat') else str(value)
    if column_type in ('number', 'percent', 'currency', 'hours'):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, bool):
            return int(value)
        return value
    text = str(value)
    if value_labels and text in value_labels:
        return value_labels[text]
    return text


def _columns(definition: Definition) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dim_id in definition.group_by:
        dim = definition.dataset.dimensions[dim_id]
        col = {'id': dim.id, 'label': dim.label, 'type': dim.type,
               'kind': 'dimension', 'description': dim.description}
        if dim.value_labels:
            col['value_labels'] = dim.value_labels
        out.append(col)
    for measure_id in definition.measures:
        measure = definition.dataset.measures[measure_id]
        out.append({'id': measure.id, 'label': measure.label,
                    'type': measure.type, 'kind': 'measure',
                    'description': measure.description})
    return out


def _format_rows(compiled: CompiledReport, rows: Iterable[tuple],
                 definition: Definition) -> list[list[Any]]:
    # ``compiled.dimensions`` is the selected subset, already in SELECT order.
    # Indexing the dataset catalog by position here silently paired a value
    # with the wrong column whenever the selection was not the catalog prefix.
    formats = [dim.type for dim in compiled.dimensions]
    formats += [measure.type for measure in compiled.measures]
    labels = [dim.value_labels for dim in compiled.dimensions]
    labels += [None] * len(compiled.measures)
    return [[_json_value(value, formats[i], labels[i])
             for i, value in enumerate(row)] for row in rows]


def _filter_summary(definition: Definition) -> str:
    parts: list[str] = []
    if definition.window is not None:
        parts.append(definition.window.label)
    parts.append('Active ATMs' if definition.filters.get(
        'atm_scope', 'active') == 'active' else 'All ATMs')
    for key in FILTER_ORDER:
        value = definition.filters.get(key)
        if value in (None, [], ''):
            continue
        spec = definition.dataset.filters.get(key)
        if spec is None:
            continue
        values = value if isinstance(value, list) else [value]
        parts.append(f'{spec.label}: {", ".join(values)}')
    if not definition.dataset.requires_period:
        parts = ['Current snapshot'] + [p for p in parts if p != 'Current snapshot']
    if len(parts) > 6:
        parts = parts[:5] + [f'+{len(parts) - 5} more']
    return ' · '.join(parts)


def _title(definition: Definition) -> str:
    if not definition.group_by:
        return f'{definition.dataset.label} summary'
    labels = [definition.dataset.dimensions[d].label
              for d in definition.group_by]
    if len(labels) == 1:
        by = labels[0]
    elif len(labels) == 2:
        by = f'{labels[0]} and {labels[1]}'
    else:
        by = ', '.join(labels[:-1]) + f' and {labels[-1]}'
    return f'{definition.dataset.label} by {by}'


def run_report(conn, definition: Definition, *,
               limit: int | None = None,
               timeout_ms: int = 12000) -> dict[str, Any]:
    """Execute a validated definition and return a renderer-neutral result."""
    # The stored definition owns the row cap.  A caller may ask for less (the
    # browser preview does), never for more than the definition or the hard
    # export ceiling.
    effective_limit = min(
        limit if limit is not None else definition.limit,
        definition.limit,
        MAX_EXPORT_LIMIT,
    )
    compiled = compile_report(definition, fetch_limit=effective_limit + 1)
    started = time.monotonic()
    cur = conn.cursor()
    try:
        # get_db() sets a 30s statement timeout; a preview should fail fast so
        # the builder can explain the problem instead of hanging the page.
        cur.execute(f'SET LOCAL statement_timeout = {int(timeout_ms)}')
        cur.execute(compiled.sql, compiled.params)
        raw_rows = cur.fetchall()
    except Exception as exc:
        raise _execution_error(exc) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass

    truncated = len(raw_rows) > effective_limit
    if truncated:
        raw_rows = raw_rows[:effective_limit]

    rows = _format_rows(compiled, raw_rows, definition)

    totals = None
    totals_compiled = compile_report(definition, totals=True)
    cur = conn.cursor()
    try:
        cur.execute(f'SET LOCAL statement_timeout = {int(timeout_ms)}')
        cur.execute(totals_compiled.sql, totals_compiled.params)
        total_row = cur.fetchone()
        if total_row is not None:
            measure_values = _format_rows(
                totals_compiled, [total_row], definition)[0]
            # Re-align the totals with the visible table: a leading blank for
            # every grouping column, then the measures.  The preview, Excel,
            # CSV and PDF all address totals by the visible column index.
            totals = [None] * len(compiled.dimensions) + measure_values
    except Exception:
        # A totals failure must not lose a report that already has rows, but
        # the failed statement leaves the pooled connection in an aborted
        # transaction.  Roll back before running the data-quality checks.
        try:
            conn.rollback()
        except Exception:
            pass
        totals = None
    finally:
        try:
            cur.close()
        except Exception:
            pass

    warnings = warnings_for(conn, definition)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    period_days = definition.window.days if definition.window else 0

    return {
        'dataset': definition.dataset.id,
        'dataset_label': definition.dataset.label,
        'title': _title(definition),
        'subtitle': _filter_summary(definition),
        'period_label': definition.window.label if definition.window else 'Current snapshot',
        'period_days': period_days,
        'columns': _columns(definition),
        'rows': rows,
        'totals': totals,
        'row_count': len(rows),
        'limit': effective_limit,
        'truncated': truncated,
        'generated_at': dt.datetime.now(EAT).strftime('%Y-%m-%d %H:%M EAT'),
        'duration_ms': elapsed_ms,
        'warnings': warnings,
        'definition_hash': definition.definition_hash,
        'definition': definition.payload(),
    }


def _execution_error(exc: Exception) -> ReportExecutionError:
    name = exc.__class__.__name__
    text = str(exc).lower()
    if name in ('QueryCanceled', 'QueryCanceledError') or 'timeout' in text \
            or 'canceling statement' in text:
        return ReportExecutionError(
            'The report took too long. Narrow the period, add a filter, or '
            'choose fewer groupings.')
    return ReportExecutionError(
        'The report could not be generated. Check the data and try again.')


# ── Data-quality warnings ───────────────────────────────────────────────────

_MISSING_DIMENSIONS = {
    'district': ('district', 'District', 'Unassigned'),
    'region': ('region', 'Region', 'Unassigned'),
    'city': ('city', 'City', 'Unassigned'),
    'branch': ('branch', 'Branch', 'Unassigned'),
    'model': ('model', 'Model', 'Unknown'),
    'vendor': ('vendor', 'Vendor', 'Unknown'),
}

_QUALITY_CACHE: dict[tuple, tuple[float, Any]] = {}
_QUALITY_TTL = 300


def _cached(key: tuple, compute):
    now = time.monotonic()
    hit = _QUALITY_CACHE.get(key)
    if hit and now - hit[0] < _QUALITY_TTL:
        return hit[1]
    value = compute()
    _QUALITY_CACHE[key] = (now, value)
    return value


def _scalar(conn, sql: str, params: tuple = ()) -> Any:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        # A failed check must not leave the shared connection unusable for the
        # remaining warnings (psycopg2 keeps the transaction aborted).
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass


def warnings_for(conn, definition: Definition) -> list[str]:
    """Cheap, honest caveats that belong beside the numbers."""
    warnings: list[str] = []
    dataset = definition.dataset
    used_dims = set(definition.group_by)
    for dim_id, (column, label, bucket) in _MISSING_DIMENSIONS.items():
        if dim_id not in used_dims and dim_id not in definition.filters:
            continue

        def _missing():
            return int(_scalar(
                conn,
                "SELECT COUNT(*) FROM atm_locations "
                "WHERE LOWER(COALESCE(status, '')) = 'active' "
                f"AND ({column} IS NULL OR TRIM({column}) = '')") or 0)

        try:
            missing = _cached(('missing', column), _missing)
        except Exception:
            continue
        if missing:
            warnings.append(
                f'{missing} active ATM{"s" if missing != 1 else ""} have no '
                f'{label.lower()}; they are grouped as {bucket}.')

    problem_used = ('problem_type' in definition.group_by
                    or 'problem_type' in definition.filters)
    if problem_used and dataset.id in ('transactions', 'problems') \
            and definition.window is not None:
        scope_sql = ("AND LOWER(COALESCE(l.status, '')) = 'active'"
                     if definition.filters.get('atm_scope', 'active') == 'active'
                     else '')

        def _unmapped():
            return int(_scalar(
                conn,
                "SELECT COUNT(*) FROM atm_transactions_visible t "
                "JOIN atm_locations l ON l.atm_id = t.atm_id "
                "LEFT JOIN fault_type_map f ON f.vendor = l.vendor "
                "AND f.raw_fault_code = t.error_code "
                "WHERE t.status = 'ERROR' AND t.error_code IS NOT NULL "
                "AND TRIM(t.error_code) <> '' "
                "AND t.recorded_at >= %s AND t.recorded_at < %s "
                f"{scope_sql} AND f.id IS NULL",
                (definition.window.start_utc, definition.window.end_utc))
            or 0)

        try:
            unmapped = _cached(
                ('unmapped', definition.window.start_utc,
                 definition.window.end_utc,
                 definition.filters.get('atm_scope', 'active')),
                _unmapped)
        except Exception:
            unmapped = 0
        if unmapped:
            warnings.append(
                f'{unmapped} error transaction{"s" if unmapped != 1 else ""} '
                'in this period have an unmapped fault code; they appear as '
                'Unmapped.')

    if dataset.id == 'availability' and definition.window is not None:
        def _coverage():
            row = None
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM atm_locations "
                    " WHERE LOWER(COALESCE(status, '')) = 'active'), "
                    "(SELECT COUNT(DISTINCT h.atm_id) FROM atm_state_history h "
                    " WHERE h.started_at < %s "
                    " AND (h.ended_at IS NULL OR h.ended_at > %s))",
                    (definition.window.end_utc, definition.window.start_utc))
                row = cur.fetchone()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

        try:
            active, covered = _cached(
                ('coverage', definition.window.start_utc,
                 definition.window.end_utc), _coverage)
        except Exception:
            active, covered = 0, 0
        if active and covered == 0:
            warnings.append(
                'No state history is available for this period; the report '
                'will be empty.')
        elif active and covered < active:
            warnings.append(
                f'{active - covered} of {active} active ATMs have no state '
                'history in this period; uptime covers only ATMs with data.')

    return warnings


# ── Filter options ──────────────────────────────────────────────────────────

_OPTIONS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_OPTIONS_TTL = 120


def _option_queries() -> dict[str, str]:
    active = "LOWER(COALESCE(status, '')) = 'active'"
    return {
        'vendors': f"SELECT DISTINCT COALESCE(NULLIF(TRIM(vendor), ''), "
                   f"'Unknown') FROM atm_locations WHERE {active} ORDER BY 1",
        'districts': f"SELECT DISTINCT COALESCE(NULLIF(TRIM(district), ''), "
                     f"'Unassigned') FROM atm_locations WHERE {active} ORDER BY 1",
        'regions': f"SELECT DISTINCT COALESCE(NULLIF(TRIM(region), ''), "
                   f"'Unassigned') FROM atm_locations WHERE {active} ORDER BY 1",
        'cities': f"SELECT DISTINCT COALESCE(NULLIF(TRIM(city), ''), "
                  f"'Unassigned') FROM atm_locations WHERE {active} ORDER BY 1",
        'branches': f"SELECT DISTINCT COALESCE(NULLIF(TRIM(branch), ''), "
                    f"'Unassigned') FROM atm_locations WHERE {active} ORDER BY 1",
        'models': f"SELECT DISTINCT COALESCE(NULLIF(TRIM(model), ''), "
                  f"'Unknown') FROM atm_locations WHERE {active} ORDER BY 1",
        'txn_types': "SELECT DISTINCT txn_type FROM atm_transactions_visible "
                     "WHERE recorded_at >= NOW() - INTERVAL '90 days' "
                     "AND txn_type IS NOT NULL AND TRIM(txn_type) <> '' "
                     "ORDER BY 1",
        'problem_types': "SELECT DISTINCT display_name FROM fault_type_map "
                         "ORDER BY 1",
    }


def _fixed_options() -> dict[str, list[dict[str, str]]]:
    return {
        'txn_statuses': [
            {'value': 'APPROVED', 'label': 'Approved'},
            {'value': 'DECLINED', 'label': 'Declined'},
            {'value': 'ERROR', 'label': 'Error'},
        ],
        'atm_statuses': [
            {'value': 'active', 'label': 'Active'},
            {'value': 'inactive', 'label': 'Inactive'},
        ],
        'states': [{'value': code, 'label': STATE_LABELS.get(code, code)}
                   for code in STATE_ORDER],
        'scope': [
            {'value': 'active', 'label': 'Active ATMs'},
            {'value': 'all', 'label': 'All ATMs'},
        ],
    }


def filter_options(conn, sources: Iterable[str] | None = None,
                   force: bool = False) -> dict[str, Any]:
    """Return the current values for each filter source.

    Options are advisory: the compiler still validates every value as a bound
    parameter.  A missing or failed query returns an empty list and a warning,
    never a 500 that makes the whole builder unusable.
    """
    wanted = set(sources or ())
    fixed = _fixed_options()
    out: dict[str, Any] = {}
    warnings: list[str] = []
    now = time.monotonic()

    for source, values in fixed.items():
        if not wanted or source in wanted:
            out[source] = list(values)

    queries = _option_queries()
    for source, sql in queries.items():
        if wanted and source not in wanted:
            continue
        cached = _OPTIONS_CACHE.get(source)
        if not force and cached and now - cached[0] < _OPTIONS_TTL:
            out[source] = list(cached[1])
            continue
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                rows = cur.fetchall()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            values = [{'value': str(r[0]), 'label': str(r[0])}
                      for r in rows if r and r[0] is not None]
            _OPTIONS_CACHE[source] = (now, values)
            out[source] = list(values)
        except Exception:
            out[source] = []
            warnings.append(f'Could not load {source.replace("_", " ")}.')

    if not wanted or 'atms' in wanted:
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT atm_id, COALESCE(NULLIF(TRIM(branch), ''), "
                    "'Unknown branch') FROM atm_locations "
                    "WHERE LOWER(COALESCE(status, '')) = 'active' "
                    "ORDER BY atm_id")
                rows = cur.fetchall()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            out['atms'] = [
                {'value': str(r[0]),
                 'label': f'{r[0]} — {r[1]}' if r[1] else str(r[0])}
                for r in rows if r and r[0] is not None]
        except Exception:
            out['atms'] = []
            warnings.append('Could not load the ATM list.')

    if warnings:
        out['warnings'] = warnings
    return out


# ── Catalog payload ─────────────────────────────────────────────────────────

def catalog_payload() -> dict[str, Any]:
    """JSON-friendly catalog for the builder UI.  Never includes SQL."""
    datasets = []
    for dataset in DATASETS.values():
        datasets.append({
            'id': dataset.id,
            'label': dataset.label,
            'description': dataset.description,
            'grain': dataset.grain,
            'requires_period': dataset.requires_period,
            'default_group_by': list(dataset.default_group_by),
            'default_measures': list(dataset.default_measures),
            'default_period': dataset.default_period,
            'default_atm_scope': dataset.default_atm_scope,
            'default_sort': {'id': dataset.default_sort_id,
                             'direction': dataset.default_sort_dir},
            'dimensions': [
                {'id': dim.id, 'label': dim.label, 'type': dim.type,
                 'description': dim.description,
                 'value_labels': dim.value_labels or {}}
                for dim in dataset.dimensions.values()
            ],
            'measures': [
                {'id': measure.id, 'label': measure.label,
                 'type': measure.type, 'description': measure.description}
                for measure in dataset.measures.values()
            ],
            'filters': [
                {'id': spec.id, 'label': spec.label, 'kind': spec.kind,
                 'group': spec.group, 'description': spec.description,
                 'options_source': spec.options_source,
                 'options': [{'value': v, 'label': l}
                             for v, l in spec.static_options]}
                for spec in dataset.filters.values()
            ],
        })
    return {
        'datasets': datasets,
        'period_presets': [{'value': value, 'label': label}
                           for value, label in PERIOD_PRESETS],
        'limits': {
            'max_range_days': MAX_RANGE_DAYS,
            'max_group_by': MAX_GROUP_BY,
            'max_measures': MAX_MEASURES,
            'max_preview_limit': MAX_PREVIEW_LIMIT,
            'preview_rows': PREVIEW_ROW_LIMIT,
            'max_export_limit': MAX_EXPORT_LIMIT,
            'default_limit': DEFAULT_LIMIT,
        },
    }
