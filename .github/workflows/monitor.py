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
EXCLUSIONS = ROOT / 'verified_exclusions.json'
ENGINE_REVISION = 'v3.5-verified-exclusions'
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


def parse_non_disponibile(entries):
    """Parse Municipium blocked slot start times; fail closed on unknown formats."""
    if entries is None:
        return set()
    if not isinstance(entries, list):
        raise ValueError('Unrecognized nonDisponibile collection')
    blocked = set()
    for raw in entries:
        if not isinstance(raw, str):
            raise ValueError(f'Unrecognized nonDisponibile entry: {str(raw)[:80]!r}')
        try:
            if len(raw) == 12 and raw.isascii() and raw.isdecimal():
                t = dt.datetime.strptime(raw, '%Y%m%d%H%M')
            elif len(raw) == 14 and raw.isascii() and raw.isdecimal():
                t = dt.datetime.strptime(raw, '%Y%m%d%H%M%S')
            else:
                raise ValueError
            blocked.add(t.replace(tzinfo=TZ))
        except ValueError:
            raise ValueError(f'Unrecognized nonDisponibile entry: {raw[:80]!r}') from None
    return blocked


def split_intervals(data, min_duration, not_before, max_days):
    """Candidate intervals from dated or weekly schedule; never claim booking confirmed.

    Municipal 'week' is treated as a 7-bit day mask, Sunday=1,...Saturday=64;
    this convention matches the HAR example week=32 on Friday 2026-11-20.
    For a recurring schedule the day mask must be present and valid. An
    ambiguous schedule raises rather than silently pretending no availability.
    """
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
    duration = int(min_duration)
    if duration <= 0:
        raise ValueError('Invalid slot duration')
    for schedule in data['orari']:
        if schedule.get('indisponibile') is not False:
            continue
        blocked_slots = parse_non_disponibile(schedule.get('nonDisponibile'))
        try:
            start_date, end_date = date_part(schedule['inizio']), date_part(schedule['fine'])
            period_start, period_end = schedule['oraInizio'], schedule['oraFine']
            capacity = int(schedule.get('slot', 0))
            start_time = dt.time(int(period_start[0]), int(period_start[1]))
            end_time = dt.time(int(period_end[0]), int(period_end[1]))
        except (KeyError, ValueError, TypeError, IndexError):
            raise ValueError('Unrecognized published schedule')
        if capacity < 1 or end_time <= start_time:
            continue
        if end_date < start_date:
            raise ValueError('Schedule ends before it starts')
        recurring = start_date != end_date
        mask = schedule.get('week')
        # Accept only unambiguous numeric encodings of the verified 7-bit mask.
        if isinstance(mask, str) and mask.isdecimal():
            mask = int(mask)
        if recurring:
            if mask == 0:
                # Municipium publishes special intervals with week=0 alongside
                # ordinary weekly schedules (observed for Eugenia Bonatti).
                # This does not identify a weekday. Never expand it to every
                # day: doing so would invent appointment availability. The
                # separate week=8 (Wednesday) schedule is processed normally.
                logging.warning('Ignoring undetermined week=0 schedule for resource %s (%s to %s); no weekdays inferred',
                                schedule.get('risorsaId', '?'), start_date, end_date)
                continue
            if not isinstance(mask, int) or mask < 1 or mask > 127:
                raise ValueError(f'Unrecognized weekly schedule bitmask: {mask!r} (type={type(mask).__name__})')
        no_days = schedule.get('noDays') or []
        if not isinstance(no_days, list):
            raise ValueError('Unrecognized excluded days')
        excluded = set()
        for x in no_days:
            if isinstance(x, list) and len(x) >= 3:
                excluded.add(date_part(x))
            elif isinstance(x, str):
                try:
                    excluded.add(dt.date.fromisoformat(x[:10]))
                except ValueError:
                    raise ValueError(f'Unrecognized excluded-day value: {x!r}') from None
            else:
                # Unknown exception format: do not assert these days available.
                raise ValueError(f'Unrecognized excluded-day value: {str(x)[:180]!r} (type={type(x).__name__})')
        first = max(start_date, not_before.date())
        last = min(end_date, cutoff.date())
        if first > last:
            continue
        current = first
        while current <= last:
            # Python weekday Mon=0..Sun=6 => Sunday bit 0, Monday bit 1.
            weekday_bit = 1 << ((current.weekday() + 1) % 7)
            if current not in excluded and (not recurring or mask & weekday_bit):
                beginning = dt.datetime.combine(current, start_time, TZ)
                ending = dt.datetime.combine(current, end_time, TZ)
                cur = beginning
                while cur + dt.timedelta(minutes=duration) <= ending:
                    nxt = cur + dt.timedelta(minutes=duration)
                    if cur > not_before and nxt <= cutoff and cur not in blocked_slots:
                        busy_count = sum(a < nxt and b > cur for a, b in busy)
                        if busy_count < capacity:
                            results.append(cur.isoformat())
                    cur = nxt
            current += dt.timedelta(days=1)
    return sorted(set(results))


def load_verified_exclusions():
    """Specific time slots manually verified *absent* on the public booking UI.

    Never infer recurrence from a manually observed missing slot. Missing file
    means no additional exclusions. Invalid files fail closed.
    """
    if not EXCLUSIONS.exists():
        return {}
    doc = json.loads(EXCLUSIONS.read_text(encoding='utf-8'))
    if not isinstance(doc, dict):
        raise ValueError('Invalid verified_exclusions.json')
    parsed = {}
    for rid, entries in doc.items():
        if not str(rid).isdigit() or not isinstance(entries, list):
            raise ValueError('Invalid exclusion resource or entries')
        parsed[str(rid)] = set()
        for value in entries:
            if not isinstance(value, str):
                raise ValueError('Invalid exclusion date/time')
            stamp = dt.datetime.fromisoformat(value)
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError('Exclusion must include time zone')
            parsed[str(rid)].add(stamp.isoformat())
    return parsed


def exclude_verified(slots, rid, excluded):
    """Remove only individually observed missing slots, not a whole schedule."""
    removed = set(slots) & excluded.get(str(rid), set())
    if removed:
        logging.warning('Resource %s: suppressed %d manually verified absent slot(s)', rid, len(removed))
    return sorted(set(slots) - removed)


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
    parser.add_argument('--rebaseline', action='store_true', help='Save current intervals without sending alerts; manual only')
    args = parser.parse_args()
    if args.test:
        from tests.test_logic import test_har_sample, test_benedetta_excluded_slots, test_verified_exclusions
        test_har_sample()
        test_benedetta_excluded_slots()
        test_verified_exclusions()
        print('Offline test OK: all occupied time slots correctly excluded')
        return 0
    if args.rebaseline and args.dry_run:
        parser.error('--rebaseline and --dry-run are mutually exclusive')
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    exclusions = load_verified_exclusions()
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not args.dry_run and not args.rebaseline and (not token or not chat_id):
        print('Missing Telegram credentials in GitHub secrets', file=sys.stderr)
        return 2
    old = read_state()
    new = dict(old)
    needs_migration = old.get('_engine_revision') != ENGINE_REVISION
    if needs_migration and not args.dry_run:
        logging.warning('New availability calculation: rebaseline all services without sending alerts on this run')
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
            slots = exclude_verified(slots, rid, exclusions)
            if args.dry_run:
                preview = ', '.join(dt.datetime.fromisoformat(s).strftime('%d/%m %H:%M') for s in slots[:6])
                logging.info('%s: %d possibili; primi orari: %s', name, len(slots), preview or 'nessuno')
            if args.rebaseline or (needs_migration and not args.dry_run):
                logging.info('%s: baseline updated with %d potential intervals; NO NOTIFICATIONS', name, len(slots))
                new[str(rid)] = slots
                continue
            prior = old.get(str(rid))
            if not isinstance(prior, list):
                logging.info('%s: baseline %d possible intervals (no alerts on initial scan)', name, len(slots))
            else:
                fresh = sorted(set(slots) - set(prior))
                logging.info('%s: %d possible intervals, %d newly seen', name, len(slots), len(fresh))
                if fresh:
                    sample = ', '.join(dt.datetime.fromisoformat(s).strftime('%d/%m %H:%M') for s in fresh[:5])
                    extra = f' (+{len(fresh)-5} altri)' if len(fresh) > 5 else ''
                    text = (f'Nuovi intervalli POTENZIALI - {name}\n'
                            f'{len(fresh)} intervalli rilevati: {sample}{extra}\n'
                            f'Verifica gli orari sul calendario ufficiale: {BOOKING}\n'
                            'Non sono prenotazioni confermate.')
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
        new['_engine_revision'] = ENGINE_REVISION
        save_state(new)
    elif not args.dry_run:
        # Preserve successful independent services; do not mark migration complete
        # unless every service was actually checked.
        save_state(new)
    return 1 if errors else 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(main())
