"""Fleet state summary helpers.

The ATM list overlays live Zabbix state on the persisted ``atm_current_state``
row.  The summary must use the same overlay for the ATMs it can see; otherwise
a green row can sit under a summary that still counts the old state.
"""


def merge_live_in_service(stored_count, atms):
    """Correct the persisted In-Service count with live rows.

    ``stored_count`` is the database count for all active ATMs.  For rows on
    the current page we know both the persisted state and the live state, so
    replace one with the other.  ATMs outside the page keep their persisted
    state, which is why the UI labels the count as live where visible.
    """
    count = int(stored_count or 0)
    for atm in atms or ():
        if not atm.get('live'):
            continue
        if (atm.get('status') or 'active').lower() != 'active':
            continue
        before = (atm.get('stored_state') or '').upper()
        after = (atm.get('state') or '').upper()
        if before == after:
            continue
        if before == 'IN_SERVICE':
            count -= 1
        if after == 'IN_SERVICE':
            count += 1
    return max(count, 0)
