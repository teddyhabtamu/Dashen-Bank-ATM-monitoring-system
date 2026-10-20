"""
ticket_lifecycle.py — GLPI status transitions for the portal's ticket actions.

GLPI's status values are 1 New, 2 Processing (assigned), 3 Processing
(planned), 4 Pending, 5 Solved, 6 Closed. The portal can solve a ticket, but
used to stop there: Solved could not be closed or reopened, and an operator
could not park a ticket as Pending while waiting for a vendor or cash.

Kept pure and separate from the blueprint so every guard (which status may
close, what reopen does to a pending ticket, what happens when the action is
already applied) is testable without Flask or a live GLPI.
"""

STATUS_NEW = 1
STATUS_ASSIGNED = 2
STATUS_PLANNED = 3
STATUS_PENDING = 4
STATUS_SOLVED = 5
STATUS_CLOSED = 6

STATUS_NAMES = {
    STATUS_NEW: 'New',
    STATUS_ASSIGNED: 'Processing (assigned)',
    STATUS_PLANNED: 'Processing (planned)',
    STATUS_PENDING: 'Pending',
    STATUS_SOLVED: 'Solved',
    STATUS_CLOSED: 'Closed',
}


def decide(action, status, assigned=False):
    """Return (ok, new_status, code) for a portal ticket action.

    `new_status is None` means the request is already satisfied (already
    closed, already open, already pending) and no GLPI write is needed. The
    route maps the code to a message; keeping codes stable makes the rules
    testable and stops the UI from inventing its own transitions.
    """
    if action == 'close':
        if status == STATUS_CLOSED:
            return True, None, 'already_closed'
        if status != STATUS_SOLVED:
            return False, None, 'not_solved'
        return True, STATUS_CLOSED, 'closed'

    if action == 'reopen':
        if status == STATUS_CLOSED or status == STATUS_SOLVED:
            return True, STATUS_ASSIGNED if assigned else STATUS_NEW, 'reopened'
        if status == STATUS_PENDING:
            return True, STATUS_ASSIGNED if assigned else STATUS_NEW, 'resumed'
        if status in (STATUS_NEW, STATUS_ASSIGNED, STATUS_PLANNED):
            return True, None, 'already_open'
        return False, None, 'unknown_status'

    if action == 'pending':
        if status == STATUS_PENDING:
            return True, None, 'already_pending'
        if status in (STATUS_NEW, STATUS_ASSIGNED, STATUS_PLANNED):
            return True, STATUS_PENDING, 'pending'
        return False, None, 'not_open'

    return False, None, 'unknown_action'


def status_label(status):
    return STATUS_NAMES.get(status, str(status))
