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
