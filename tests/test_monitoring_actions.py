"""Tests for acknowledging problems from the Monitoring Center.

The page was read-only; this is its first write path. Two things matter more
than the happy case: the bitmask must be Zabbix's (a wrong value silently does
something else), and the action UI must survive the 20-second auto-refresh
that replaces every row.
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import problem_actions as pa  # noqa: E402


# ── payload ────────────────────────────────────────────────────────────────
def test_acknowledge_uses_the_zabbix_bitmask():
    assert pa.ack_params('1700000001', 'acknowledge') == {
        'eventids': ['1700000001'], 'action': 2}


def test_unacknowledge_is_a_different_bit():
    """16, not the inverse of 2. Zabbix treats these as independent bits."""
    assert pa.ack_params('1700000001', 'unacknowledge') == {
        'eventids': ['1700000001'], 'action': 16}


def test_an_unknown_action_never_reaches_the_api():
    """An unrecognised name must fail here, not become a 0 bitmask that Zabbix
    rejects with a message the operator cannot act on."""
    with pytest.raises(ValueError):
        pa.ack_params('123', 'delete')
    with pytest.raises(ValueError):
        pa.ack_params('123', '')


def test_a_non_numeric_event_id_is_rejected():
    """The id comes from the URL path and goes straight into a request."""
    for bad in ('1 OR 1=1', '../1', '', None, '12a'):
        with pytest.raises(ValueError):
            pa.ack_params(bad, 'acknowledge')


def test_an_integer_event_id_is_accepted():
    assert pa.ack_params(123, 'acknowledge')['eventids'] == ['123']


# ── route ──────────────────────────────────────────────────────────────────
def _monitoring_src():
    return (REPO / 'report-portal' / 'blueprints' / 'monitoring.py').read_text()


def test_the_action_route_is_post_and_operator_and_above():
    src = _monitoring_src()
    head = src[src.index("@bp.route('/problems/"):src.index('def problem_action')]

    assert "methods=['POST']" in head
    assert 'ROLE_OPERATOR' in head and 'ROLE_ADMIN' in head
    assert 'ROLE_VIEWER' not in head, 'a viewer must not be able to write'


def test_viewers_do_not_get_the_button():
    src = _monitoring_src()
    assert "can_act=session.get('role') in (ROLE_OPERATOR, ROLE_ADMIN)" in src

    tpl = (REPO / 'report-portal' / 'templates' / 'monitoring_center.html').read_text()
    assert '{% if can_act %}' in tpl
    assert 'MC_CAN_ACT' in tpl


def test_a_successful_action_drops_the_read_cache():
    """The redirect lands on a 20s cache. Without this the Ack column still
    shows the old value and the click looks like it did nothing."""
    src = _monitoring_src()
    fn = src[src.index('def problem_action'):]
    fn = fn[:fn.index('@bp.route')]

    assert "_cache['ts'] = 0.0" in fn
    assert 'log_action(' in fn, 'the human actor is only recorded here'


def test_the_redirect_cannot_leave_the_blueprint():
    """`back` is a form field, so it must not become an open redirect."""
    src = _monitoring_src()
    fn = src[src.index('def _back'):src.index('def problem_action')]

    assert "startswith('/monitoring')" in fn


def test_the_auto_refresh_keeps_the_action_button():
    """The 20s re-render replaces every row from JSON. If that path does not
    build the action cell, the buttons vanish after the first tick - the most
    likely way this feature regresses, and invisible to a manual click test
    performed in the first 20 seconds."""
    tpl = (REPO / 'report-portal' / 'templates' / 'monitoring_center.html').read_text()
    js = tpl[tpl.index('pb.innerHTML'):tpl.index('var hb')]

    assert 'mc-act' in js, 'the re-rendered row needs the action cell'
    assert 'MC_CAN_ACT' in js, 'and it must respect the role flag'
    assert 'colspan="7"' in js, 'the empty row spans the new column count'


def test_every_row_has_the_same_number_of_cells():
    """Header, server-rendered row, JS row and empty row must agree, or the
    Action column lands under the wrong heading."""
    tpl = (REPO / 'report-portal' / 'templates' / 'monitoring_center.html').read_text()

    header = tpl[tpl.index('<thead><tr>'):tpl.index('</tr></thead>')]
    assert header.count('<th>') == 7
    assert '<th>Ack</th>' in header
    assert '<th>Suppression</th>' in header
    assert '<th>Action</th>' in header


# ── comment / confirmation ─────────────────────────────────────────────────
def test_a_comment_is_sent_with_the_acknowledge_bit():
    """Zabbix stores the note in the problem's own event history, next to the
    acknowledgement - the trail an operator reads later, unlike the portal's
    audit row which only knows that someone clicked."""
    p = pa.ack_params('123', 'acknowledge', 'Customer reported; dispenser replaced.')

    assert p['action'] == 6, '2 = acknowledge, 4 = add message'
    assert p['message'] == 'Customer reported; dispenser replaced.'


def test_an_empty_comment_does_not_set_the_message_bit():
    """Bit 4 without a message is an error from Zabbix, and an empty textarea
    is the common case - it must collapse to a plain acknowledge."""
    for blank in ('', '   ', None):
        assert pa.ack_params('123', 'acknowledge', blank) == {
            'eventids': ['123'], 'action': 2}


def test_a_comment_on_unacknowledge_is_rejected():
    """The UI never offers the field for it; accepting one silently would set
    a bit the caller did not mean."""
    with pytest.raises(ValueError):
        pa.ack_params('123', 'unacknowledge', 'note')


def _tpl():
    return (REPO / 'report-portal' / 'templates' / 'monitoring_center.html').read_text()


def test_the_row_click_opens_a_dialog_instead_of_posting():
    """Acknowledge is reversible, so a blocking confirm would be friction -
    the dialog earns its place by carrying the comment, not by asking "are you
    sure"."""
    tpl = _tpl()
    handler = tpl[tpl.index("document.addEventListener('click'"):
                   tpl.index('confirmBtn.addEventListener')]

    assert 'openAction(' in handler
    assert 'form.submit()' not in handler, 'the row click must not post directly'
    assert 'id="mcModal"' in tpl and 'id="mcConfirm"' in tpl


def test_the_confirm_step_carries_the_comment_into_the_form():
    tpl = _tpl()
    js = tpl[tpl.index('confirmBtn.addEventListener'):]
    js = js[:js.index("document.addEventListener('keydown'")]

    assert 'form.submit()' in js
    assert "i.name = 'message'" in js
    assert "note.value.trim()" in js


def test_the_dialog_knows_which_problem_it_is_for():
    """Both the server render and the 20s re-render must pass the problem name,
    or the dialog asks "remove the acknowledgement?" with no context."""
    tpl = _tpl()
    assert 'data-problem="{{ p.name }}"' in tpl
    assert "data-problem=\"' + attr(p.name)" in tpl


# ── acknowledgement history ────────────────────────────────────────────────
def test_notes_are_newest_first():
    got = pa.notes_from([
        {'clock': '100', 'message': 'older', 'username': 'a'},
        {'clock': '300', 'message': 'newest', 'username': 'c'},
        {'clock': '200', 'message': 'middle', 'username': 'b'},
    ])

    assert [n['message'] for n in got] == ['newest', 'middle', 'older']


def test_events_without_a_message_do_not_hide_a_real_comment():
    """Un-acknowledging records an event with no message. It is the newest
    entry, and taking it as "the note" would erase the reason a human wrote."""
    got = pa.notes_from([
        {'clock': '300', 'message': '', 'action': '16'},
        {'clock': '100', 'message': 'Dispenser replaced', 'username': 'a'},
    ])

    assert len(got) == 1
    assert got[0]['message'] == 'Dispenser replaced'


def test_a_note_survives_a_junk_clock():
    assert pa.notes_from([{'clock': 'not-a-number', 'message': 'x'}])[0]['clock'] == 0


def test_notes_tolerate_empty_or_missing_arrays():
    assert pa.notes_from(None) == []
    assert pa.notes_from([]) == []
    assert pa.notes_from(['junk']) == []


def test_the_page_asks_zabbix_for_the_acknowledges():
    """The comment lives in `acknowledges`, which problem.get does not return
    unless asked - that is why it was invisible after acknowledging."""
    src = _monitoring_src()

    assert "'selectAcknowledges': 'extend'" in src
    assert "params.pop('selectAcknowledges'" in src, (
        'a build that rejects the parameter must not lose the whole list')


def test_the_note_is_rendered_on_the_row_and_after_the_auto_refresh():
    tpl = _tpl()
    assert 'p.ack_note' in tpl

    js = tpl[tpl.index('pb.innerHTML'):tpl.index('var hb')]
    assert 'mc-note' in js, 'the 20s re-render would drop the note'
    assert 'attr(p.ack_tip)' in js


def test_operator_text_is_escaped_for_attributes():
    """The note is free text typed by a person. esc() handles & < > only, so a
    double quote would close the title="" early."""
    tpl = _tpl()

    assert 'function attr(' in tpl
    assert tpl.count("data-problem=\"' + attr(p.name)") == 3, (
        'the ack pill and the kebab carry the name; menu items read the kebab')
    assert "title=\"' + attr(p.ack_tip)" in tpl


# ── suppression ────────────────────────────────────────────────────────────
def test_suppress_acknowledges_and_carries_the_reason():
    """32 = suppress, 2 = acknowledge, 4 = message. Suppressing without the
    ack bit leaves the problem in the Unacknowledged filter the operator just
    acted on."""
    p = pa.ack_params('1700000001', 'suppress',
                      'Vendor visit booked, ref VN-1042', until=1700003600)

    assert p == {
        'eventids': ['1700000001'],
        'action': 38,
        'message': 'Vendor visit booked, ref VN-1042',
        'suppress_until': 1700003600,
    }


def test_suppress_requires_a_reason():
    """A suppression without a reason is an invisible blind spot: the problem
    disappears from the wall and nobody can say why it was silenced."""
    with pytest.raises(ValueError):
        pa.ack_params('123', 'suppress', '', until=1700003600)
    with pytest.raises(ValueError):
        pa.ack_params('123', 'suppress', '   ', until=1700003600)


def test_suppress_requires_an_end_time():
    with pytest.raises(ValueError):
        pa.ack_params('123', 'suppress', 'vendor visit')


def test_unsuppress_does_not_accept_an_end_time():
    with pytest.raises(ValueError):
        pa.ack_params('123', 'unsuppress', 'fixed', until=1700003600)


def test_unsuppress_can_carry_a_note():
    assert pa.ack_params('123', 'unsuppress') == {
        'eventids': ['123'], 'action': 64}
    assert pa.ack_params('123', 'unsuppress', 'part replaced')['action'] == 68


def test_durations_are_a_closed_set():
    """A raw minute count from the form is one typo away from a multi-year
    blind spot, so only preset keys are accepted."""
    now = 1_700_000_000
    assert pa.suppress_until('30', now) == now + 30 * 60
    assert pa.suppress_until('1440', now) == now + 24 * 60 * 60
    for bad in ('5', '99999', '', None, 'forever'):
        with pytest.raises(ValueError):
            pa.suppress_until(bad, now)


def test_only_an_admin_may_suppress_indefinitely():
    with pytest.raises(ValueError):
        pa.suppress_until('0', 1_700_000_000)
    assert pa.suppress_until('0', 1_700_000_000, admin=True) == 0


def test_bulk_ids_are_validated_deduped_and_capped():
    assert pa.ack_params(['2', '1', '2'], 'acknowledge')['eventids'] == ['2', '1']
    assert pa.ack_params('2,1', 'acknowledge')['eventids'] == ['2', '1']
    with pytest.raises(ValueError):
        pa.ack_params(['1', 'x'], 'acknowledge')
    with pytest.raises(ValueError):
        pa.ack_params(['%d' % i for i in range(pa.MAX_BULK + 1)], 'acknowledge')


def test_suppression_state_reads_the_flag_expiry_and_maintenance():
    assert pa.suppression_state({
        'suppressed': '1',
        'suppression_data': [{'suppress_until': '1700003600', 'maintenanceid': '0'}],
    }) == (True, 1700003600, '')
    assert pa.suppression_state({
        'suppressed': '1',
        'suppression_data': {'suppress_until': '1700003600', 'maintenanceid': '7'},
    }) == (True, 1700003600, '7')


def test_a_missing_suppression_block_does_not_crash():
    assert pa.suppression_state({'suppressed': '0'}) == (False, 0, '')
    assert pa.suppression_state({}) == (False, 0, '')
    assert pa.suppression_state(None) == (False, 0, '')


def test_the_page_asks_for_and_renders_suppression():
    src = _monitoring_src()
    assert "'suppressed'" in src
    assert "'selectSuppressionData': 'extend'" in src

    tpl = _tpl()
    assert '<th>Suppression</th>' in tpl
    assert 'class="sup-y"' in tpl and 'class="sup-m"' in tpl


def test_maintenance_suppression_cannot_be_manually_removed():
    """Zabbix will skip a manual unsuppress while a maintenance window is
    active, so offering the item would lie to the operator."""
    tpl = _tpl()
    assert "if(!maintenance) addKebab('Unsuppress', 'unsuppress');" in tpl


def test_the_duration_options_come_from_the_validated_table():
    """Hardcoding the <option> values in the template would let the UI offer a
    duration the route rejects - and only an operator would find out."""
    src = _monitoring_src()
    assert 'from problem_actions import SUPPRESS_LABELS' in src
    assert 'suppress_options=' in src

    tpl = _tpl()
    assert '{% for key, label in suppress_options %}' in tpl


def test_time_is_imported_at_module_level():
    """problem_action() calls time.time() for suppress_until. The module only
    imported time inside _cached_zabbix(), so every acknowledge still worked
    and suppression returned 500 with NameError - invisible to a static string
    test, caught here by looking at the real import table."""
    import ast

    tree = ast.parse(_monitoring_src())
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names)

    assert 'time' in imported, 'time.time() must resolve at module level'


# ── kebab menu + GLPI link ─────────────────────────────────────────────────
def test_the_secondary_action_lives_in_the_kebab_not_a_second_pill():
    """Two stacked pills doubled every row's height. The primary action stays
    visible; everything else moves behind the menu."""
    tpl = _tpl()
    start = tpl.index('{% for p in problems %}')
    row = tpl[start:tpl.index('{% endfor %}', start)]

    assert 'class="mc-kebab"' in row
    assert 'data-action="suppress"' not in row
    # Exactly one of the two ack branches renders; no second pill exists.
    assert row.count('data-action="acknowledge"') == 1
    assert row.count('data-action="unacknowledge"') == 1
    assert 'data-action="unsuppress"' not in row

    js = tpl[tpl.index('pb.innerHTML'):tpl.index('var hb')]
    assert 'class="mc-kebab"' in js, 'the 20s re-render must rebuild it'
    assert "addKebab('Suppress…', 'suppress')" in tpl


def _src(rel):
    return (REPO / 'report-portal' / rel).read_text()


def test_the_kebab_menu_is_floating_not_clipped_by_the_table():
    """The table wrapper scrolls. An absolutely positioned menu would be cut
    off; the shared .kebab-menu.floating pattern uses fixed positioning."""
    base = _src('templates/base.html')
    assert '.kebab-menu{' in base and 'position:absolute' in base
    assert '.kebab-menu.floating' in base

    tpl = _tpl()
    assert 'class="kebab-menu floating"' in tpl, (
        'the JS positions it in viewport coordinates; absolute would drift on scroll')
    assert 'id="mcKebab"' in tpl
    assert 'getBoundingClientRect()' in tpl


def test_the_kebab_closes_on_scroll_and_escape():
    tpl = _tpl()
    assert "document.addEventListener('scroll', closeKebab, true)" in tpl
    assert 'if(kebab && kebab.classList.contains(\'show\')){ closeKebab(); return; }' in tpl
    assert 'window.mcCloseKebab = closeKebab' in tpl


def test_a_glpi_ticket_is_linked_without_a_second_login():
    """The webhook tags the event glpi_id only after ticket creation. The link
    goes to the portal's incident page, not to GLPI."""
    src = _monitoring_src()
    assert "'selectTags': 'extend'" in src
    assert "'ticket_url': ('/incidents/%s' % ticket_id) if ticket_id else ''" in src

    tpl = _tpl()
    assert "a.textContent = 'Open incident #' + tid" in tpl
    assert "a.href = url" in tpl

    assert pa.glpi_ticket({'tags': [{'tag': 'glpi_id', 'value': '15'}]}) == '15'
    assert pa.glpi_ticket({'tags': [{'tag': 'other', 'value': '9'}]}) == ''
    assert pa.glpi_ticket({'tags': [{'tag': 'glpi_id', 'value': '9 OR 1=1'}]}) == ''
    assert pa.glpi_ticket({}) == ''
