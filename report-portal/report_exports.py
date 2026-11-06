"""Render a report-engine result to Excel, CSV or PDF.

The renderer is deliberately separate from the engine so the same result can
be previewed in the browser and exported without a second query path.  Every
text cell is passed through ``_safe_text``: a branch name beginning with ``=``
must not become a formula when an analyst opens the workbook.
"""

from __future__ import annotations

import io
from datetime import datetime

from flask import send_file
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from helpers import (csv_send, pdf_send, xl_autosize, xl_finalize,
                     xl_header, xl_style_data_rows, xl_style_row)
from report_engine import EAT

# Excel and CSV formula injection: a leading apostrophe forces text.
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

_TOTAL_FILL = PatternFill('solid', fgColor='EEF2FF')
_TOTAL_FONT = Font(bold=True, color='012169')
_TOTAL_BORDER = Border(top=Side(style='medium', color='012169'))


def _safe_text(value):
    if value is None:
        return ''
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _display_value(value, column):
    """Formatted string for CSV, PDF and the preview-style table."""
    if value is None:
        return ''
    kind = column.get('type')
    if kind == 'percent':
        try:
            return f'{float(value):,.2f}%'
        except (TypeError, ValueError):
            return _safe_text(value)
    if kind == 'currency':
        try:
            return f'{float(value):,.2f}'
        except (TypeError, ValueError):
            return _safe_text(value)
    if kind == 'hours':
        try:
            return f'{float(value):,.1f}'
        except (TypeError, ValueError):
            return _safe_text(value)
    if kind == 'number':
        try:
            number = float(value)
        except (TypeError, ValueError):
            return _safe_text(value)
        if number.is_integer():
            return f'{int(number):,}'
        return f'{number:,.2f}'
    return _safe_text(value)


def _excel_value(value, column):
    if value is None:
        return None
    kind = column.get('type')
    if kind in ('number', 'percent', 'currency', 'hours'):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return _safe_text(value)
        return int(number) if number.is_integer() and kind == 'number' else number
    return _safe_text(value)


def _number_format(column):
    kind = column.get('type')
    if kind == 'percent':
        return '0.00"%"'
    if kind == 'currency':
        return '#,##0.00'
    if kind == 'hours':
        return '#,##0.0'
    if kind == 'number':
        return '#,##0.00'
    return None


def _headers(result):
    return [column['label'] for column in result['columns']]


def _notes(result):
    notes = list(result.get('warnings') or [])
    if result.get('truncated'):
        notes.append(
            f'Showing the first {result.get("limit", 0):,} rows. Narrow the '
            'period or add a filter to include the rest.')
    return notes


def _sheet_name(result):
    name = (result.get('dataset_label') or 'Report').strip()
    # Excel sheet names: max 31 chars, no : \ / ? * [ ]
    for ch in ':\\/?*[]':
        name = name.replace(ch, ' ')
    return (name or 'Report')[:31]


def build_workbook(result):
    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_name(result)

    columns = result['columns']
    headers = _headers(result)
    ncols = len(headers)
    days = result.get('period_days') or 0
    atm_label = result.get('atm_label') or 'Filtered'

    xl_header(ws, _safe_text(result.get('title') or 'Custom Report'), days,
              _safe_text(atm_label), ncols=ncols,
              subtitle=_safe_text(result.get('subtitle')),
              period=_safe_text(result.get('period_label')))

    ws.append(headers)
    header_row = ws.max_row
    xl_style_row(ws, header_row, ncols)

    data_start = ws.max_row + 1
    for row in result.get('rows') or []:
        ws.append([_excel_value(value, columns[i])
                   for i, value in enumerate(row)])

    if result.get('rows'):
        xl_style_data_rows(ws, data_start, ncols)

    # Right-align numeric columns and set their display format.
    for idx, column in enumerate(columns, start=1):
        fmt = _number_format(column)
        if fmt:
            for row_idx in range(data_start, ws.max_row + 1):
                cell = ws.cell(row=row_idx, column=idx)
                cell.number_format = fmt
                cell.alignment = Alignment(horizontal='right')

    totals = result.get('totals')
    if totals and result.get('columns') and len(columns) > 0:
        # A totals row is only meaningful when the report has groups.  With no
        # grouping the single data row already is the total.
        dimension_count = sum(1 for c in columns if c.get('kind') == 'dimension')
        if dimension_count:
            total_row = []
            for idx, column in enumerate(columns):
                if idx == 0:
                    total_row.append('Total')
                elif column.get('kind') == 'dimension':
                    total_row.append(None)
                else:
                    total_row.append(_excel_value(totals[idx], column))
            ws.append(total_row)
            total_idx = ws.max_row
            for idx, column in enumerate(columns, start=1):
                cell = ws.cell(row=total_idx, column=idx)
                cell.fill = _TOTAL_FILL
                cell.font = _TOTAL_FONT
                cell.border = _TOTAL_BORDER
                fmt = _number_format(column)
                if fmt:
                    cell.number_format = fmt
                    cell.alignment = Alignment(horizontal='right')

    note_rows = _notes(result)
    if note_rows:
        ws.append([])
        for note in note_rows:
            ws.append([f'Note: {_safe_text(note)}'])
            row_idx = ws.max_row
            cell = ws.cell(row=row_idx, column=1)
            cell.font = Font(size=9, italic=True, color='92400E')
            cell.alignment = Alignment(wrap_text=True, vertical='top')
            # Merge each note row on its own; merging a block would discard
            # every value except the top-left one.
            if ncols > 1:
                ws.merge_cells(start_row=row_idx, start_column=1,
                               end_row=row_idx,
                               end_column=max(1, min(ncols, 6)))

    xl_autosize(ws)
    xl_finalize(ws, header_row, ncols)
    return wb


def render_excel(result):
    wb = build_workbook(result)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = _filename(result, 'xlsx')
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )


def render_csv(result):
    headers = _headers(result)
    columns = result['columns']
    rows = [[_display_value(value, columns[i])
             for i, value in enumerate(row)]
            for row in (result.get('rows') or [])]

    totals = result.get('totals')
    dimension_count = sum(1 for c in columns if c.get('kind') == 'dimension')
    if totals and dimension_count:
        total_row = []
        for idx, column in enumerate(columns):
            if idx == 0:
                total_row.append('Total')
            elif column.get('kind') == 'dimension':
                total_row.append('')
            else:
                total_row.append(_display_value(totals[idx], column))
        rows.append(total_row)

    for note in _notes(result):
        rows.append([f'Note: {note}'])

    return csv_send(
        headers, rows, 'Custom_Report',
        title=_safe_text(result.get('title') or 'Custom Report'),
        days=result.get('period_days') or 0,
        atm=_safe_text(result.get('atm_label') or 'Filtered'),
        subtitle=_safe_text(result.get('subtitle') or ''),
        period=_safe_text(result.get('period_label')),
    )


def render_pdf(result):
    headers = _headers(result)
    columns = result['columns']
    rows = [[_display_value(value, columns[i])
             for i, value in enumerate(row)]
            for row in (result.get('rows') or [])]

    totals = result.get('totals')
    dimension_count = sum(1 for c in columns if c.get('kind') == 'dimension')
    if totals and dimension_count:
        total_row = []
        for idx, column in enumerate(columns):
            if idx == 0:
                total_row.append('Total')
            elif column.get('kind') == 'dimension':
                total_row.append('')
            else:
                total_row.append(_display_value(totals[idx], column))
        rows.append(total_row)

    # PDF is a management summary; keep the KPI block short.  The period and
    # the row count are the two facts a reader needs to trust the table.
    kpis = [
        ('Rows', f'{result.get("row_count", 0):,}'),
        ('Period', result.get('period_label') or '—'),
        ('Subject', result.get('dataset_label') or '—'),
        ('Generated', datetime.now(EAT).strftime('%Y-%m-%d')),
    ]

    return pdf_send(
        result.get('title') or 'Custom Report', headers, rows,
        result.get('period_days') or 0,
        result.get('atm_label') or 'Filtered',
        'Custom_Report',
        kpis=kpis,
        subtitle=result.get('subtitle') or '',
        period=result.get('period_label'),
        notes=_notes(result),
    )


def _filename(result, extension):
    title = (result.get('title') or 'Custom_Report').replace(' ', '_')
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in title)
    while '__' in safe:
        safe = safe.replace('__', '_')
    stamp = datetime.now(EAT).strftime('%Y%m%d')
    return f'Dashen_ATM_{safe.strip("_")}_{stamp}.{extension}'


def render(fmt, result):
    if fmt == 'excel':
        return render_excel(result)
    if fmt == 'csv':
        return render_csv(result)
    if fmt == 'pdf':
        return render_pdf(result)
    raise ValueError(f'Unsupported export format {fmt!r}.')
