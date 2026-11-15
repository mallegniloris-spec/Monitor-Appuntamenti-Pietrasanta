#!/usr/bin/env python3
"""Read-only diagnosis of Municipium public booking data for Benedetta Matteini."""
import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

RID = 2301
API = 'https://cloud.municipiumapp.it/api/gastone/prenotabile'
TZ = ZoneInfo('Europe/Rome')


def parse_date(value):
    if not isinstance(value, list) or len(value) < 3:
        return None
    try:
        return dt.date(*map(int, value[:3]))
    except (ValueError, TypeError):
        return None


def summarize():
    url = API + '?' + urllib.parse.urlencode({'rId': RID, 'data': int(time.time()*1000), 'months': 8})
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 (read-only diagnostic)'})
    with urllib.request.urlopen(request, timeout=30) as response:
        print('HTTP:', response.status)
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError('Unexpected API response')
    schedules, occupied = data.get('orari'), data.get('occupati')
    if not isinstance(schedules, list) or not isinstance(occupied, list):
        raise ValueError('Missing expected orari[] or occupati[]')
    print('Benedetta Matteini - resource', RID)
    print('Fasce pubblicate:', len(schedules), '| intervalli occupati:', len(occupied))
    print('Oggi:', dt.datetime.now(TZ).date(), '| risposta ricevuta:', dt.datetime.now(TZ).isoformat(timespec='seconds'))
    print('Nota: le fasce sono regole di servizio, non appuntamenti liberi confermati.')
    for i, s in enumerate(schedules, 1):
        print('\nFASCIA', i)
        for field in ('id', 'risorsaId', 'inizio', 'fine', 'week', 'oraInizio', 'oraFine', 'slot', 'noDays', 'festivi', 'indisponibile', 'nonDisponibile', 'inizioPrenotabile', 'finePrenotabile'):
            if field in s:
                print(' ', field, ':', json.dumps(s[field], ensure_ascii=False))
        start, end = parse_date(s.get('inizio')), parse_date(s.get('fine'))
        if start and end:
            if start == end:
                print('  INTERPRETAZIONE: giorno singolo')
            else:
                week = s.get('week')
                if isinstance(week, int) and 1 <= week <= 127:
                    print('  INTERPRETAZIONE: ricorrenza con bitmask', week)
                elif week == 0:
                    print('  INTERPRETAZIONE: week=0, regola speciale DA VERIFICARE')
                else:
                    print('  INTERPRETAZIONE: ricorrenza non riconosciuta')
    print('\nINTERVALLI OCCUPATI PER GIORNO (conteggi, senza dati personali)')
    counts = {}
    for obj in occupied:
        day = parse_date(obj.get('inizio')) if isinstance(obj, dict) else None
        if day is not None:
            key = day.isoformat()
            counts[key] = counts.get(key, 0) + 1
    for day, count in sorted(counts.items())[:70]:
        print(' ', day, ':', count)
    if not counts:
        print(' Nessuno riportato nella finestra API')
    print('\nFINE DIAGNOSTICA (nessuna prenotazione eseguita)')


if __name__ == '__main__':
    summarize()
