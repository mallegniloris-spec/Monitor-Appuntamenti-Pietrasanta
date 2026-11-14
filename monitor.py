"""Monitor non invasivo di slot appuntamenti; configurazione per singolo istruttore.
Non prenota, non aggira login/CAPTCHA; segnala soltanto slot positivamente riconosciuti.
"""
import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
STATE = ROOT / 'stato.json'
TZ = ZoneInfo('Europe/Rome')
LOG = ROOT / 'monitor.log'


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def telegram(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not (token and chat):
        return False
    body = urllib.parse.urlencode({'chat_id':chat,'text':message, 'disable_web_page_preview':'true'}).encode()
    with urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=body), timeout=20) as r:
        result = json.load(r)
        if not result.get('ok'):
            raise RuntimeError('Telegram non ha confermato l\'invio')
    return True


def email(message):
    host = os.getenv('SMTP_HOST','').strip()
    user = os.getenv('SMTP_USER','').strip()
    password = os.getenv('SMTP_PASSWORD','').strip()
    recipient = os.getenv('EMAIL_TO','').strip()
    if not all((host,user,password,recipient)):
        return False
    m = EmailMessage()
    m['Subject'] = '[Pietrasanta] Nuova disponibilità appuntamento'
    m['From'] = os.getenv('EMAIL_FROM',user)
    m['To'] = recipient
    m.set_content(message)
    port = int(os.getenv('SMTP_PORT','465'))
    if port == 465:
        with smtplib.SMTP_SSL(host,port,timeout=20) as s:
            s.login(user,password)
            s.send_message(m)
    else:
        with smtplib.SMTP(host,port,timeout=20) as s:
            s.starttls()
            s.login(user,password)
            s.send_message(m)
    return True


def send(message):
    successes = []
    errors = []
    for name, func in [('Telegram', telegram), ('Email',email)]:
        try:
            if func(message): successes.append(name)
        except Exception as exc:
            errors.append(f'{name}: {exc}')
    if errors:
        logging.error('Errori invio: %s', '; '.join(errors))
    return bool(successes)


def run_steps(page, steps):
    for i, step in enumerate(steps,1):
        action = step.get('action','click_text')
        timeout = int(step.get('timeout_ms', 12000))
        logging.info('Passaggio %s: %s',i, action)
        if action == 'click_text':
            page.get_by_text(step['text'], exact=bool(step.get('exact',True))).first.click(timeout=timeout)
        elif action == 'click_button':
            page.get_by_role('button', name=step['name'], exact=bool(step.get('exact',True))).first.click(timeout=timeout)
        elif action == 'click_css':
            page.locator(step['selector']).first.click(timeout=timeout)
        elif action == 'select_label':
            page.get_by_label(step['label']).select_option(label=step['value'],timeout=timeout)
        elif action == 'wait_css':
            page.locator(step['selector']).first.wait_for(state=step.get('state','visible'),timeout=timeout)
        elif action == 'wait_ms':
            page.wait_for_timeout(min(int(step.get('ms',1000)),10000))
        else:
            raise ValueError(f'Azione non supportata: {action}')
        page.wait_for_timeout(500)


def check_operator(browser, item, diagnostic=False):
    """Risultati solo da elementi CSS configurati; niente deduzioni da testo generico."""
    page = browser.new_page()
    name = item['name']
    try:
        page.goto(item['url'], wait_until='domcontentloaded', timeout=45000)
        run_steps(page,item.get('steps',[]))
        if diagnostic:
            folder = ROOT / 'diagnostica'
            folder.mkdir(exist_ok=True)
            safe = re.sub(r'[^a-zA-Z0-9_-]','_',name)
            page.screenshot(path=str(folder/f'{safe}.png'),full_page=True,timeout=10000)
            (folder/f'{safe}.txt').write_text(page.locator('body').inner_text(timeout=10000)[:18000],encoding='utf-8')
        selector = item.get('slot_selector','').strip()
        if not selector:
            logging.warning('%s: slot_selector non configurato. Nessun avviso inviato.',name)
            return None
        page.locator(selector).first.wait_for(state='attached',timeout=int(item.get('wait_slots_ms',10000)))
        matches = page.locator(selector)
        slots = []
        for i in range(min(matches.count(), 200)):
            el = matches.nth(i)
            if not el.is_visible() or not el.is_enabled():
                continue
            value = (el.inner_text(timeout=2000) or el.get_attribute('aria-label') or '').strip()
            value = ' '.join(value.split())
            if not value: continue
            if item.get('slot_pattern') and not re.search(item['slot_pattern'],value,re.I):
                continue
            if item.get('exclude_pattern') and re.search(item['exclude_pattern'],value,re.I):
                continue
            slots.append(value)
        return {'slots':sorted(set(slots)),'url':page.url}
    finally:
        page.close()


def cycle(config, diagnostic=False):
    state = load_json(STATE,{})
    changed = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for item in config['operators']:
                if not item.get('enabled',False): continue
                name = item['name']
                logging.info('Controllo %s',name)
                try:
                    result = check_operator(browser,item,diagnostic)
                    if result is None: continue
                    slots = result['slots']
                    previous = set(state.get(name,[]))
                    newly = sorted(set(slots)-previous)
                    logging.info('%s: %d possibili slot; %d nuovi',name,len(slots),len(newly))
                    if newly and diagnostic:
                        logging.info('%s: diagnostica, notifiche disabilitate',name)
                    elif newly:
                        message = (f'APPUNTAMENTI PIETRASANTA - NUOVA DISPONIBILITA\n'
                                   f'Istruttore: {name}\n'
                                   f'Orari rilevati: {", ".join(newly[:15])}\n'
                                   f'Controllo: {datetime.now(TZ):%d/%m/%Y %H:%M}\n'
                                   f'Prenota subito: {result["url"]}\n'
                                   f'Gli slot possono esaurirsi prima della prenotazione.')
                        if send(message):
                            logging.info('Notifica inviata per %s',name)
                            state[name]=slots
                            changed=True
                        else:
                            logging.warning('Nessun canale di notifica configurato o invio fallito; ritento al prossimo ciclo')
                    else:
                        state[name]=slots
                        changed=True
                except Exception as exc:
                    logging.exception('Controllo non riuscito per %s: %s',name,exc)
        finally:
            browser.close()
    if changed: save_json(STATE,state)
    elif not STATE.exists(): save_json(STATE,state)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true',help='Esegui un solo ciclo')
    parser.add_argument('--diagnostic',action='store_true',help='Browser visibile, screenshot/testo in diagnostica/')
    parser.add_argument('--test-notify',action='store_true',help='Invia test Telegram/email')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[logging.StreamHandler(),logging.FileHandler(LOG,encoding='utf-8')])
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    if args.test_notify:
        ok=send('TEST - Monitor appuntamenti Comune di Pietrasanta: notifiche configurate correttamente.')
        print('TEST INVIATO' if ok else 'NESSUN CANALE FUNZIONANTE: controllare .env e monitor.log')
        return 0 if ok else 1
    config=load_json(CONFIG,None)
    if not config:
        print('config.json mancante')
        return 1
    if not any(x.get('enabled',False) and (args.diagnostic or x.get('slot_selector')) for x in config.get('operators',[])):
        print('CONFIGURAZIONE NECESSARIA: attivare un istruttore e (per il monitor reale) impostare slot_selector. Leggere GUIDA_CLOUD_TELEGRAM.md')
        return 2
    while True:
        cycle(config,diagnostic=args.diagnostic)
        if args.once or args.diagnostic: return 0
        mins=max(10,int(config.get('interval_minutes',15)))
        logging.info('Prossimo controllo fra %s minuti',mins)
        time.sleep(mins*60)


if __name__ == '__main__':
    sys.exit(main())
