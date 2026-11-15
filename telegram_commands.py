#!/usr/bin/env python3
"""Read-only Telegram command responder, run periodically by GitHub Actions.

Commands are only accepted from TELEGRAM_CHAT_ID. Does not book appointments,
change appointment state or modify the monitor's availability baseline.
"""
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

import monitor

ROOT = pathlib.Path(__file__).resolve().parent
OFFSET_FILE = ROOT / 'telegram_offset.json'
MAX_AGE_SECONDS = 60 * 60


def telegram_api(token, method, args):
    # Never print token or URLs in logs / exception output.
    uri = 'https://api.telegram.org/bot' + token + '/' + method
    payload = urllib.parse.urlencode(args).encode('utf-8')
    req = urllib.request.Request(uri, data=payload,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.load(res)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Telegram {method}: HTTP {exc.code}') from None
    except urllib.error.URLError:
        raise RuntimeError(f'Telegram {method}: network failure') from None
    if not data.get('ok'):
        raise RuntimeError(f'Telegram {method}: API rejected request')
    return data['result']


def load_offset():
    try:
        x = json.loads(OFFSET_FILE.read_text(encoding='utf-8'))
        return max(0, int(x.get('last_update_id', -1)) + 1)
    except (ValueError, OSError, TypeError):
        return 0


def save_offset(offset):
    temp = OFFSET_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps({'last_update_id': offset - 1}, indent=2) + '\n', encoding='utf-8')
    temp.replace(OFFSET_FILE)


def command_name(text):
    first = text.strip().split(maxsplit=1)[0].casefold() if text.strip() else ''
    name = first.split('@', 1)[0]
    return name if name in ('/controlla', '/testavviso') else None


def test_alert_message():
    """Deliver an explicitly simulated notification, never an appointment claim."""
    return (
        '🧪 TEST NOTIFICA AUTOMATICA — Monitor Pietrasanta\n\n'
        'Nuovi intervalli POTENZIALI - ISTRUTTORE DI PROVA\n'
        '1 intervallo di ESEMPIO (data e ora fittizie)\n\n'
        'Questa e solo una simulazione di consegna Telegram.\n'
        'NESSUN appuntamento e stato trovato o prenotato.\n'
        'Lo storico delle disponibilita non viene modificato.\n\n'
        'Calendario ufficiale: ' + monitor.BOOKING
    )


def fetch_availability():
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    now = dt.datetime.now(monitor.TZ)
    services = {int(item['risorsa_id']): item for item in cfg['services'] if item.get('enabled', True)}
    discovery_issue = False
    if cfg.get('planning_staff'):
        try:
            for item in monitor.discover_planning(cfg):
                services.setdefault(int(item['risorsa_id']), item)
            expected = sum(bool(x.get('enabled', True)) for x in cfg['planning_staff'])
            found = sum(1 for x in services.values() if 'Pianificazione urbanistica' in x['name'])
            discovery_issue = found < expected
        except Exception:
            discovery_issue = True
            logging.warning('Planning staff discovery unavailable')
    lines = ['📅 Pietrasanta – verifica appuntamenti',
             'Controllo: ' + now.strftime('%d/%m/%Y %H:%M') + ' (ora italiana)', '']
    exclusions = monitor.load_verified_exclusions()
    potential_count = 0
    failures = 0
    for rid, service in services.items():
        name = service['name']
        try:
            raw = monitor.get_json('prenotabile', {
                'rId': rid,
                'data': int(now.timestamp() * 1000),
                'months': int(cfg.get('months', 2)),
            })
            slots = monitor.split_intervals(
                raw, int(service.get('minutes', 20)), now, int(cfg.get('days_ahead', 45)))
            slots = monitor.exclude_verified(slots, rid, exclusions)
            potential_count += len(slots)
            if slots:
                dates = [dt.datetime.fromisoformat(x).strftime('%d/%m %H:%M') for x in slots[:3]]
                extra = f' (+{len(slots)-3})' if len(slots) > 3 else ''
                lines.append(f'🟡 {name}: {len(slots)} potenziali — ' + ', '.join(dates) + extra)
            else:
                lines.append(f'⚪ {name}: 0 intervalli potenziali')
        except Exception:
            failures += 1
            logging.warning('Unable to check resource %d', rid)
            lines.append(f'⚠️ {name}: controllo non riuscito')
    if discovery_issue:
        lines.append('⚠️ Alcuni istruttori di Pianificazione non identificati: verifica incompleta.')
    lines.extend(['', f'Totale: {potential_count} intervalli potenziali; {failures} errori.',
                  'Le fasce non sono conferme di posti liberi: verifica sempre sul sito ufficiale.',
                  monitor.BOOKING])
    return '\n'.join(lines)


def run():
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    allowed_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not allowed_id:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secret')
    offset = load_offset()
    updates = telegram_api(token, 'getUpdates', {'offset': offset, 'limit': 100, 'timeout': 0,
                                                'allowed_updates': json.dumps(['message'])})
    for update in updates:
        update_id = int(update['update_id'])
        offset = max(offset, update_id + 1)
        msg = update.get('message') or {}
        chat = msg.get('chat') or {}
        if str(chat.get('id')) != allowed_id:
            save_offset(offset)
            continue
        text = msg.get('text', '')
        command = command_name(text) if isinstance(text, str) else None
        if command is None:
            save_offset(offset)
            continue
        sent_at = int(msg.get('date', 0))
        age = dt.datetime.now(dt.timezone.utc).timestamp() - sent_at
        if age < -120 or age > MAX_AGE_SECONDS:
            logging.info('Skipping stale command update %d', update_id)
            save_offset(offset)
            continue
        logging.info('Replying to %s update %d', command, update_id)
        if command == '/testavviso':
            reply = test_alert_message()
        else:
            try:
                reply = fetch_availability()
            except Exception:
                logging.exception('Availability command failed')
                reply = '⚠️ Controllo non riuscito: consulta GitHub Actions e riprova.'
        telegram_api(token, 'sendMessage', {'chat_id': allowed_id, 'text': reply,
                                            'disable_web_page_preview': 'true'})
        save_offset(offset)
    logging.info('Processed %d Telegram updates', len(updates))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        run()
    except Exception as exc:
        logging.error('Telegram command checker failed: %s', exc)
        sys.exit(1)
