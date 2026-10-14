"""Shared .env loader for the Zabbix/API helper scripts.

Why this exists: every script read os.environ only, so each fresh shell
demanded the three exports despite .env sitting one directory up — and each
forgotten run burned a failed login into the shared service account
(temporary blocks included). Now: explicit environment wins, .env fills the
rest, hardcoded defaults last. No third-party dependency (plain parser).

Rules:
- KEY=VALUE lines only; leading `export ` tolerated; single/double quotes
  stripped; blanks and `#` comments skipped.
- Values containing unexpanded `${...}` are SKIPPED (compose-style
  indirection like ZABBIX_PASS=${ZBX_PASS} must not leak literally).
- Never overrides variables already present in os.environ.
- .env is located at the repository root (parent of this file's directory),
  independent of the caller's working directory.
"""
import os

_LOADED = False


def repo_dotenv_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), '.env')


def parse_dotenv(path):
    """Parse a dotenv file into a dict (pure function — unit-tested)."""
    out = {}
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        if '=' not in line:
            continue
        key, _, val = line.partition('=')
        key = key.strip()
        val = val.strip()
        if (len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'")):
            val = val[1:-1]
        if not key or '${' in val:
            continue
        out[key] = val
    return out


def ensure_loaded():
    """Fill missing env vars from the repo .env (idempotent)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    for key, val in parse_dotenv(repo_dotenv_path()).items():
        os.environ.setdefault(key, val)
