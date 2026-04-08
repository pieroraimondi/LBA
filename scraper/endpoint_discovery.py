"""
endpoint_discovery.py
=====================
Motore di auto-discovery e validazione degli endpoint API di legabasket.it.

Funzionamento:
  1. load_config()         → legge api_config.json
  2. validate_endpoints()  → verifica che gli endpoint salvati funzionino ancora
  3. Se invalidi → discover_endpoints()  → Playwright intercetta tutto il traffico
  4. analyze_traffic()     → analizza le richieste e individua gli endpoint utili
  5. save_config()         → salva api_config.json aggiornato (poi il workflow lo committa)
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

BASE_URL    = "https://www.legabasket.it"
CONFIG_PATH = Path(__file__).parent / "api_config.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Accept-Language": "it-IT,it;q=0.9",
}


# ─────────────────────────────────────────
# Config I/O
# ─────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"endpoints": {}, "fallback_game_ids": {}}


def save_config(cfg: dict):
    cfg["_last_updated"] = datetime.now(timezone.utc).isoformat()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"💾 api_config.json salvato")


# ─────────────────────────────────────────
# Validazione endpoint esistenti
# ─────────────────────────────────────────

def validate_endpoints(cfg: dict) -> bool:
    """
    Ritorna True se almeno boxscore e games_by_round funzionano.
    """
    eps  = cfg.get("endpoints", {})
    ok   = 0
    need = {"games_by_round", "game_boxscore"}

    for name in need:
        ep = eps.get(name, {})
        if ep.get("status") != "ok" or not ep.get("url_template"):
            print(f"   ⚠️  {name}: non configurato")
            continue

        # costruisci un URL di test con i sample values
        sample  = cfg.get("fallback_game_ids", {})
        url     = ep["url_template"]
        url     = url.replace("{game_id}", str(sample.get("sample_game_id", "25212")))
        url     = url.replace("{round}",   str(sample.get("sample_round", 25)))
        url     = url.replace("{season}",  str(sample.get("sample_season", "2024-2025")))

        print(f"   🔍 Valido {name}: {url}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if _looks_like_basketball_data(data, name):
                    print(f"   ✅ {name} OK")
                    ok += 1
                else:
                    print(f"   ❌ {name}: risposta non riconosciuta")
                    ep["status"] = "stale"
            else:
                print(f"   ❌ {name}: HTTP {r.status_code}")
                ep["status"] = "stale"
        except Exception as e:
            print(f"   ❌ {name}: {e}")
            ep["status"] = "stale"

    cfg["_last_validated"] = datetime.now(timezone.utc).isoformat()
    return ok == len(need)


def _looks_like_basketball_data(data, endpoint_name: str) -> bool:
    """Controlla euristicamente se il JSON sembra dati basket LBA."""
    text = json.dumps(data).lower()
    keywords = ["team", "player", "game", "round", "score", "minutes",
                "partita", "giocatore", "minuti", "punti", "rimbalzi",
                "home", "away", "pts", "reb", "ast"]
    hits = sum(1 for k in keywords if k in text)
    return hits >= 3


# ─────────────────────────────────────────
# Discovery via Playwright
# ─────────────────────────────────────────

def discover_endpoints(cfg: dict) -> dict:
    """
    Naviga le pagine chiave con Playwright, intercetta tutto il traffico
    XHR/fetch e analizza le risposte per trovare gli endpoint API.
    Ritorna il cfg aggiornato.
    """
    print("\n🔎 DISCOVERY MODE – avvio Playwright...")
    from playwright.sync_api import sync_playwright

    sample = cfg.get("fallback_game_ids", {})
    gid    = sample.get("sample_game_id", "25212")
    rnd    = sample.get("sample_round", 25)

    # Pagine da visitare per la discovery
    target_pages = [
        f"{BASE_URL}/competition/1/lba/results",
        f"{BASE_URL}/game/{gid}",
    ]

    all_requests: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": "it-IT,it;q=0.9"},
        )

        for page_url in target_pages:
            print(f"   📄 Navigo: {page_url}")
            page = ctx.new_page()

            captured = []

            def on_response(response, _url=page_url):
                ct     = response.headers.get("content-type", "")
                req_url= response.url
                method = response.request.method

                # Filtra solo JSON
                if "json" not in ct:
                    return
                if response.status != 200:
                    return
                # Ignora analytics, CDN, ecc.
                if any(x in req_url for x in ["google", "facebook", "analytics",
                                               "hotjar", "sentry", "cloudfront",
                                               "fonts", "gtm", ".svg", ".png"]):
                    return
                try:
                    body = response.json()
                    captured.append({
                        "url":      req_url,
                        "method":   method,
                        "status":   response.status,
                        "data":     body,
                        "from_page": _url,
                    })
                    print(f"      📥 {method} {req_url[:80]}")
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                page.goto(page_url, wait_until="networkidle", timeout=40_000)
                page.wait_for_timeout(3000)  # lazy-load extra
            except Exception as e:
                print(f"      ⚠️  timeout/errore: {e}")
            finally:
                all_requests.extend(captured)
                page.close()

        browser.close()

    print(f"\n   📊 {len(all_requests)} risposte JSON intercettate")

    # Analisi
    cfg = _analyze_traffic(all_requests, cfg, rnd, gid)
    cfg["_last_discovery"] = datetime.now(timezone.utc).isoformat()
    return cfg


# ─────────────────────────────────────────
# Analisi del traffico intercettato
# ─────────────────────────────────────────

def _analyze_traffic(requests_: list[dict], cfg: dict, sample_round: int, sample_gid: str) -> dict:
    """
    Classifica le richieste intercettate e aggiorna cfg["endpoints"].
    """
    eps = cfg.setdefault("endpoints", {})

    for req in requests_:
        url  = req["url"]
        data = req["data"]
        text = json.dumps(data).lower()

        # ── games_by_round ──
        if eps.get("games_by_round", {}).get("status") != "ok":
            if _is_games_list(data, sample_round):
                path, params = _extract_template(url, sample_round=sample_round, sample_gid=sample_gid)
                print(f"   🎯 games_by_round trovato: {url}")
                eps["games_by_round"] = {
                    "url_template": path,
                    "params":       params,
                    "status":       "ok",
                    "sample_url":   url,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                    "data_path":    _detect_data_path(data, ["games","matches","results"]),
                    "notes":        "Lista partite per giornata",
                }

        # ── game_boxscore ──
        if eps.get("game_boxscore", {}).get("status") != "ok":
            if _is_boxscore(data):
                path, params = _extract_template(url, sample_round=sample_round, sample_gid=sample_gid)
                print(f"   🎯 game_boxscore trovato: {url}")
                eps["game_boxscore"] = {
                    "url_template": path,
                    "params":       params,
                    "status":       "ok",
                    "sample_url":   url,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                    "data_path":    _detect_data_path(data, ["homeTeam","players","boxscore","home"]),
                    "notes":        "Statistiche giocatori di una partita",
                }

        # Se entrambi trovati, esci prima
        if (eps.get("games_by_round",{}).get("status") == "ok" and
            eps.get("game_boxscore",{}).get("status") == "ok"):
            break

    # Salva tutte le richieste raw come riferimento futuro
    cfg["_raw_discovery_sample"] = [
        {"url": r["url"], "from_page": r["from_page"]}
        for r in requests_[:30]  # max 30 per non gonfiare il file
    ]

    return cfg


def _is_games_list(data: dict | list, sample_round: int) -> bool:
    """Controlla se il JSON sembra una lista di partite con round corrispondente."""
    text = json.dumps(data).lower()
    hits = sum(1 for k in ["hometeam","awayteam","homescore","awayscore",
                            "home","away","round","score","game"] if k in text)
    return hits >= 3


def _is_boxscore(data: dict | list) -> bool:
    """Controlla se il JSON sembra un boxscore (ha giocatori con statistiche)."""
    text = json.dumps(data).lower()
    player_signals = sum(1 for k in ["minutes","mins","timeplayed","rating","eval",
                                     "efficiency","players","boxscore","points",
                                     "rebounds","assists","minuti","valutazione"] if k in text)
    return player_signals >= 3


def _extract_template(url: str, sample_round: int, sample_gid: str) -> tuple[str, dict]:
    """
    Sostituisce i valori campione con placeholder nel URL.
    Es: .../game/25212/stats → .../game/{game_id}/stats
    """
    tmpl = url
    tmpl = re.sub(re.escape(str(sample_gid)), "{game_id}", tmpl)
    tmpl = re.sub(r'\b' + re.escape(str(sample_round)) + r'\b', "{round}", tmpl)

    # Estrai query params
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params = {k: v[0] for k, v in params.items()}  # semplifica

    return tmpl, params


def _detect_data_path(data, keys: list[str]) -> list[str]:
    """Trova il percorso annidato verso i dati principali."""
    if isinstance(data, list):
        return []
    if isinstance(data, dict):
        for key in keys:
            if key in data:
                return [key]
        for k, v in data.items():
            if isinstance(v, dict):
                sub = _detect_data_path(v, keys)
                if sub:
                    return [k] + sub
            elif isinstance(v, list) and v:
                if any(key in json.dumps(v).lower() for key in keys):
                    return [k]
    return []


# ─────────────────────────────────────────
# Funzione principale
# ─────────────────────────────────────────

def ensure_valid_endpoints(force_discovery: bool = False) -> dict:
    """
    Entry point principale.
    Ritorna la config con endpoint validi (dopo discovery se necessario).
    """
    cfg = load_config()
    print("\n🔧 Verifica endpoint API legabasket.it...")

    if force_discovery:
        print("   🔄 Discovery forzata")
        cfg = discover_endpoints(cfg)
        save_config(cfg)
        return cfg

    valid = validate_endpoints(cfg)

    if not valid:
        print("\n   ⚠️  Endpoint non validi – avvio discovery automatica...")
        cfg = discover_endpoints(cfg)

        # Ri-valida dopo discovery
        valid2 = validate_endpoints(cfg)
        if valid2:
            print("\n   ✅ Nuovi endpoint validati con successo!")
        else:
            print("\n   ⚠️  Discovery completata ma validazione automatica non conclusiva.")
            print("   Procedo comunque con i dati trovati (fallback HTML).")

        save_config(cfg)
    else:
        print("   ✅ Endpoint validi, nessuna discovery necessaria")
        cfg["_last_validated"] = datetime.now(timezone.utc).isoformat()
        save_config(cfg)

    return cfg


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Forza re-discovery")
    args = parser.parse_args()
    cfg = ensure_valid_endpoints(force_discovery=args.force)
    print(json.dumps(cfg.get("endpoints", {}), indent=2, ensure_ascii=False))
