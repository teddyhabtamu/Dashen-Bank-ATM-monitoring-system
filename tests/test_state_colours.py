"""The portal's operator-facing states must match the NetXMS eight.

Operations staff move between NetXMS and this portal, so a state name or
colour that differs between the two is a training hazard, not a cosmetic
choice. These tests pin the eight labels, their colours, and the order of the
legend so a future palette tweak cannot silently drop one.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

import helpers  # noqa: E402


# NetXMS order and colours agreed for the migration.
EXPECTED = [
    ('IN_SERVICE', 'In Service', '#059669'),
    ('UNREACHABLE', 'Agent Disconnected', '#BE185D'),
    ('UNKNOWN', 'Unknown', '#F9A8D4'),
    ('IN_SUPERVISOR', 'In Supervisor', '#CA8A04'),
    ('HARDWARE_FAULT', 'Hardware Faults', '#1D4ED8'),
    ('OUT_OF_CASH', 'Out Of Cash', '#0F4C5C'),
    ('OFFLINE', 'Offline', '#38BDF8'),
    ('OUT_OF_SERVICE', 'Out of Service', '#DC2626'),
]


def test_all_eight_states_have_the_netxms_label_and_colour():
    for state, label, colour in EXPECTED:
        assert helpers.state_label(state) == label
        assert helpers.state_colour(state) == colour


def test_colours_are_distinct():
    colours = [helpers.state_colour(state) for state, _l, _c in EXPECTED]
    assert len(colours) == len(set(colours)) == 8, (
        'two states sharing a colour is how the old four-tone map lost meaning')


def test_legend_is_the_eight_states_in_netxms_order():
    legend = helpers.state_legend()
    assert [(row['state'], row['label']) for row in legend] == [
        (state, label) for state, label, _c in EXPECTED]


def test_light_states_get_dark_ink():
    for light in ('UNKNOWN', 'IN_SUPERVISOR', 'OFFLINE'):
        assert helpers.state_ink(light) != '#FFFFFF', (
            f'{light} is a light colour; white text would be unreadable')
    for dark in ('IN_SERVICE', 'UNREACHABLE', 'HARDWARE_FAULT',
                 'OUT_OF_CASH', 'OUT_OF_SERVICE'):
        assert helpers.state_ink(dark) == '#FFFFFF'


def test_unknown_values_fall_back_without_crashing():
    assert helpers.state_label('SOME_NEW_STATE') == 'Some New State'
    assert helpers.state_colour('SOME_NEW_STATE') == '#64748B'
    assert helpers.state_label(None) == 'Unknown'
    assert helpers.state_colour(None) == '#64748B'
