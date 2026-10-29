"""Static guardrails for the NCR APTRA XFS probe.

The NCR test core is 32-bit Windows with PowerShell 2.0, so ConvertTo-Json
does not exist, and the XFS manager is loaded from the NCR common-files PATH.
The recon also proved that the first CDM service with a readable 301 is not
the dispenser: BillDispenser1 answers 301 but 303 fails, while
CurrencyDispenser1 answers both. These tests pin those hard-won facts.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = (REPO / 'scripts' / 'ncr-xfs-probe.ps1').read_text()


def test_probe_is_powershell_2_compatible():
    assert '#Requires -Version 2.0' in PROBE
    assert 'ConvertTo-Json -InputObject' not in PROBE, (
        'ConvertTo-Json is PowerShell 3.0+; the NCR core has 2.0')
    assert 'ConvertTo-JsonValue' in PROBE
    assert 'ConvertTo-JsonString' in PROBE


def test_probe_is_read_only():
    assert '::WFSExecute' not in PROBE and 'WFSExecute(' not in PROBE, (
        'the collector must never dispense, reset or reconfigure')
    for call in ('WFSStartUp', 'WFSOpen', 'WFSGetInfo', 'WFSFreeResult',
                 'WFSClose', 'WFSCleanUp'):
        assert call in PROBE


def test_probe_uses_the_verified_categories():
    assert '301' in PROBE and '303' in PROBE
    assert '201' in PROBE and '101' in PROBE
    assert '401' in PROBE and '801' in PROBE


def test_probe_prefers_the_real_currency_dispenser():
    assert 'CurrencyDispenser' in PROBE
    assert 'MediaDispenser' in PROBE
    assert 'BillDispenser1' not in PROBE, (
        'the probe must not hardcode a service it has not verified')


def test_probe_reuses_the_template_cassette_ids():
    for unit_id in ('0050A', '0100A', '0100B', '0200A', 'Rej01'):
        assert unit_id in PROBE


def test_probe_relaunches_32_bit_once():
    assert 'NCR_XFS_PROBE_CHILD' in PROBE
    assert 'SysWOW64' in PROBE
    assert 'Sysnative' in PROBE


def test_siu_doors_stay_unknown_until_mapped():
    assert 'door/tamper sensors not mapped on NCR yet' in PROBE
    assert 'cabinet_door = 255' in PROBE
    assert 'safe_door = 255' in PROBE
    assert 'tamper_sensor = 255' in PROBE
