# 🏀 LBA Serie A – Statistiche Giocatori

Boxscore completo per ogni giornata della Lega Basket Serie A.  
**Scraper auto-aggiornante**: rileva automaticamente i cambiamenti agli endpoint API di legabasket.it.

## Architettura

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions (workflow_dispatch o schedule)              │
│                                                             │
│  1. endpoint_discovery.py                                   │
│     ├── Legge  api_config.json  (endpoint salvati)          │
│     ├── Valida: l'endpoint risponde ancora correttamente?   │
│     │    ├── ✅ SÌ  → procedi con scraping                  │
│     │    └── ❌ NO  → Discovery Mode:                       │
│     │         Playwright naviga il sito                     │
│     │         intercetta tutto il traffico XHR/JSON         │
│     │         identifica i nuovi endpoint                   │
│     │         salva api_config.json aggiornato              │
│     │         committa automaticamente                      │
│                                                             │
│  2. lba_scraper.py                                          │
│     ├── Scarica lista partite della giornata                │
│     ├── Per ogni partita → boxscore giocatori               │
│     └── Genera docs/giornata_N.html                         │
│                                                             │
│  3. build_index.py → aggiorna docs/index.html              │
│                                                             │
│  4. git push → GitHub Pages pubblica automaticamente        │
└─────────────────────────────────────────────────────────────┘
```

## Workflow GitHub Actions

### `scrape.yml` – Scraping manuale
Triggera su `workflow_dispatch` con:
- `giornata` (obbligatorio): numero giornata es. `25`
- `stagione` (opzionale): default `2024-2025`
- `force_discovery` (opzionale): forza re-discovery endpoint

### `validate_endpoints.yml` – Validazione automatica
- Ogni **domenica alle 06:00 UTC** (schedulato)
- Valida gli endpoint in `api_config.json`
- Se invalidi → discovery automatica + commit
- Se discovery fallisce → **apre automaticamente una Issue** con istruzioni

## Setup (5 minuti)

1. **Fork** questo repo
2. `Settings → Pages → Source: main / docs`
3. `Settings → Actions → Workflow permissions → Read and write ✅`
4. `Actions → 🏀 Scrape LBA Giornata → Run workflow → inserisci giornata`

## Struttura repo

```
.github/workflows/
  scrape.yml                ← workflow principale (manuale)
  validate_endpoints.yml    ← validazione schedulata (domenicale)

scraper/
  endpoint_discovery.py     ← motore auto-discovery e validazione
  lba_scraper.py            ← scraper principale
  build_index.py            ← genera index.html
  api_config.json           ← endpoint salvati (auto-aggiornato)

docs/
  index.html                ← home page (auto-generata)
  giornata_N.html           ← risultati giornate (auto-generati)
```

## Come funziona la self-healing discovery

Il cuore del sistema è `endpoint_discovery.py`:

```
Ogni run:
  load api_config.json
      ↓
  validate_endpoints()
    → chiama gli endpoint salvati con dati campione
    → verifica: status 200 + JSON con dati basket
      ↓
  ✅ validi → usa endpoint direttamente
  ❌ non validi → discover_endpoints()
    → Playwright apre il browser headless
    → naviga /competition/.../results e /game/XXXXX
    → intercetta TUTTE le risposte XHR/fetch JSON
    → analizza ogni risposta con _is_games_list() e _is_boxscore()
    → estrae url_template sostituendo i valori campione con {round}/{game_id}
    → salva in api_config.json
    → il workflow committa il file aggiornato
```

## Uso locale

```bash
pip install requests beautifulsoup4 playwright
playwright install chromium

# Solo validazione/discovery endpoint
python scraper/endpoint_discovery.py
python scraper/endpoint_discovery.py --force  # forza re-discovery

# Scraping completo
python scraper/lba_scraper.py --giornata 25
python scraper/lba_scraper.py --giornata 25 --force-discovery
```
