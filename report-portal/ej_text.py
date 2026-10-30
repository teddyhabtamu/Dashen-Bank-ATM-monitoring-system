"""Human-readable rendering of NDC journal text (display only).

Raw wire bytes stay untouched in storage (dispute evidence); this module
decides what a human sees. Pure stdlib, fully unit-tested — unlike the
Flask-bound parsers, which is why it lives here and not in blueprints/.

Deliberate boundary: supervisor op-codes (*1*P*21,M-) and leftover control
fragments are NOT decoded — they need an NDC op-code dictionary (Phase 2),
and guessing their meaning would be worse than showing them.
"""
import re

_ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_MARKER = re.compile(r'\[020t\*[^*]*\*[^*]*\*[^*]*\*')
_WRAPPER = re.compile(r'\[(?:05p|00p)')
_WS = re.compile(r'[ \t\xa0]+')


def humanize_ndc_text(text):
    """Strip wire framing, keep every human word. Never raises."""
    if not text:
        return ''
    try:
        t = _ANSI.sub('', str(text))
        t = _MARKER.sub('', t)
        t = _WRAPPER.sub('', t)
        t = _WS.sub(' ', t).strip(' ;*')
        return t or str(text)
    except Exception:
        return str(text)
