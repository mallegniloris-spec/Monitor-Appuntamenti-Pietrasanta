# Monitor Pietrasanta - versione online (GitHub Actions + Telegram)

## Stato del progetto (leggere)
Questa e' una BASE DI CONFIGURAZIONE, non un monitor gia' collaudato.
I singoli istruttori partono disabilitati (`enabled: false`) e senza selettori di slot.
E' intenzionale: i calendari sono dinamici e non si deve inviare un falso allarme sulla base del testo generale della pagina.
Per ogni istruttore bisogna identificare i click e il selettore di un orario prenotabile e provarlo.
Non vengono effettuate prenotazioni automatiche e non vengono aggirati CAPTCHA o autenticazioni.

## A. Crea il bot Telegram
1. Installa Telegram sull'iPhone, accedi al tuo account e abilita notifiche.
2. Cerca `@BotFather` (bot ufficiale con verifica), apri la chat, invia `/newbot`.
3. Scegli un nome e username che termina in `bot`. Copia il TOKEN restituito.
   Non inviarlo in chat o in un repository.
4. Apri il tuo nuovo bot e premi Avvia / Start (o invia `/start`).
5. Sul browser apri: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Sostituisci `<TOKEN>` con il tuo vero token SOLO nel tuo browser; nella risposta cerca `"chat":{"id":...}` (numero chat_id).
   Se il risultato e' vuoto, invia un messaggio al bot e aggiorna il browser.
6. Conserva TOKEN e chat_id solo nei Secrets GitHub (passo C).

## B. Crea un repository GitHub
1. Vai su https://github.com e accedi o crea un account.
2. Crea un repository, ad esempio `monitor-appuntamenti-pietrasanta` (preferibilmente privato).
3. Carica il CONTENUTO di questa cartella nella radice del repository, inclusa la cartella nascosta `.github/workflows`.
   Se GitHub non conserva i file nascosti con upload via browser, crea `.github/workflows/monitor.yml` usando Add file > Create new file.
4. Verifica che in GitHub esistano `monitor.py`, `requirements.txt`, `config.json`, `.github/workflows/monitor.yml`.
5. NON caricare `.env`, `stato.json`, `monitor.log`, `diagnostica/`.

## C. Metti i dati Telegram in GitHub Secrets
Repository > Settings > Secrets and variables > Actions > New repository secret.
Crea `TELEGRAM_BOT_TOKEN` (valore: token BotFather).
Crea `TELEGRAM_CHAT_ID` (valore: numero chat_id).
Nessuno dei due va messo in config.json.

## D. Prova Telegram senza mettere in funzione il monitor
In GitHub > Actions > Controllo appuntamenti Pietrasanta > Run workflow.
Finche' gli istruttori sono disattivati il monitor esce con errore di configurazione; e' previsto. Per il collaudo della pagina attiva `enabled: true` anche senza `slot_selector` e lancia `diagnostic: true`.
Per fare il test Telegram usa su PC (con Python installato) `python monitor.py --test-notify`, impostando le variabili d'ambiente TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID oppure usando `.env` locale.
Alternativa cloud: vedi file `test_telegram.yml` in `.github/workflows`, avvia `Test notifica Telegram` da Actions > Run workflow.

## E. Collaudo di ogni istruttore
Apri https://www.comune.pietrasanta.lu.it/it/book nel browser e naviga fino all'istruttore desiderato.
Annota gli esatti passaggi e se compare un iframe, un popup o accesso con credenziali.
Apri `config.json` in GitHub > pulsante matita: per il primo istruttore imposta `enabled: true`, `steps` con i click e `slot_selector` con un CSS che punti SOLO a pulsanti di orari disponibili, non a date generiche.
Esempio solo illustrativo (NON FUNZIONERA' senza adattarlo al sito):
`"steps": [{"action":"click_button","name":"Prenota"}], "slot_selector":"button.slot.available"`
Per visualizzare gli elementi selezionati, Actions > Controllo appuntamenti Pietrasanta > Run workflow > diagnostic true.
Al termine scarica artifact `schermate-calendari` (screenshot + testo) e controlla la pagina.
Per l'avvio effettivo, disattiva diagnostic, verifica che negli output compaiano slot validi e testa due stati: nessun orario / orario disponibile.
Poi ripeti per ogni istruttore.

## F. Controlli programmati
La pianificazione `7,22,37,52 * * * *` equivale a un lancio ogni 15 minuti (UTC; in questo caso l'intervallo non cambia con l'ora legale).
GitHub Actions schedule e' best-effort: puo' ritardare o saltare alcuni controlli.
In repo pubblici i workflow programmati possono essere sospesi dopo 60 giorni senza attivita'; repository privati soggetti alle quote/actions del piano.
Su GitHub > Actions verifica periodicamente le esecuzioni riuscite.
Il file `stato.json` viene salvato fra le esecuzioni con Actions Cache (soluzione di semplicita', non database permanente; una pulizia cache puo' causare una nuova notifica per vecchi slot).

## G. Sicurezza e limiti
Non fare polling aggressivo. Non aggirare CAPTCHA, login o limiti del Comune.
Gli avvisi sono SOLO un supporto: non garantiscono che lo slot resti disponibile.
Il programma non prenota automaticamente, non invia dati personali al Comune.
Per assistenza al collaudo condividi screenshot del calendario senza token/chat_id o dati personali.
