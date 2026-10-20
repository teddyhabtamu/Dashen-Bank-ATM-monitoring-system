"""Tests for the GLPI ticket lifecycle actions: close, reopen, pending.

The portal could acknowledge, assign, solve and comment, and then stopped.
Solved tickets could not be closed, reopened, or parked as Pending, so the
lifecycle leaked into the GLPI UI. The rules live in ticket_lifecycle.decide()
so they are testable without Flask or a live GLPI; the route tests only check
wiring.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import ticket_lifecycle as tl  # noqa: E402

NEW, ASSIGNED, PLANNED = tl.STATUS_NEW, tl.STATUS_ASSIGNED, tl.STATUS_PLANNED
PENDING, SOLVED, CLOSED = tl.STATUS_PENDING, tl.STATUS_SOLVED, tl.STATUS_CLOSED


# ── close ──────────────────────────────────────────────────────────────────
def test_close_only_from_solved():
    assert tl.decide('close', SOLVED) == (True, CLOSED, 'closed')
    for st in (NEW, ASSIGNED, PLANNED, PENDING):
        assert tl.decide('close', st) == (False, None, 'not_solved'), (
            'closing open work would hide a fault that is still being worked')


def test_closing_a_closed_ticket_is_a_no_op():
    """Two operators clicked, or one double-clicked. The state is already the
    requested one; an error toast would make a working button look broken."""
    assert tl.decide('close', CLOSED) == (True, None, 'already_closed')


# ── reopen / resume ────────────────────────────────────────────────────────
def test_reopen_restores_assignment_when_a_technician_exists():
    assert tl.decide('reopen', SOLVED, assigned=True) == (True, ASSIGNED, 'reopened')
    assert tl.decide('reopen', CLOSED, assigned=True) == (True, ASSIGNED, 'reopened')


def test_reopen_without_a_technician_goes_back_to_new():
    assert tl.decide('reopen', SOLVED, assigned=False) == (True, NEW, 'reopened')
    assert tl.decide('reopen', CLOSED, assigned=False) == (True, NEW, 'reopened')


def test_a_pending_ticket_is_resumed_not_reopened():
    """Pending -> open is the exit from the waiting state. Without it, setting
    a ticket pending would be one-way."""
    assert tl.decide('reopen', PENDING, assigned=True) == (True, ASSIGNED, 'resumed')
    assert tl.decide('reopen', PENDING, assigned=False) == (True, NEW, 'resumed')


def test_reopening_an_open_ticket_writes_nothing():
    for st in (NEW, ASSIGNED, PLANNED):
        assert tl.decide('reopen', st) == (True, None, 'already_open')


# ── pending ────────────────────────────────────────────────────────────────
def test_pending_only_from_open_states():
    for st in (NEW, ASSIGNED, PLANNED):
        assert tl.decide('pending', st) == (True, PENDING, 'pending')
    for st in (SOLVED, CLOSED):
        assert tl.decide('pending', st) == (False, None, 'not_open'), (
            'a solved ticket must be reopened first, not silently un-solved')


def test_pending_is_idempotent():
    assert tl.decide('pending', PENDING) == (True, None, 'already_pending')


def test_unknown_action_never_writes():
    ok, new_status, code = tl.decide('delete', NEW)
    assert (ok, new_status, code) == (False, None, 'unknown_action')


def test_status_labels_are_glpi_names():
    assert tl.status_label(4) == 'Pending'
    assert tl.status_label(6) == 'Closed'
    assert tl.status_label(99) == '99'


# ── wiring ─────────────────────────────────────────────────────────────────
def _src(rel):
    return (REPO / 'report-portal' / rel).read_text()


def _route_block(src, action):
    marker = f"/<int:ticket_id>/{action}', methods=['POST'])"
    start = src.index(marker)
    end = src.find('@bp.route', start + 1)
    return src[start:end if end != -1 else len(src)]


def test_each_action_is_a_post_route_for_operators_only():
    src = _src('blueprints/incidents.py')
    for action in ('close', 'reopen', 'pending'):
        block = _route_block(src, action)
        assert f'def {action}(ticket_id):' in block
        assert '@role_required(ROLE_OPERATOR, ROLE_ADMIN)' in block, (
            f'{action} must not be a viewer action')


def test_new_actions_are_audited():
    src = _src('blueprints/incidents.py')
    for act in ('TICKET_CLOSE', 'TICKET_REOPEN', 'TICKET_PENDING'):
        assert f"log_action('{act}'" in src


def test_reopen_asks_glpi_for_the_assignee():
    src = _src('blueprints/incidents.py')
    block = _route_block(src, 'reopen')
    assert '_has_technician(token, ticket_id)' in block
    assert 'assigned=assigned' in block, (
        'the target status depends on whether a technician is attached')


def test_reason_is_required_for_reopen_and_pending():
    src = _src('blueprints/incidents.py')
    for action in ('reopen', 'pending'):
        block = _route_block(src, action)
        assert "reason = (request.get_json(silent=True) or {}).get('reason', '').strip()" in block
        assert 'if len(reason) < 5:' in block, (
            'without a reason the transition has no audit trail')


def test_buttons_follow_the_status():
    tpl = _src('templates/incidents_detail.html')
    assert 'id="pendingBtn"' in tpl
    assert 'ticket.status_n in (1, 2, 3)' in tpl
    assert "ticket.status_n == 4" in tpl and '>Resume</button>' in tpl
    assert 'ticket.status_n in (5, 6)' in tpl and '>Reopen</button>' in tpl
    assert "ticket.status_n == 5" in tpl and 'id="closeBtn"' in tpl, (
        'close must not appear on a ticket that is not solved')


def test_js_posts_to_the_new_routes_and_confirms_close():
    tpl = _src('templates/incidents_detail.html')
    js = tpl[tpl.index('function replyAction'):tpl.index('function showPickedFiles')]
    assert "close: '/incidents/{{ ticket.id }}/close'" in js
    assert "reopen: '/incidents/{{ ticket.id }}/reopen'" in js
    assert "pending: '/incidents/{{ ticket.id }}/pending'" in js
    assert 'text.length < 5' in js
    assert 'window.confirm(' in js
