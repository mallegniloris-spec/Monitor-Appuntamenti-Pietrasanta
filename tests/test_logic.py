"""Offline regression tests for the V3.4 recurring schedule logic."""
import datetime as dt
from zoneinfo import ZoneInfo

from monitor import split_intervals

TZ = ZoneInfo('Europe/Rome')


def test_har_sample():
    # Single-day appointments: all six 20-minute intervals occupied.
    d = {'occupati': [], 'orari': [{
        'indisponibile': False, 'inizio': [2026, 11, 20],
        'fine': [2026, 11, 20], 'oraInizio': [9, 0],
        'oraFine': [11, 0], 'slot': 1,
    }]}
    for minute_of_day in range(9 * 60, 11 * 60, 20):
        h, m = divmod(minute_of_day, 60)
        h2, m2 = divmod(minute_of_day + 20, 60)
        d['occupati'].append({
            'inizio': [2026, 11, 20, h, m],
            'fine': [2026, 11, 20, h2, m2],
        })
    now = dt.datetime(2026, 11, 14, tzinfo=TZ)
    assert split_intervals(d, 20, now, 45) == []
    d['occupati'].pop()
    assert split_intervals(d, 20, now, 45) == ['2026-11-20T10:40:00+01:00']

    # Weekly recurrence: Friday=32 in the verified Municipium bitmask.
    # Restrict to 20 and 27 Nov: 20 Nov is full except last slot;
    # 27 Nov has all six slots unoccupied.
    recurring = dict(d['orari'][0], fine=[2026, 11, 27], week=32)
    result = split_intervals({'occupati': d['occupati'], 'orari': [recurring]},
                             20, now, 45)
    assert len(result) == 7, result
    assert result[0] == '2026-11-20T10:40:00+01:00'
    assert result[-1] == '2026-11-27T10:40:00+01:00'

    # Avoid silent false negatives if the recurrence mask is absent.
    ambiguous = dict(recurring)
    ambiguous.pop('week')
    try:
        split_intervals({'occupati': [], 'orari': [ambiguous]}, 20, now, 45)
    except ValueError as exc:
        assert 'weekly schedule bitmask' in str(exc)
    else:
        raise AssertionError('Ambiguous recurrence must not be interpreted as no availability')

    # Municipium Eugenia Bonatti: special week=0 interval alongside a
    # recurring Wednesday=8 interval. week=0 must not become every day,
    # and three free Wednesday slots must still be found.
    wed = {
        'risorsaId': 1721, 'indisponibile': False,
        'inizio': [2026, 11, 18], 'fine': [2026, 12, 2],
        'oraInizio': [11, 0], 'oraFine': [12, 20],
        'slot': 1, 'week': 8, 'noDays': [],
    }
    special = dict(wed, week=0)
    occupied = [{'inizio': [2026, 11, 25, 12, 0],
                 'fine': [2026, 11, 25, 12, 20]}]
    actual = split_intervals({'occupati': occupied, 'orari': [special, wed]},
                             20, dt.datetime(2026, 11, 24, tzinfo=TZ), 2)
    assert actual == [
        '2026-11-25T11:00:00+01:00',
        '2026-11-25T11:20:00+01:00',
        '2026-11-25T11:40:00+01:00',
    ], actual
    # An isolated zero-mask recurring schedule cannot safely imply a day.
    assert split_intervals({'occupati': [], 'orari': [special]},
                           20, dt.datetime(2026, 11, 24, tzinfo=TZ), 2) == []


def test_benedetta_excluded_slots():
    # A published weekly schedule with specific excluded time slots.
    schedule = {
        'risorsaId': 2301, 'indisponibile': False,
        'inizio': [2024, 11, 18], 'fine': [2026, 12, 31],
        'week': 8, 'oraInizio': [11, 0], 'oraFine': [13, 0],
        'slot': 1, 'noDays': [],
        'nonDisponibile': ['202611251100', '202611251140'],
    }
    now = dt.datetime(2026, 11, 24, tzinfo=TZ)
    actual = split_intervals({'occupati': [], 'orari': [schedule]}, 20, now, 2)
    assert actual == [
        '2026-11-25T11:20:00+01:00',
        '2026-11-25T12:00:00+01:00',
        '2026-11-25T12:20:00+01:00',
        '2026-11-25T12:40:00+01:00',
    ], actual
    schedule['nonDisponibile'] = [f'20261125{h:02d}{m:02d}'
        for h, m in [(11, 0), (11, 20), (11, 40), (12, 0), (12, 20), (12, 40)]]
    assert split_intervals({'occupati': [], 'orari': [schedule]}, 20, now, 2) == []
    schedule['nonDisponibile'].append('invalid')
    try:
        split_intervals({'occupati': [], 'orari': [schedule]}, 20, now, 2)
    except ValueError as e:
        assert 'nonDisponibile' in str(e)
    else:
        raise AssertionError('Malformed exclusion must fail closed')
