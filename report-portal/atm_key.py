"""Stable internal ATM keys derived from the bank's terminal ID.

The ATM team owns the terminal ID (ISO 8583 field 41); the monitoring platform
owns ``atm_id`` and uses it as the Zabbix technical host name. Operators should
never have to invent the internal key, so this module turns a terminal ID into
a deterministic, unique, human-readable key:

    PATML001 -> ATM-PATML001

It is stdlib-only and side-effect free, so the allocation rules are unit-tested
without a database. The caller supplies the set of keys already in use and the
terminal-ID -> atm_id index from the registry.
"""
from __future__ import annotations

import hashlib
import re

MAX_ATM_ID = 20
ATM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def terminal_key(terminal_id):
    """Case-insensitive comparison key for a terminal ID."""
    return (terminal_id or '').strip().upper()


def _component(value):
    """A terminal ID fragment that is safe inside an atm_id."""
    text = re.sub(r'[^A-Za-z0-9_-]+', '-', (value or '').strip())
    text = re.sub(r'-{2,}', '-', text).strip('-_')
    return text


def derived_atm_id(terminal_id):
    """The preferred key for a terminal ID, before collision handling.

    Short IDs keep the readable ``ATM-<TID>`` form. An ID long enough to
    overflow the 20-character column keeps a prefix plus a short digest, so
    two long IDs cannot collapse to the same truncated key.
    """
    tid = _component(terminal_id)
    if not tid:
        return None
    base = f'ATM-{tid}'
    if len(base) <= MAX_ATM_ID:
        return base
    digest = hashlib.sha1((terminal_id or '').encode('utf-8')).hexdigest()[:6]
    head = tid[:MAX_ATM_ID - len('ATM-') - 1 - len(digest)]
    return f'ATM-{head}-{digest}'


def _with_suffix(base, number):
    suffix = f'-{number}'
    head = base[:MAX_ATM_ID - len(suffix)].rstrip('-_')
    return f'{head}{suffix}'


def choose_atm_id(terminal_id, used_ids, existing_by_terminal=None):
    """Pick the internal key for a terminal ID.

    Order of preference:
      1. the key already registered for this terminal ID (case-insensitive),
         so a TID-only re-import updates the same row instead of creating a
         second one;
      2. ``ATM-<TID>``;
      3. ``ATM-<TID>-2``, ``-3``, ... when another terminal already owns the
         readable key.

    Returns None when no terminal ID was supplied; validation reports that.
    """
    tid = (terminal_id or '').strip()
    if not tid:
        return None
    existing_by_terminal = existing_by_terminal or {}
    known = existing_by_terminal.get(terminal_key(tid))
    if known:
        return known

    base = derived_atm_id(tid)
    if not base:
        return None
    if base not in used_ids:
        return base
    for number in range(2, 10000):
        candidate = _with_suffix(base, number)
        if candidate not in used_ids:
            return candidate
    raise ValueError(f'could not allocate an ATM ID for terminal {tid!r}')
