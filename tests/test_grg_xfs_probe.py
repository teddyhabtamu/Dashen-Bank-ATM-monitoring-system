"""Static guardrails for the GRG XFS probe.

The first peripheral attempt used the wrong status categories (IDC 401 instead
of 201, PIN 601 instead of 401) and opened every IDC/PTR service it could find.
Those are exactly the kind of constants that are easy to "clean up" later, so
pin them here.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = (REPO / 'scripts' / 'grg-xfs-probe.ps1').read_text()


def test_known_good_services_are_used():
    assert "IDC = 'CardReader'" in PROBE
    assert "PTR = 'CheckPrinter'" in PROBE
    assert "PIN = 'Encryptor'" in PROBE
    assert "SIU = 'SIU'" in PROBE


def test_status_categories_match_the_cen_xfs_service_classes():
    assert 'IDC = 201' in PROBE
    assert 'PTR = 101' in PROBE
    assert 'PIN = 401' in PROBE
    assert 'SIU = 801' in PROBE


def test_peripherals_use_an_app_handle_and_isolated_children():
    assert 'WFSCreateAppHandle' in PROBE
    assert "Invoke-ChildProbe 'IDC'" in PROBE
    assert "Invoke-ChildProbe 'SIU'" in PROBE
    assert 'WFS_ERR_INVALID_SERVPROV' not in PROBE
