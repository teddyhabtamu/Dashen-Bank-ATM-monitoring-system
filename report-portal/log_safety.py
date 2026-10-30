"""Keep user-controlled values on one line before they reach a log.

A newline in a username, ATM id or query parameter lets an attacker forge a
second log record.  These helpers flatten control characters and cap the
length; they do not alter the value used by the application.

``replace`` is deliberate: it is the sanitizer shape static analysers recognise
for log-injection dataflow, and it keeps the helper dependency-free.
"""
import re

_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]+')


def safe_log(value, limit=200):
    """Return a single-line, length-capped representation of ``value``."""
    text = str(value)
    # Normalise every C0 control byte (including CR/LF) and the Unicode line
    # separators to one space.  The explicit replace calls keep the CR/LF
    # sanitisation visible to the taint engine; the regex covers the rest.
    text = text.replace('\r', ' ').replace('\n', ' ')
    text = text.replace('\u2028', ' ').replace('\u2029', ' ')
    text = _CONTROL_CHARS.sub(' ', text)
    return text[:limit]
