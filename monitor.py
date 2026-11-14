#!/usr/bin/env python3
"""Monitor read-only of public Municipium booking intervals. No reservations."""
import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = 'https://cloud.municipiumapp.it/api/gastone/'
BOOKING = 'https://www.comune.pietrasanta.lu.it/it/book'
TZ = ZoneInfo('Europe/Rome')
ROOT = pathlib.Path(__file__).parent
STATE = ROOT / 'state.json'
HEADERS = {'Accept': 'application/json', 'User-Agent': 'Pietrasanta-appointments-monitor/3.0 (read-only)'}


def get_json(path, params):
    uri = BASE + path + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(uri, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status} from {path}')
        return json.loads(response.read().decode('utf-8'))


def date_part(seq):
    if not isinstance(seq, list) or len(seq) < 3:
        raise ValueError('Invalid date')
    return dt.date(*map(int, seq[:3]))


def datetime_part(seq):
    if not isinstance(seq, list) or len(seq) < 5:
        raise ValueError('Invalid date/time')
    return dt.datetime(*map(int, seq[:5]), tzinfo=TZ)


def split_intervals(data, min_duration, not_before, max_days):
    """Conservative: only concrete, single-date schedules; never guess recurrence."""
    if not isinstance(data, dict) or not isinstance(data.get('occupati'), list) or not isinstance(data.get('orari'), list):
        raise ValueError('Expected JSON occupati[] and orari[]')
    busy = []
    for item in data['occupati']:
        try:
            busy.append((datetime_part(item['inizio']), datetime_part(item['fine'])))
        except (ValueError, KeyError, TypeError):
            raise ValueError('Unrecognized occupied interval')
    results = []
    cutoff = not_before + dt.timedelta(days=max_days)
    for schedule in data['orari']:
        if schedule.get('indisponibile') is not False or schedule.get('nonDisponibile'):
            continue
        try:
            start_date, end_date = date_part(schedule['inizio']), date_part(schedule['fine'])
            period_start, period_end = schedule['oraInizio'], schedule['oraFine']
            duration = int(min_duration)
            capacity = int(schedule.get('slot', 0))
        except (KeyError, ValueError, TypeError):
            continue
        # A date range may represent recurrence; never infer weekly/day-of-week rules.
        if start_date != end_date or capacity < 1 or duration < 1:
            continue
        if not (not_before.date() <= start_date <= cutoff.date()):
            continue
        try:
            beginning = dt.datetime.combine(start_date, dt.time(int(period_start[0]), int(period_start[1])), TZ)
            ending = dt.datetime.combine(end_date, dt.time(int(period_end[0]), int(period_end[1])), TZ)
        except (ValueError, IndexError, TypeError):
            continue
        cur = beginning
        while cur + dt.timedelta(minutes=duration) <= ending:
            nxt = cur + dt.timedelta(minutes=duration)
            if cur > not_before and nxt <= cutoff:
                busy_count = sum(a < nxt and b > cur for a, b in busy)
                if busy_count < capacity:
                    results.append(cur.isoformat())
            cur = nxt
    return sorted(set(results))


def discover_planning(config):
    """Find named staff via the same read-only actions used by the public booking UI.

    Never guess resource IDs. Unmatched names are reported but not monitored.
    """
    office_id = int(config.get('planning_office_id', 8722))
    offices = get_json('actions', {'id': office_id, 'action': 'preno_luogo_unita'})
    if not isinstance(offices, list):
        raise ValueError('Unexpected planning office locations response')
    found = []
    for office in offices:
        office_location_id = office.get('unita_luogo')
        if office_location_id is None:
            continue
        services = get_json('actions', {
            'id': office_id, 'sportello': office_location_id,
            'action': 'preno_risorse_servizio_by_luogo'})
        if not isinstance(services, list):
            raise ValueError('Unexpected planning resources response')
        found.extend(services)
    matches = []
    for target in config.get('planning_staff', []):
        if not target.get('enabled', True):
            continue
        full_name = str(target['name']).casefold().strip()
        # Require full name as an uninterrupted phrase in the published service label.
        candidates = [x for x in found if full_name in str(x.get('sportello_name', '')).casefold()]
        ids = {(str(x.get('risorsa_id')), str(x.get('servizio'))) for x in candidates
               if x.get('risorsa_id') is not None and x.get('servizio') is not None}
        if len(ids) != 1:
            logging.warning('Pianificazione - %s: %d matching resource/service pairs; not monitored',
                            target['name'], len(ids))
            continue
        rid, sid = next(iter(ids))
        matches.append({
            'name': target['name'] + ' - Pianificazione urbanistica',
            'risorsa_id': int(rid),
            'servizio_id': int(sid),
            'minutes': int(candidates[0].get('minuti_prenotabili') or 20),
            'enabled': True})
        logging.info('Pianificazione - %s: identified resource %s, service %s',
                     target['name'], rid, sid)
    return matches


def read_state():
    try:
        d = json.loads(STATE.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(STATE)


def telegram_send(token, chat_id, message):
    endpoint = 'https://api.telegram.org/bot' + urllib.parse.quote(token, safe='') + '/sendMessage'
    form = urllib.parse.urlencode({'chat_id': chat_id, 'text': message, 'disable_web_page_preview': 'true'}).encode()
    req = urllib.request.Request(endpoint, data=form, headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
        if not d.get('ok'):
            raise RuntimeError('Telegram did not accept the message')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true', help='Offline HAR fixture check only')
    parser.add_argument('--dry-run', action='store_true', help='Read live data, no notifications/state changes')
    args = parser.parse_args()
    if args.test:
        from tests.test_logic import test_har_sample
        test_har_sample()
        print('Offline test OK: all occupied time slots correctly excluded')
        return 0
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not args.dry_run and (not token or not chat_id):
        print('Missing Telegram credentials in GitHub secrets', file=sys.stderr)
        return 2
    old = read_state()
    new = dict(old)
    now = dt.datetime.now(TZ)
    errors = 0
    configured = {int(x['risorsa_id']): x for x in config['services'] if x.get('enabled', True)}
    if config.get('planning_staff'):
        try:
            planning = discover_planning(config)
            for service in planning:
                rid = int(service['risorsa_id'])
                if rid in configured:
                    logging.warning('Pianificazione: resource %d already configured; keeping existing service', rid)
                else:
                    configured[rid] = service
        except Exception as err:
            logging.warning('Pianificazione: discovery unavailable (%s). Other staff will still be checked.', err)
    for i, (rid, local) in enumerate(configured.items()):
        name = local['name']
        try:
            raw = get_json('prenotabile', {'rId': rid, 'data': int(time.time() * 1000), 'months': config.get('months', 2)})
            slots = split_intervals(raw, int(local.get('minutes', 20)), now, int(config.get('days_ahead', 45)))
            prior = old.get(str(rid))
            if not isinstance(prior, list):
                logging.info('%s: baseline %d possible intervals (no alerts on initial scan)', name, len(slots))
            else:
                fresh = sorted(set(slots) - set(prior))
                logging.info('%s: %d possible intervals, %d newly seen', name, len(slots), len(fresh))
                for s in fresh:
                    when = dt.datetime.fromisoformat(s)
                    text = (f'Appuntamento POTENZIALMENTE disponibile - {name}\n'
                            f'{when:%d/%m/%Y alle %H:%M}\n'
                            f'Conferma subito sul calendario ufficiale: {BOOKING}\n'
                            'La disponibilita puo cambiare prima della prenotazione.')
                    if not args.dry_run:
                        telegram_send(token, chat_id, text)
                    else:
                        print('DRY RUN:', text)
            new[str(rid)] = slots
        except Exception as err:
            errors += 1
            logging.error('%s (%s): unable to verify: %s', name, rid, err)
            # Keep prior on API failures to avoid false baseline.
        if i < len(configured)-1:
            time.sleep(2)
    if not args.dry_run and errors == 0:
        save_state(new)
    elif not args.dry_run:
        # Preserve successful independent services, but do not reset failed ones.
        save_state(new)
    return 1 if errors else 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(main())
