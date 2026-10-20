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
    assert 'colspan="6"' in js, 'the empty row spans the new column count'


def test_every_row_has_the_same_number_of_cells():
    """Header, server-rendered row, JS row and empty row must agree, or the
    Action column lands under the wrong heading."""
    tpl = (REPO / 'report-portal' / 'templates' / 'monitoring_center.html').read_text()

    header = tpl[tpl.index('<thead><tr>'):tpl.index('</tr></thead>')]
    assert header.count('<th>') == 6
    assert '<th>Ack</th>' in header and '<th>Action</th>' in header


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

    assert 'open(b)' in handler
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
    assert "data-problem=\"' + esc(p.name)" in tpl
