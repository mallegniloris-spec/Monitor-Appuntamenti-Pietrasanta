"""Monitor calendario Pietrasanta. Non effettua prenotazioni.
Avvisa soltanto per date con orari effettivamente visibili e selezionabili.
"""
import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Europe/Rome')
CALENDAR = '.mat-calendar, mat-calendar'
DAY = 'button.mat-calendar-body-cell'
TIME_RX = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)')


def load_json(file, fallback):
    try:
        return json.loads(file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return fallback


def write_json(file, data):
    file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def notify(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        logging.error('Token Telegram o Chat ID assente nei Secrets')
        return False
    payload = urllib.parse.urlencode({'chat_id': chat_id, 'text': message,
                                      'disable_web_page_preview': 'true'}).encode()
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=payload)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return bool(json.load(response).get('ok'))
    except Exception as exc:
        logging.error('Notifica Telegram non riuscita: %s', type(exc).__name__)
        return False


def diagnostic_save(page, name, prefix):
    folder = ROOT / 'diagnostica'
    folder.mkdir(exist_ok=True)
    slug = re.sub('[^A-Za-z0-9_-]+', '_', name)[:80]
    path = folder / (slug + '_' + prefix)
    try:
        page.screenshot(path=str(path) + '.png', full_page=True, timeout=15000)
        (folder / (slug + '_' + prefix + '.txt')).write_text(
            'URL: ' + page.url + '\n' + page.locator('body').inner_text(timeout=10000)[:30000],
            encoding='utf-8')
        # DOM solo dei calendari e di possibili orari, senza dati inseriti in moduli.
        html = page.evaluate('''() => [...document.querySelectorAll('mat-calendar, .mat-calendar, mat-radio-button, [role="radio"]')]
            .slice(0, 80).map(e => e.outerHTML.slice(0, 15000)).join('\\n\\n')''')
        (folder / (slug + '_' + prefix + '_elementi.html')).write_text(html, encoding='utf-8')
    except Exception as exc:
        logging.warning('Diagnostica parziale: %s', type(exc).__name__)


def dismiss_cookie_banner(page):
    """Chiude il banner cookie con rifiuto dei cookie facoltativi."""
    for selector in [
        'button:has-text("RIFIUTA TUTTO")',
        'button:has-text("Rifiuta tutto")',
        'text="RIFIUTA TUTTO"',
    ]:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible():
                button.click(timeout=5000)
                page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


def calendar_ready(page):
    try:
        return page.locator(CALENDAR).first.is_visible(timeout=1500)
    except Exception:
        return False


def advance_booking_steps(page, item, diagnostic=False):
    """Passa da /it/book a 'Data e orario' senza compilare o inviare moduli.

    Le pagine diagnostiche mostrano 'Luogo' con ufficio gia' selezionato,
    pulsante 'Avanti', e una successiva fase 'Dettagli appuntamento'.
    L'avanzamento richiede radio gia' selezionato o nessuna scelta obbligatoria.
    """
    for step in range(1, 4):
        dismiss_cookie_banner(page)
        if calendar_ready(page):
            return page
        if diagnostic:
            diagnostic_save(page, item['name'], 'prenotazione_fase_' + str(step))
        body = page.locator('body').inner_text(timeout=8000).lower()
        if 'prenota un appuntamento' not in body and 'informazioni richieste' not in body:
            raise RuntimeError('Procedura di prenotazione non riconosciuta: nessun avanzamento')
        # Non procedere mai nella fase Richiedente, Riepilogo o conferma.
        # La parola nel breadcrumb non basta: controlliamo campi personali reali.
        if page.locator('input[autocomplete="given-name"], input[autocomplete="family-name"], input[type="email"]').count():
            raise RuntimeError('Raggiunto il modulo richiedente senza calendario: interrotto per sicurezza')
        radio = page.locator('input[type="radio"]')
        if radio.count() and not page.locator('input[type="radio"]:checked').count():
            raise RuntimeError('Nessuna opzione selezionata nella fase ' + str(step) + ': controllo manuale necessario')
        buttons = page.get_by_role('button', name=re.compile(r'^\s*Avanti\s*$', re.I))
        chosen = None
        for i in range(min(buttons.count(), 8)):
            candidate = buttons.nth(i)
            if candidate.is_visible() and candidate.is_enabled():
                chosen = candidate
                break
        if chosen is None:
            raise RuntimeError('Pulsante Avanti non disponibile nella fase ' + str(step))
        chosen.click(timeout=8000)
        page.wait_for_timeout(2000)
        logging.info('%s: avanzamento prenotazione fase %d, URL %s', item['name'], step, page.url)
        if diagnostic:
            diagnostic_save(page, item['name'], 'dopo_avanti_' + str(step))
        if calendar_ready(page):
            return page
    raise RuntimeError('Calendario non raggiunto dopo tre fasi: controllare diagnostica')


def open_calendar(page, item, diagnostic=False):
    """Apre il calendario partendo dalla pagina servizio, seguendo /it/book."""
    page.goto(item['url'], wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_cookie_banner(page)
    if diagnostic:
        diagnostic_save(page, item['name'], 'pagina_servizio')
    if calendar_ready(page):
        return page
    # Il link presente sulla pagina servizio porta spesso a /it/book,
    # con l'ufficio gia' selezionato. Non usare link del footer.
    if '/it/book' not in page.url:
        possible = [
            'main a:has-text("Prenota appuntamento")',
            'main button:has-text("Prenota appuntamento")',
            'a:has-text("Prenota appuntamento")',
        ]
        found = False
        for selector in possible:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    href = el.get_attribute('href')
                    if href and '/it/book' in href:
                        page.goto(urllib.parse.urljoin(page.url, href), wait_until='domcontentloaded', timeout=45000)
                        found = True
                        break
                    old = list(page.context.pages)
                    el.click(timeout=6000)
                    page.wait_for_timeout(1000)
                    newer = [p for p in page.context.pages if p not in old]
                    if newer:
                        page = newer[-1]
                    found = True
                    break
                except Exception:
                    continue
            if found:
                break
        if not found:
            diagnostic_save(page, item['name'], 'errore_navigazione')
            raise RuntimeError('Nessun pulsante di prenotazione trovato nella pagina servizio')
    try:
        page.wait_for_load_state('domcontentloaded', timeout=12000)
        page.wait_for_timeout(1200)
        dismiss_cookie_banner(page)
        if calendar_ready(page):
            return page
        if '/it/book' not in page.url:
            raise RuntimeError('Collegamento prenotazione non raggiunto: ' + page.url)
        return advance_booking_steps(page, item, diagnostic)
    except Exception:
        diagnostic_save(page, item['name'], 'errore_navigazione')
        raise


def enabled_dates(page):
    """Legge date solo da pulsanti Angular Material abilitati; ignora giorni passati."""
    today = datetime.now(TZ).date()
    result = []
    for button in page.locator(DAY).all():
        if not button.is_visible() or not button.is_enabled():
            continue
        if button.get_attribute('aria-disabled') == 'true':
            continue
        classes = button.get_attribute('class') or ''
        if 'mat-calendar-body-disabled' in classes:
            continue
        label = (button.get_attribute('aria-label') or '').strip()
        # Per una data non riconoscibile evitiamo falsi positivi.
        try:
            from datetime import date
            months = {'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
                      'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
                      'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12}
            d, m, y = label.lower().split()[:3]
            day = date(int(y), months[m], int(d))
        except (ValueError, KeyError):
            logging.warning('Etichetta data sconosciuta: %s', label)
            continue
        if day >= today:
            result.append((day.isoformat(), label))
    return result


def visible_times(page):
    """Orari confermati da controlli interattivi FUORI dal calendario.
    Un semplice orario testuale non e' prova di prenotabilita'.
    """
    selectors = [
        'mat-radio-button:not([aria-disabled="true"])',
        '[role="radio"]:not([aria-disabled="true"])',
        'input[type="radio"]:not(:disabled)',
        'button:not(.mat-calendar-body-cell):not([disabled]):not([aria-disabled="true"])',
        'label[for]',
    ]
    found = set()
    for selector in selectors:
        for el in page.locator(selector).all()[:160]:
            try:
                if not el.is_visible() or not el.is_enabled():
                    continue
                if el.locator('xpath=ancestor::*[contains(@class,"mat-calendar")]').count():
                    continue
                value = (el.inner_text(timeout=800) or el.get_attribute('aria-label') or '').strip()
                times = [m.group(0).replace('.', ':') for m in TIME_RX.finditer(value)]
                # Evita identificare come slot un testo descrittivo ricco di orari.
                if len(value) > 80 or len(times) != 1:
                    continue
                found.add(times[0])
            except Exception:
                continue
    return sorted(found)


def scan(browser, item, diagnostic=False, max_months=2):
    page = browser.new_page(locale='it-IT', timezone_id='Europe/Rome')
    try:
        page = open_calendar(page, item, diagnostic=diagnostic)
        if diagnostic:
            diagnostic_save(page, item['name'], 'inizio')
        slots = set()
        dates_seen = set()
        for idx in range(max_months):
            page.locator(CALENDAR).first.wait_for(state='visible', timeout=10000)
            dates = enabled_dates(page)
            logging.info('%s mese %d: %d giorni selezionabili', item['name'], idx+1, len(dates))
            for iso, label in dates:
                if iso in dates_seen:
                    continue
                dates_seen.add(iso)
                try:
                    # Cerca per data, non per posizione, per non cliccare il giorno errato.
                    cell = page.locator(DAY + '[aria-label="' + label + '"]').first
                    cell.click(timeout=4000)
                    page.wait_for_timeout(900)
                    times = visible_times(page)
                    if diagnostic and times:
                        diagnostic_save(page, item['name'], 'orari_' + iso)
                    for hour in times:
                        slots.add(iso + ' ' + hour)
                except Exception as exc:
                    logging.warning('%s: data %s non analizzabile: %s', item['name'], label, type(exc).__name__)
            if idx < max_months - 1:
                next_button = page.locator('button.mat-calendar-next-button').first
                if not next_button.count() or not next_button.is_enabled():
                    break
                next_button.click(timeout=5000)
                page.wait_for_timeout(500)
        if diagnostic:
            diagnostic_save(page, item['name'], 'fine')
        logging.info('%s: %d orari confermati, %d date selezionabili esaminate', item['name'], len(slots), len(dates_seen))
        return {'slots': sorted(slots), 'link': page.url, 'dates': len(dates_seen)}
    finally:
        page.close()


def run(diagnostic=False):
    config = load_json(ROOT / 'config.json', {})
    operators = [op for op in config.get('operators', []) if op.get('enabled')]
    if not operators:
        logging.error('Attivare almeno un istruttore con enabled=true in config.json')
        return 2
    state_file = ROOT / 'stato.json'
    state = load_json(state_file, {})
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for item in operators:
                try:
                    result = scan(browser, item, diagnostic, int(config.get('months_ahead', 2)))
                    latest = set(result['slots'])
                    previous = set(state.get(item['name'], []))
                    new = sorted(latest - previous)
                    logging.info('%s: %s nuovi orari', item['name'], len(new))
                    if diagnostic:
                        continue
                    if new:
                        message = ('🔔 APPUNTAMENTO DISPONIBILE - PIETRASANTA\n'
                                   'Istruttore: ' + item['name'] + '\n'
                                   'Orari: ' + ', '.join(new[:15]) + '\n'
                                   'Prenota: ' + result['link'] + '\n'
                                   'Disponibilita soggetta a variazioni.')
                        if not notify(message):
                            failures += 1
                            continue  # Non salvare gli slot se l'avviso non e' arrivato
                    state[item['name']] = sorted(latest)
                except Exception as exc:
                    failures += 1
                    logging.exception('%s: errore di controllo: %s', item['name'], exc)
        finally:
            browser.close()
    if not diagnostic:
        write_json(state_file, state)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--diagnostic', action='store_true')
    parser.add_argument('--test-notify', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    if args.test_notify:
        return 0 if notify('✅ TEST Monitor Appuntamenti Pietrasanta: Telegram collegato correttamente.') else 1
    return run(diagnostic=args.diagnostic)


if __name__ == '__main__':
    sys.exit(main())
