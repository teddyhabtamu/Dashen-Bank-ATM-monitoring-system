"""Timestamp/event image matching, and the on-demand candidate finder.

The GRG evidence from 2026-10-22 is the fixture: the EJ interval is
15:34:44.992 -> 15:35:20.043, the filename card field says 8888########8888,
and the EJ says NOCARD.  These tests pin the rule that the card field is not
the link.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import atm_image_link as link  # noqa: E402
import find_atm_image as finder  # noqa: E402
from fetch_atm_image import ImageFetchError  # noqa: E402

SESSION_START = dt.datetime(2026, 10, 22, 15, 34, 44)
SESSION_END = dt.datetime(2026, 10, 22, 15, 35, 21)
PREFIX = 'CAM[1]_[DateTime-261022'
CARD = '8888########8888'

CARD_INSERTED = f'{PREFIX}153444]_[CarNum-{CARD}]CardInserted.jpg'
ENTER_INFO = f'{PREFIX}153519]_[CarNum-{CARD}]EnterInfo.jpg'
CARD_RETURNED = f'{PREFIX}153521]_[CarNum-{CARD}]CardReturned.jpg'
VIDEO = f'{PREFIX}153444]_[CarNum-{CARD}].mp4'
NCR_ZIP = '172.23.248.4_102226_163377_16_1.zip'


def rows(*names):
    return [
        {'name': name, 'length': 1000 + index,
         'last_write_time': '2026-10-22T15:34:44.0000000+03:00'}
        for index, name in enumerate(names)
    ]


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

def test_grg_filename_parses_timestamp_event_and_card_field():
    parsed = link.parse_grg_name(CARD_INSERTED)

    assert parsed['captured_at'] == SESSION_START
    assert parsed['event'] == 'CardInserted'
    assert parsed['kind'] == 'image'
    assert parsed['camera'] == 1
    assert parsed['card_field'] == CARD
    assert parsed['card_field_present'] is True


def test_grg_video_without_an_event_parses():
    parsed = link.parse_grg_name(VIDEO)

    assert parsed['event'] == ''
    assert parsed['kind'] == 'video'
    assert parsed['captured_at'] == SESSION_START


def test_grg_filename_card_field_is_masked_but_never_used_as_the_key():
    name = (f'{PREFIX}153444]_[CarNum-1234567890123456]CardInserted.jpg')
    parsed = link.parse_grg_name(name)

    assert parsed['card_field'] == '1234********3456'
    assert '1234567890123456' not in parsed['name_masked']
    result = link.rank_grg_candidates(
        rows(name), SESSION_START, SESSION_END)
    assert result['card_field_used_for_link'] is False


@pytest.mark.parametrize('name', [
    'notes.jpg',
    'CAM[1]_[DateTime-2610221534]_[CarNum-1]CardInserted.jpg',
    'CAM[1]_[DateTime-261322153444]_[CarNum-1]CardInserted.jpg',
    'CAM[1]_[DateTime-261022153444]_[CarNum-1]CardInserted.gif',
    'CAM[1]_[DateTime-261022153444]_[CarNum-1]Card Inserted.jpg',
    '',
])
def test_non_grg_names_are_rejected(name):
    assert link.parse_grg_name(name) is None


def test_ncr_zip_name_parses_the_observed_name():
    parsed = link.parse_ncr_zip_name(NCR_ZIP)

    assert parsed['day'] == dt.date(2026, 10, 22)
    assert parsed['atm_ip'] == '172.23.248.4'
    assert parsed['size'] == 163377
    assert parsed['count'] == 16
    assert parsed['camera'] == 1


@pytest.mark.parametrize('name', [
    '172.23.248.4_102226_163377_16.zip',
    '172.23.248.4_132226_163377_16_1.zip',
    'random.zip',
])
def test_non_ncr_zip_names_are_rejected(name):
    assert link.parse_ncr_zip_name(name) is None


def test_name_hmac_is_keyed_and_stable():
    key = b'k' * 32
    first = link.name_hmac(CARD_INSERTED, key)

    assert first == link.name_hmac(CARD_INSERTED, key)
    assert len(first) == 64
    assert link.name_hmac(CARD_INSERTED, b'x' * 32) != first
    assert CARD_INSERTED not in first


def test_name_hmac_requires_a_256_bit_key():
    with pytest.raises(link.LinkError) as exc:
        link.name_hmac(CARD_INSERTED, b'short')
    assert str(exc.value) == 'E_KEY'


# ---------------------------------------------------------------------------
# GRG ranking
# ---------------------------------------------------------------------------

def test_real_grg_session_has_a_strong_start_and_end_anchor():
    result = link.rank_grg_candidates(
        rows(CARD_INSERTED, ENTER_INFO, CARD_RETURNED, VIDEO),
        SESSION_START, SESSION_END,
    )

    assert result['confidence'] == 'high'
    assert result['ambiguous'] is False
    assert result['candidate_count'] == 4
    assert result['start_anchor_count'] == 1
    assert result['end_anchor_count'] == 1
    assert result['card_field_used_for_link'] is False
    assert 'not a transaction ID' in result['caveat']
    assert [item['role'] for item in result['candidates']] == [
        'start', 'end', 'in_window', 'in_window']
    assert [item['event'] for item in result['candidates']] == [
        'CardInserted', 'CardReturned', '', 'EnterInfo']
    assert [item['capture_basis'] for item in result['candidates']] == [
        'filename_time', 'filename_time',
        'filename_start+file_mtime_end', 'filename_time']


def test_video_clip_end_uses_the_file_write_time():
    video = {
        'name': VIDEO,
        'length': 1383102,
        'last_write_time': '2026-10-22T15:35:21.1063883+03:00',
    }
    result = link.rank_grg_candidates(
        [video, rows(CARD_INSERTED)[0]], SESSION_START, SESSION_END)

    clip = [item for item in result['candidates']
            if item['kind'] == 'video'][0]
    assert clip['captured_at'] == '2026-10-22 15:34:44'
    assert clip['file_mtime'] == '2026-10-22T15:35:21.1063883+03:00'
    assert clip['clip_end'] == '2026-10-22 15:35:21'
    assert clip['clip_duration_s'] == 37.0
    assert clip['capture_basis'] == 'filename_start+file_mtime_end'


def test_video_covering_the_session_is_included_even_if_it_starts_early():
    video = {
        'name': 'CAM[1]_[DateTime-261022153400]_[CarNum-1].mp4',
        'length': 100,
        'last_write_time': '2026-10-22T15:35:00.0000000+03:00',
    }
    result = link.rank_grg_candidates([video], SESSION_START, SESSION_END)

    assert result['candidate_count'] == 1
    assert result['candidates'][0]['in_interval'] is True


def test_video_ending_before_the_window_is_dropped():
    video = {
        'name': 'CAM[1]_[DateTime-261022153300]_[CarNum-1].mp4',
        'length': 100,
        'last_write_time': '2026-10-22T15:33:30.0000000+03:00',
    }
    result = link.rank_grg_candidates([video], SESSION_START, SESSION_END)

    assert result['candidates'] == []
    assert result['confidence'] == 'none'


def test_candidates_outside_the_window_are_dropped():
    old = 'CAM[1]_[DateTime-261022153000]_[CarNum-1]CardInserted.jpg'
    result = link.rank_grg_candidates(
        rows(old), SESSION_START, SESSION_END)

    assert result['candidates'] == []
    assert result['confidence'] == 'none'
    assert result['candidate_count'] == 0


def test_two_start_anchors_make_the_match_ambiguous():
    second = 'CAM[2]_[DateTime-261022153445]_[CarNum-2]CardInserted.jpg'
    result = link.rank_grg_candidates(
        rows(CARD_INSERTED, second, CARD_RETURNED),
        SESSION_START, SESSION_END,
    )

    assert result['start_anchor_count'] == 2
    assert result['ambiguous'] is True
    assert result['confidence'] == 'medium'


def test_cabinet_open_is_service_evidence_not_customer_evidence():
    cabinet = 'CAM[1]_[DateTime-261022153500]_[CarNum-1]CabinetOpen.jpg'
    result = link.rank_grg_candidates(
        rows(CARD_INSERTED, cabinet, CARD_RETURNED),
        SESSION_START, SESSION_END,
    )

    service = [item for item in result['candidates'] if item['service']]
    assert len(service) == 1
    assert service[0]['role'] == 'service'
    assert result['candidate_count'] == 2
    assert result['service_count'] == 1


def test_unparsed_files_are_counted_not_returned():
    result = link.rank_grg_candidates(
        rows(CARD_INSERTED, 'unrelated.jpg'),
        SESSION_START, SESSION_END,
    )

    assert result['unparsed_count'] == 1
    assert all(item['name'] != 'unrelated.jpg'
               for item in result['candidates'])


def test_reversed_session_is_refused():
    with pytest.raises(link.LinkError) as exc:
        link.rank_grg_candidates(
            rows(CARD_INSERTED), SESSION_END, SESSION_START)
    assert str(exc.value) == 'E_RANGE'


def test_a_non_numeric_size_is_refused():
    with pytest.raises(link.LinkError) as exc:
        link.rank_grg_candidates(
            [{'name': CARD_INSERTED, 'length': 'not-a-number'}],
            SESSION_START, SESSION_END)
    assert str(exc.value) == 'E_ROWS'


def test_timezone_aware_session_times_are_refused():
    aware = SESSION_START.replace(tzinfo=dt.timezone.utc)
    with pytest.raises(link.LinkError) as exc:
        link.rank_grg_candidates(rows(CARD_INSERTED), aware, SESSION_END)
    assert str(exc.value) == 'E_TZ'


# ---------------------------------------------------------------------------
# NCR day container
# ---------------------------------------------------------------------------

def test_ncr_zip_is_selected_for_the_session_day():
    other = '172.23.248.4_102126_153953_15_1.zip'
    result = link.select_ncr_zips(
        rows(NCR_ZIP, other, 'notes.jpg'), dt.date(2026, 10, 22))

    assert result['vendor'] == 'NCR'
    assert result['confidence'] == 'low'
    assert result['candidate_count'] == 1
    assert result['unparsed_count'] == 1
    assert result['candidates'][0]['name'] == NCR_ZIP
    assert result['candidates'][0]['image_count'] == 16
    assert result['candidates'][0]['zip_bytes'] == 163377
    assert 'not a transaction link' in result['caveat']


def test_ncr_day_without_a_zip_is_explicitly_empty():
    result = link.select_ncr_zips(rows(NCR_ZIP), dt.date(2026, 10, 20))

    assert result['confidence'] == 'none'
    assert result['candidates'] == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _args(*extra):
    return [
        '--host', '10.41.30.115', '--vendor', 'GRG',
        '--start', '2026-10-22 15:34:44',
        '--end', '2026-10-22 15:35:21',
        *extra,
    ]


def test_cli_returns_masked_candidates(monkeypatch, capsys):
    monkeypatch.setattr(
        finder, 'list_dir',
        lambda host, directory, **kwargs: rows(
            CARD_INSERTED, ENTER_INFO, CARD_RETURNED, VIDEO))

    assert finder.main(_args()) == 0
    out = json.loads(capsys.readouterr().out)

    assert out['available'] is True
    assert out['confidence'] == 'high'
    assert out['directory_masked'] == r'E:\pic\2026\10\22'
    assert 'directory' not in out
    assert all('name' not in item for item in out['candidates'])
    assert out['candidates'][0]['name_masked'] == CARD_INSERTED


def test_cli_show_sensitive_prints_the_raw_name(monkeypatch, capsys):
    monkeypatch.setattr(
        finder, 'list_dir',
        lambda host, directory, **kwargs: rows(CARD_INSERTED))

    assert finder.main(_args('--show-sensitive')) == 0
    out = json.loads(capsys.readouterr().out)

    assert out['directory'] == r'E:\pic\2026\10\22'
    assert out['candidates'][0]['name'] == CARD_INSERTED


def test_cli_missing_day_folder_is_not_available(monkeypatch, capsys):
    def missing(host, directory, **kwargs):
        raise ImageFetchError('ERR:E_MISSING')

    monkeypatch.setattr(finder, 'list_dir', missing)

    assert finder.main(_args()) == 0
    out = json.loads(capsys.readouterr().out)

    assert out['available'] is False
    assert out['reason'] == 'Not available on ATM'
    assert out['detail'] == 'ERR:E_MISSING'
    assert out['confidence'] == 'none'
    assert out['candidates'] == []


def test_cli_other_fetch_errors_exit_nonzero(monkeypatch, capsys):
    def refused(host, directory, **kwargs):
        raise ImageFetchError('ERR:E_ROOT')

    monkeypatch.setattr(finder, 'list_dir', refused)

    assert finder.main(_args()) == 1
    assert 'ERR:E_ROOT' in capsys.readouterr().err


def test_cli_ncr_returns_the_day_container(monkeypatch, capsys):
    monkeypatch.setattr(
        finder, 'list_dir',
        lambda host, directory, **kwargs: rows(NCR_ZIP))

    assert finder.main([
        '--host', '10.41.30.114', '--vendor', 'NCR',
        '--start', '2026-10-22 00:00:00',
        '--end', '2026-10-22 23:59:59',
    ]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out['vendor'] == 'NCR'
    assert out['confidence'] == 'low'
    assert out['candidate_count'] == 1
    assert out['candidates'][0]['image_count'] == 16


def test_cli_refuses_a_session_that_crosses_midnight():
    with pytest.raises(SystemExit):
        finder.main([
            '--host', '10.41.30.115', '--vendor', 'GRG',
            '--start', '2026-10-22 23:59:50',
            '--end', '2026-10-23 00:00:10',
        ])


def test_grg_day_folder_uses_two_digit_month_and_day():
    assert finder.grg_directory(dt.date(2026, 1, 2)) == \
        r'E:\pic\2026\01\02'


# ---------------------------------------------------------------------------
# NCR zip entries
# ---------------------------------------------------------------------------

NCR_ENTRY = ('1.102226.103353.Default-PicOnly.1.102226.103353.1.'
             '123456789012345.jpg')
NCR_ENTRY_CAM10 = ('10.102226.103526.Default-PicOnly.1.102226.103353.1.'
                   '123456789012345.jpg')
NCR_ENTRY_LATER = ('2.102226.104430.Default-PicOnly.1.102226.104155.1.'
                   '123456789012345.jpg')


def test_ncr_entry_name_parses_capture_and_session_times():
    parsed = link.parse_ncr_entry_name(NCR_ENTRY)

    assert parsed['camera'] == 1
    assert parsed['captured_at'] == dt.datetime(2026, 10, 22, 10, 33, 53)
    assert parsed['session_at'] == dt.datetime(2026, 10, 22, 10, 33, 53)
    assert parsed['image_type'] == 'Default-PicOnly'
    assert parsed['kind'] == 'image'
    assert parsed['card_field_present'] is True
    assert '123456789012345' not in parsed['name_masked']


@pytest.mark.parametrize('name', [
    'notes.jpg',
    '1.102226.103353.Default-PicOnly.1.102226.103353.1.jpg',
    '1.132226.103353.Default-PicOnly.1.132226.103353.1.123456789012345.jpg',
    '1.102226.103353.Default-PicOnly.1.102226.103353.1.123456789012345.gif',
    '',
])
def test_non_ncr_entry_names_are_rejected(name):
    assert link.parse_ncr_entry_name(name) is None


def test_ncr_entry_ranking_uses_the_session_anchor_not_the_card_field():
    rows = [
        {'index': 0, 'name': NCR_ENTRY},
        {'index': 1, 'name': NCR_ENTRY_CAM10},
        {'index': 2, 'name': NCR_ENTRY_LATER},
        {'index': 3, 'name': 'notes.jpg'},
    ]
    result = link.rank_ncr_entries(
        rows,
        dt.datetime(2026, 10, 22, 10, 33, 53),
        dt.datetime(2026, 10, 22, 10, 35, 30),
    )

    assert result['confidence'] == 'high'
    assert result['card_field_used_for_link'] is False
    assert result['candidate_count'] == 2
    assert result['unparsed_count'] == 1
    assert result['start_anchor_count'] == 2
    assert result['in_window_count'] == 2
    assert [item['index'] for item in result['candidates'][:2]] == [0, 1]
    assert [item['role'] for item in result['candidates'][:2]] == [
        'start', 'start']
    assert result['candidates'][2]['role'] == 'other'
    assert 'card field' in result['caveat']


def test_ncr_entry_ranking_matches_a_later_session_window():
    result = link.rank_ncr_entries(
        [{'index': 2, 'name': NCR_ENTRY_LATER}],
        dt.datetime(2026, 10, 22, 10, 41, 55),
        dt.datetime(2026, 10, 22, 10, 45, 0),
    )

    assert result['confidence'] == 'high'
    assert result['candidates'][0]['role'] == 'start'
    assert result['candidates'][0]['in_interval'] is True


def test_ncr_entry_ranking_refuses_aware_times():
    aware = dt.datetime(2026, 10, 22, 10, 33, 53, tzinfo=dt.timezone.utc)
    with pytest.raises(link.LinkError) as exc:
        link.rank_ncr_entries([], aware, aware)
    assert str(exc.value) == 'E_TZ'


def test_ncr_entry_ranking_refuses_a_reversed_window():
    with pytest.raises(link.LinkError) as exc:
        link.rank_ncr_entries(
            [],
            dt.datetime(2026, 10, 22, 10, 35, 0),
            dt.datetime(2026, 10, 22, 10, 34, 0),
        )
    assert str(exc.value) == 'E_ARGS'
