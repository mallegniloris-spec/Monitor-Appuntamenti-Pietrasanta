import datetime as dt
from zoneinfo import ZoneInfo
from monitor import split_intervals

def test_har_sample():
    d={'occupati':[], 'orari':[{'indisponibile':False,'inizio':[2026,11,20], 'fine':[2026,11,20], 'oraInizio':[9,0], 'oraFine':[11,0], 'slot':1}]}
    for hour,minute in [(9,0),(9,20),(9,40),(10,0),(10,20),(10,40)]:
        h2,m2=divmod(hour*60+minute+20,60)
        d['occupati'].append({'inizio':[2026,11,20,hour,minute],'fine':[2026,11,20,h2,m2]})
    now=dt.datetime(2026,11,14,tzinfo=ZoneInfo('Europe/Rome'))
    assert split_intervals(d,20,now,45)==[]
    d['occupati'].pop()
    assert len(split_intervals(d,20,now,45))==1
    assert split_intervals({'occupati':[], 'orari':[dict(d['orari'][0], fine=[2026,12,20])]},20,now,45)==[]
