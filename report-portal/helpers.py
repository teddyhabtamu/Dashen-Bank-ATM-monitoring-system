from flask import send_file
import openpyxl, csv, io, os
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.styles.differential import DifferentialStyle
from datetime import datetime, timedelta, timezone
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                Paragraph, Spacer, HRFlowable, PageBreak, Image)
from reportlab.lib.enums import TA_CENTER

LOGO_PATH = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')
EAT = timezone(timedelta(hours=3))

DASHEN_BLUE  = '273274'
DASHEN_GOLD  = 'FDD79A'
DASHEN_NAVY  = '012169'
ALT_ROW      = 'F8FAFC'
BORDER_COLOR = 'E2E8F0'


# ─── EXCEL HELPERS ─────────────────────────────────────────────────────────────

def xl_header(ws, title, days, atm, ncols=8, subtitle=None, period=None):
    """Write the branded header block (rows 1-3) plus a subtitle or spacer row 4."""
    last = get_column_letter(ncols)
    ws.merge_cells(f'A1:{last}1')
    c = ws['A1']
    c.value = 'DASHEN BANK S.C. — ATM MONITORING SYSTEM'
    c.font = Font(bold=True, size=13, color=DASHEN_BLUE)
    c.fill = PatternFill('solid', fgColor='FFFDE7')
    c.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 22

    ws.merge_cells(f'A2:{last}2')
    c = ws['A2']
    c.value = title
    c.font = Font(bold=True, size=11, color=DASHEN_BLUE)
    c.fill = PatternFill('solid', fgColor='FFFDE7')
    c.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 18

    ws.merge_cells(f'A3:{last}3')
    c = ws['A3']
    period_label = period or f'Last {days} days'
    c.value = (
        f'Period: {period_label}  |  '
        f'ATM: {"All ATMs" if atm == "all" else atm}  |  '
        f'Generated: {datetime.now(EAT).strftime("%Y-%m-%d %H:%M")} EAT'
    )
    c.font = Font(size=9, color='64748B', italic=True)
    c.fill = PatternFill('solid', fgColor='FFFDE7')
    c.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[3].height = 14

    ws.merge_cells(f'A4:{last}4')
    c = ws['A4']
    if subtitle:
        c.value = subtitle
        c.font = Font(size=9, color='475569', italic=True)
    c.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[4].height = 14 if subtitle else 6


def xl_style_row(ws, row, ncols):
    """Style the column-header row: dark blue background, white bold text."""
    thin = Side(style='thin', color=BORDER_COLOR)
    for col in range(1, ncols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = Font(bold=True, color='FFFFFF', size=10)
        cell.fill = PatternFill('solid', fgColor=DASHEN_BLUE)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws.row_dimensions[row].height = 20


def xl_style_data_rows(ws, start_row, ncols):
    """Apply alternating row colours and borders to data rows."""
    thin = Side(style='thin', color=BORDER_COLOR)
    for row in ws.iter_rows(min_row=start_row, max_row=ws.max_row, max_col=ncols):
        is_alt = (row[0].row - start_row) % 2 == 1
        bg = ALT_ROW if is_alt else 'FFFFFF'
        for cell in row:
            cell.fill = PatternFill('solid', fgColor=bg)
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            cell.font = Font(size=9)


def xl_autosize(ws):
    for col in ws.columns:
        mx = max((len(str(c.value or '')) for c in col), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(mx + 4, 52)


def _period_slug(period):
    if not period:
        return ''
    slug = ''.join(ch if ch.isalnum() else '_' for ch in period)
    while '__' in slug:
        slug = slug.replace('__', '_')
    return '_' + slug.strip('_')


def xl_send(wb, name, period=None):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'Dashen_ATM_{name}_{datetime.now(EAT).strftime("%Y%m%d")}{_period_slug(period)}.xlsx'
    )


def xl_finalize(ws, header_row, ncols):
    """Filter dropdowns on the column-header row + freeze panes below it,
    so the team can sort/filter without setup clicks."""
    last = get_column_letter(ncols)
    ws.auto_filter.ref = f'A{header_row}:{last}{header_row}'
    ws.freeze_panes = f'A{header_row + 1}'


def xl_build_sheet(wb, sheet_name, title, days, atm, headers, rows, subtitle=None,
                   period=None):
    """Utility: create a fully styled sheet inside an existing workbook."""
    ws = wb.create_sheet(sheet_name)
    xl_header(ws, title, days, atm, ncols=len(headers), subtitle=subtitle,
              period=period)
    ws.append(headers)
    xl_style_row(ws, ws.max_row, len(headers))
    data_start = ws.max_row + 1
    for r in rows:
        ws.append([str(v) if v is not None else '—' for v in r])
    xl_style_data_rows(ws, data_start, len(headers))
    xl_autosize(ws)
    xl_finalize(ws, data_start - 1, len(headers))
    return ws


def xl_append_section(ws, section_title, headers, rows):
    """Utility: append a styled section to an existing sheet."""
    if ws.max_row > 5:  # If there is already data (past the header)
        ws.append([])
        ws.append([])

    title_row = ws.max_row + 1
    ws.append([section_title])
    c = ws.cell(row=title_row, column=1)
    c.font = Font(bold=True, size=11, color=DASHEN_BLUE)

    headers = ['S.N'] + list(headers)
    rows = [[i + 1] + list(r) for i, r in enumerate(rows)]
    ws.append(headers)
    hdr_row = ws.max_row
    xl_style_row(ws, hdr_row, len(headers))

    data_start = ws.max_row + 1
    for r in rows:
        ws.append([str(v) if v is not None else '—' for v in r])
    xl_style_data_rows(ws, data_start, len(headers))



# ─── CSV HELPERS ───────────────────────────────────────────────────────────────

def csv_send(headers, rows, name, title='', days=0, atm='all', subtitle='',
             period=None):
    """Write a CSV with a branded metadata block at the top."""
    buf = io.StringIO()
    w = csv.writer(buf)
    # Branded header block
    w.writerow(['DASHEN BANK S.C. — ATM MONITORING SYSTEM'])
    if title:
        w.writerow([title])
    if subtitle:
        w.writerow([subtitle])
    period_label = period or (f'Last {days} days' if days else '')
    w.writerow([
        f'Period: {period_label}' if period_label else '',
        f'ATM: {"All ATMs" if atm == "all" else atm}',
        f'Generated: {datetime.now(EAT).strftime("%Y-%m-%d %H:%M")} EAT'
    ])
    w.writerow([])  # blank spacer
    w.writerow(headers)
    w.writerows([[str(v) if v is not None else '—' for v in r] for r in rows])
    w.writerow([])
    w.writerow(['Dashen Bank ATM Monitoring System | Confidential'])
    buf.seek(0)
    return send_file(
        io.BytesIO(buf.getvalue().encode('utf-8-sig')),  # utf-8-sig for Excel compatibility
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'Dashen_ATM_{name}_{datetime.now(EAT).strftime("%Y%m%d")}{_period_slug(period)}.csv'
    )


# ─── PDF HELPERS ───────────────────────────────────────────────────────────────

def _kpi_value_metrics(value):
    """Pick a font size and line-height that fit one KPI card.

    The period KPI can be a full label such as
    ``2026-10-09 to 2026-11-07 (EAT)``.  Setting only ``<font size="22">``
    left ReportLab's default 12pt leading in place, so the two wrapped lines
    printed on top of each other.  Long values now shrink and always carry a
    leading larger than their font size.
    """
    length = len(str(value))
    if length > 22:
        return 11, 14
    if length > 14:
        return 15, 19
    return 22, 26


def _pdf_kpi_block(story, kpis):
    """Append a premium KPI card section with Dashen Bank branding."""
    from xml.sax.saxutils import escape as _xml_escape

    kpis = list(kpis)[:4]
    ncols = min(len(kpis), 4)
    page_w = landscape(A4)[0] - 3 * cm
    cw = page_w / ncols

    label_style = ParagraphStyle('kl', fontName='Helvetica-Bold',
                                 fontSize=9, leading=11,
                                 alignment=TA_CENTER)
    Kl = lambda txt: Paragraph(
        f'<font color="white">{_xml_escape(str(txt))}</font>',
        label_style)
    value_styles = {}

    def Kv(txt):
        size, leading = _kpi_value_metrics(txt)
        style = value_styles.get(size)
        if style is None:
            style = ParagraphStyle(f'kv{size}', fontName='Helvetica-Bold',
                                   fontSize=size, leading=leading,
                                   alignment=TA_CENTER)
            value_styles[size] = style
        return Paragraph(
            f'<font color="#012169">{_xml_escape(str(txt))}</font>', style)

    labels_row = [Kl(label) for label, _ in kpis]
    values_row = [Kv(value) for _, value in kpis]

    while len(labels_row) < ncols:
        labels_row.append(Paragraph('', ParagraphStyle('_e')))
        values_row.append(Paragraph('', ParagraphStyle('_e')))

    t = Table([labels_row, values_row], colWidths=[cw] * ncols)
    t.setStyle(TableStyle([
        ('ALIGN',       (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND',  (0, 0), (-1,  0), colors.HexColor('#273274')),
        ('TEXTCOLOR',   (0, 0), (-1,  0), colors.white),
        ('TOPPADDING',  (0, 0), (-1,  0), 14),
        ('BOTTOMPADDING', (0, 0), (-1,  0), 10),
        ('BACKGROUND',  (0, 1), (-1,  1), colors.white),
        ('TOPPADDING',  (0, 1), (-1,  1), 16),
        ('BOTTOMPADDING', (0, 1), (-1,  1), 20),
        ('LINEABOVE',   (0, 1), (-1,  1), 3, colors.HexColor('#FDD79A')),
        ('INNERGRID',   (0, 0), (-1, -1), 0, colors.white),
    ] + [
        ('BOX', (col, 0), (col, 1), 0.5, colors.HexColor('#E2E8F0'))
        for col in range(ncols)
    ]))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        '<font color="#012169" size="14"><b>Performance Summary</b></font>',
        ParagraphStyle('sec', spaceBefore=4, spaceAfter=2)))
    story.append(HRFlowable(width=4*cm, thickness=2.5,
                             color=colors.HexColor('#FDD79A'), spaceAfter=14))
    story.append(t)
    story.append(Spacer(1, 0.8 * cm))


def _pdf_header_block(story, title, days, atm, period=None):
    """Shared PDF header: logo + bank name + gold rule + report title + metadata."""
    from xml.sax.saxutils import escape as _xml_escape
    title = _xml_escape(str(title or ''))
    atm = _xml_escape(str(atm or ''))
    period = _xml_escape(str(period)) if period else None
    if os.path.exists(LOGO_PATH):
        img = Image(LOGO_PATH, width=5 * cm, height=1.5 * cm, kind='proportional')
        img.hAlign = 'CENTER'
        story.append(img)
        story.append(Spacer(1, 0.3 * cm))

    # Explicit styles to avoid overlapping
    style_bank = ParagraphStyle('_h1', fontSize=16, leading=20, alignment=TA_CENTER, spaceAfter=2)
    style_sys  = ParagraphStyle('_h2', fontSize=9, leading=12, alignment=TA_CENTER, spaceAfter=6)
    style_title = ParagraphStyle('_h3', fontSize=12, leading=15, alignment=TA_CENTER, spaceAfter=6)
    style_meta = ParagraphStyle('_h4', fontSize=8, leading=10, alignment=TA_CENTER, spaceAfter=6)

    story += [
        Paragraph('<b><font color="#012169">DASHEN BANK S.C.</font></b>', style_bank),
        Paragraph('<font color="#273274">ATM MONITORING SYSTEM</font>', style_sys),
        HRFlowable(width='70%', thickness=2.5, color=colors.HexColor('#FDD79A'), spaceAfter=10),
        Paragraph(f'<b><font color="#273274">{title}</font></b>', style_title),
        Paragraph(f'<font color="#64748B">Period: {period or f"Last {days} days"}  |  '
                  f'ATM: {"All ATMs" if atm == "all" else atm}  |  '
                  f'Generated: {datetime.now(EAT).strftime("%Y-%m-%d %H:%M")} EAT</font>', style_meta),
        Spacer(1, 0.2 * cm),
    ]


def pdf_send(title, headers, rows, days, atm, name, kpis=None, subtitle='',
             period=None, notes=None):
    """Generate a single-section branded PDF report with optional KPI summary page.

    ``notes`` is used by the custom-report exporter for data-quality caveats
    (missing district, unmapped fault codes, truncated rows).  They are printed
    after the table so a mailed PDF carries the same honesty as the portal.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            rightMargin=1.5 * cm, leftMargin=1.5 * cm,
                            topMargin=2 * cm, bottomMargin=1.5 * cm)
    P = lambda txt, **kw: Paragraph(txt, ParagraphStyle('_p', **kw))

    story = []
    _pdf_header_block(story, title, days, atm, period=period)
    if subtitle:
        from xml.sax.saxutils import escape as _xml_escape
        story.append(P(f'<font color="#475569"><i>'
                       f'{_xml_escape(str(subtitle))}</i></font>',
                       alignment=TA_CENTER))
        story.append(Spacer(1, 0.3 * cm))

    if kpis:
        _pdf_kpi_block(story, kpis)
        story.append(PageBreak())
        # Re-use same header on the data page
        _pdf_header_block(story, title, days, atm, period=period)

    story.append(mktable(headers, rows))
    if notes:
        from xml.sax.saxutils import escape as _xml_escape
        story.append(Spacer(1, 0.35 * cm))
        for note in notes:
            story.append(P(
                f'<font size="7" color="#92400E">Note: '
                f'{_xml_escape(str(note))}</font>',
                alignment=TA_CENTER
            ))
    story.append(Spacer(1, 0.5 * cm))
    story.append(P(
        f'<font size="7" color="#94A3B8">Dashen Bank ATM Monitoring System  |  Confidential  |  '
        f'{datetime.now(EAT).strftime("%Y-%m-%d")}</font>',
        alignment=TA_CENTER
    ))
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=f'Dashen_ATM_{name}_{datetime.now(EAT).strftime("%Y%m%d")}{_period_slug(period)}.pdf')


def mktable(headers, rows):
    """Build a fully styled reportlab Table for use in PDFs.

    Cells are Paragraphs (not raw strings) so long text WRAPS instead of
    overprinting its neighbours; row heights size to content. Fixed
    ROWHEIGHT + raw strings was the overlap bug in mailed PDFs.
    """
    from xml.sax.saxutils import escape
    hdr_style = ParagraphStyle('_mth', fontName='Helvetica-Bold', fontSize=9,
                               leading=11, textColor=colors.white, alignment=TA_CENTER)
    cell_style = ParagraphStyle('_mtb', fontName='Helvetica', fontSize=8,
                                leading=10, alignment=TA_CENTER)
    data = [[Paragraph(escape(str(h)), hdr_style) for h in headers]]
    for r in rows:
        data.append([Paragraph(escape(str(v) if v is not None else '—'), cell_style)
                     for v in r])
    page_w = landscape(A4)[0] - 3 * cm
    cw = page_w / len(headers)
    t = Table(data, colWidths=[cw] * len(headers), repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1,  0), colors.HexColor('#273274')),
        ('TEXTCOLOR',     (0, 0), (-1,  0), colors.white),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('GRID',          (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    return t


# How loud a state should look. Two tiers of "not healthy" were previously
# indistinguishable: an ATM that is genuinely down, and one we simply cannot
# read yet. Painting both red is how a monitoring tool trains people to ignore
# red.
STATE_TONES = {
    'IN_SERVICE': 'good',
    'IN_SUPERVISOR': 'warn',
    'OUT_OF_CASH': 'warn',
    'OUT_OF_SERVICE': 'bad',
    'OFFLINE': 'bad',
    'HARDWARE_FAULT': 'bad',
    'UNREACHABLE': 'bad',
    # "We do not know" is not an alarm. Not provisioned is a setup gap, not an
    # outage; a demo or an overnight install should not look like a failure.
    'UNKNOWN': 'muted',
    'NOT_PROVISIONED': 'muted',
    'INACTIVE': 'muted',
}

TONE_COLOURS = {
    'good': '#059669',
    'warn': '#D97706',
    'bad': '#DC2626',
    'muted': '#64748B',
}

# The eight operator-facing ATM states, matching the NetXMS colour language
# the bank's operations team already knows. STATE_TONES above stays the
# coarse good/warn/bad/muted tier used for layout (CSS classes, alert cards);
# these maps are the single source of truth for state text and colour.
#
# UNREACHABLE is displayed as "Agent Disconnected" because that is what it
# means operationally: Zabbix has positive evidence the agent is unavailable
# (power or network). The internal key is unchanged so atm_state_history,
# triggers and existing rows keep their meaning.
STATE_LABELS = {
    'IN_SERVICE': 'In Service',
    'UNREACHABLE': 'Agent Disconnected',
    'UNKNOWN': 'Unknown',
    'IN_SUPERVISOR': 'In Supervisor',
    'HARDWARE_FAULT': 'Hardware Faults',
    'OUT_OF_CASH': 'Out Of Cash',
    'OFFLINE': 'Offline',
    'OUT_OF_SERVICE': 'Out of Service',
    # Portal-only states, not part of the NetXMS eight.
    'NOT_PROVISIONED': 'Not Provisioned',
    'INACTIVE': 'Inactive',
}

STATE_COLOURS = {
    'IN_SERVICE': '#059669',       # green
    'UNREACHABLE': '#BE185D',      # dark pink
    'UNKNOWN': '#F9A8D4',          # light pink
    'IN_SUPERVISOR': '#CA8A04',    # yellow
    'HARDWARE_FAULT': '#1D4ED8',   # dark blue
    'OUT_OF_CASH': '#0F4C5C',      # deep petrol blue-green
    'OFFLINE': '#38BDF8',          # light blue
    'OUT_OF_SERVICE': '#DC2626',   # red
    'NOT_PROVISIONED': '#A5B4CB',
    'INACTIVE': '#94A3B8',
}

# White text disappears on light pink / light blue / yellow. Dark ink keeps
# the badge legible without changing the state colour itself.
STATE_INKS = {
    'UNKNOWN': '#831843',
    'IN_SUPERVISOR': '#1C1917',
    'OFFLINE': '#0C4A6E',
    'NOT_PROVISIONED': '#1E293B',
    'INACTIVE': '#1E293B',
}
STATE_INK_DEFAULT = '#FFFFFF'

# Order used by the on-screen legend — exactly the eight NetXMS states.
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


def _state_key(state):
    return str(state or '').strip().upper()


def state_tone(state):
    """good / warn / bad / muted for an ATM state string."""
    return STATE_TONES.get(_state_key(state), 'muted')


def state_label(state):
    """Operator-facing text for a state key."""
    key = _state_key(state)
    return STATE_LABELS.get(key, key.replace('_', ' ').title() or 'Unknown')


def state_colour(state):
    """NetXMS-parity colour for a state key."""
    return STATE_COLOURS.get(_state_key(state), '#64748B')


def state_ink(state):
    """Readable text colour on top of state_colour()."""
    return STATE_INKS.get(_state_key(state), STATE_INK_DEFAULT)


def state_legend():
    """The eight state legend entries, in NetXMS display order."""
    return [{'state': key, 'label': state_label(key),
             'colour': state_colour(key), 'ink': state_ink(key)}
            for key in STATE_ORDER]
