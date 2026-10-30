"""Anomaly detector Zabbix hand-off files.

The detector writes counts into a shared temp directory.  These tests pin the
two properties that matter: the JSON feed stays private, and the non-sensitive
counters stay readable by the Zabbix agent's unprivileged user.  They also pin
the symlink behaviour of the atomic replace.
"""
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anomaly_detector as ad  # noqa: E402


def test_publish_text_defaults_to_private_feed(tmp_path):
    target = tmp_path / 'anomaly_feed.json'

    ad._publish_text(target, '{"ok": true}')

    assert target.read_text() == '{"ok": true}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_publish_text_counter_is_readable_by_zabbix(tmp_path):
    target = tmp_path / 'zabbix_anomaly_count'

    ad._publish_text(target, '7', mode=0o644)

    assert target.read_text() == '7'
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_publish_text_replaces_symlink_without_following_it(tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep me')
    target = tmp_path / 'zabbix_anomaly_count'
    target.symlink_to(outside)

    ad._publish_text(target, '3', mode=0o644)

    assert target.read_text() == '3'
    assert not target.is_symlink()
    assert outside.read_text() == 'keep me'
