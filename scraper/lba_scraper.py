"""
lba_scraper.py
==============
Scraper principale LBA Serie A.
Usa endpoint_discovery.py per ottenere sempre endpoint validi prima di scrapare.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.legabasket.it"
SEASON   = "2024-2025"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ─────────────────────────────────────────
# Fetch via endpoint configurato o Playwright
# ─────────────────────────────────────────

def fetch_json(url: str) -> dict | list | None:
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return r.json()
    except Exception as e:
        print(f"      ↳ fetch_json error: {e}")
    return None


def get_html_and_intercept(url: str) -> tuple[str, list[dict]]:
    """Playwright: naviga URL e intercetta tutte le risposte JSON."""
    from playwright.sync_api import sync_playwright

    captured = []

    def on_resp(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct or response.status != 200:
            return
        if any(x in response.url for x in ["google","facebook","analytics","sentry","fonts"]):
            return
        try:
            captured.append({"url": response.url, "data": response.json()})
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(user_agent=HEADERS["User-Agent"])
        page    = ctx.new_page()
        page.on("response", on_resp)
        page.goto(url, wait_until="networkidle", timeout=40_000)
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()

    return html, captured


def extract_next_data(html: str) -> dict:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tag  = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try: return json.loads(tag.string)
        except Exception: pass
    return {}


# ─────────────────────────────────────────
# Lista partite della giornata
# ─────────────────────────────────────────

def get_games_for_round(giornata: int, stagione: str, cfg: dict) -> list[dict]:
    print(f"\n📅 Recupero partite – Giornata {giornata}...")

    ep = cfg.get("endpoints", {}).get("games_by_round", {})

    # ── Tentativo 1: endpoint configurato ──
    if ep.get("status") == "ok" and ep.get("url_template"):
        url = ep["url_template"]
        url = url.replace("{round}",  str(giornata))
        url = url.replace("{season}", stagione)
        print(f"   → endpoint configurato: {url}")
        data = fetch_json(url)
        if data:
            games = _extract_games(data, ep.get("data_path", []), giornata)
            if games:
                print(f"   ✅ {len(games)} partite da endpoint")
                return games

    # ── Tentativo 2: Playwright + intercettazione ──
    print("   → Playwright (intercettazione live)...")
    candidates = [
        f"{BASE_URL}/competition/1/lba/results",
        f"{BASE_URL}/competition/1/lba/calendar",
    ]
    for cand in candidates:
        html, api_data = get_html_and_intercept(cand)
        for resp in api_data:
            data = resp["data"]
            games = _extract_games(data, [], giornata)
            if games:
                # Aggiorna la config con il nuovo endpoint trovato
                tmpl = re.sub(re.escape(str(giornata)), "{round}", resp["url"])
                ep.update({"url_template": tmpl, "status": "ok", "sample_url": resp["url"]})
                cfg["endpoints"]["games_by_round"] = ep
                print(f"   ✅ {len(games)} partite – nuovo endpoint: {resp['url'][:60]}")
                return games
        # fallback HTML
        games = _games_from_html(html, giornata)
        if games:
            return games

    print("   ⚠️  Nessuna partita trovata")
    return []


def _extract_games(data, path: list[str], giornata: int) -> list[dict]:
    """Naviga data_path e cerca partite per la giornata."""
    node = data
    for key in path:
        if isinstance(node, dict):
            node = node.get(key, node)
        elif isinstance(node, list):
            break

    candidates = node if isinstance(node, list) else []
    if not candidates and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and len(v) > 0:
                candidates = v
                break

    # Se è una lista di rounds, cerca il round giusto
    if candidates and isinstance(candidates[0], dict):
        if "games" in candidates[0] or "matches" in candidates[0]:
            for r in candidates:
                if r.get("round") == giornata or r.get("roundNumber") == giornata or r.get("number") == giornata:
                    inner = r.get("games") or r.get("matches") or []
                    return [_norm_game(g) for g in inner]

    # Lista flat di partite
    filtered = [g for g in candidates if isinstance(g, dict) and
                (g.get("round") == giornata or g.get("roundNumber") == giornata)]
    if filtered:
        return [_norm_game(g) for g in filtered]

    # Se non c'è info round, e ci sono abbastanza partite (es. 8-10), le prendo tutte
    if candidates and isinstance(candidates[0], dict):
        text = json.dumps(candidates).lower()
        if sum(1 for k in ["hometeam","home","score","pts"] if k in text) >= 2:
            return [_norm_game(g) for g in candidates if isinstance(g, dict)]

    return []


def _games_from_html(html: str, giornata: int) -> list[dict]:
    soup  = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=re.compile(r"/game/\d+"))
    seen, games = set(), []
    for a in links:
        gid = re.search(r"/game/(\d+)", a["href"]).group(1)
        if gid not in seen:
            seen.add(gid)
            games.append({"game_id": gid, "home": "?", "away": "?",
                          "score_home": "-", "score_away": "-",
                          "url": BASE_URL + a["href"]})
    return games


def _norm_game(g: dict) -> dict:
    gid  = str(g.get("id") or g.get("gameId") or g.get("game_id") or "")
    home = g.get("homeTeam") or g.get("home") or {}
    away = g.get("awayTeam") or g.get("away") or {}
    if isinstance(home, str): home = {"name": home}
    if isinstance(away, str): away = {"name": away}
    return {
        "game_id":    gid,
        "home":       home.get("name") or home.get("teamName") or home.get("fullName") or "Home",
        "away":       away.get("name") or away.get("teamName") or away.get("fullName") or "Away",
        "score_home": g.get("homeScore") or g.get("scoreHome") or g.get("ptsh") or "-",
        "score_away": g.get("awayScore") or g.get("scoreAway") or g.get("ptsa") or "-",
        "url":        g.get("url") or f"{BASE_URL}/game/{gid}",
    }


# ─────────────────────────────────────────
# Boxscore singola partita
# ─────────────────────────────────────────

def get_boxscore(game: dict, cfg: dict) -> tuple[list[dict], list[dict]]:
    gid = game["game_id"]
    print(f"\n   🏀 {game['home']} vs {game['away']} (id:{gid})")

    ep = cfg.get("endpoints", {}).get("game_boxscore", {})

    # ── Tentativo 1: endpoint configurato ──
    if ep.get("status") == "ok" and ep.get("url_template"):
        url = ep["url_template"].replace("{game_id}", gid)
        print(f"      → endpoint: {url[:70]}")
        data = fetch_json(url)
        if data:
            hp, ap = _parse_boxscore(data, ep.get("data_path", []))
            if hp or ap:
                print(f"      ✅ {len(hp)}+{len(ap)} giocatori")
                return hp, ap

    # ── Tentativo 2: Playwright ──
    print("      → Playwright...")
    game_url = game.get("url") or f"{BASE_URL}/game/{gid}"
    html, api_data = get_html_and_intercept(game_url)

    for resp in api_data:
        hp, ap = _parse_boxscore(resp["data"], [])
        if hp or ap:
            tmpl = re.sub(re.escape(gid), "{game_id}", resp["url"])
            ep.update({"url_template": tmpl, "status": "ok", "sample_url": resp["url"]})
            cfg["endpoints"]["game_boxscore"] = ep
            print(f"      ✅ {len(hp)}+{len(ap)} giocatori – nuovo endpoint trovato")
            return hp, ap

    # ── Tentativo 3: __NEXT_DATA__ ──
    nd = extract_next_data(html)
    hp, ap = _parse_boxscore_nextdata(nd)
    if hp or ap:
        print(f"      ✅ {len(hp)}+{len(ap)} da __NEXT_DATA__")
        return hp, ap

    # ── Tentativo 4: HTML tables ──
    hp, ap = _parse_boxscore_html(html)
    if hp or ap:
        print(f"      ✅ {len(hp)}+{len(ap)} da HTML table")
    else:
        print("      ⚠️  Nessun dato trovato")
    return hp, ap


def _parse_boxscore(data, path: list[str]) -> tuple[list[dict], list[dict]]:
    node = data
    for key in path:
        if isinstance(node, dict): node = node.get(key, node)

    if isinstance(node, dict):
        ht = node.get("homeTeam") or node.get("home") or {}
        at = node.get("awayTeam") or node.get("away") or {}
        if isinstance(ht, str): ht = {}
        if isinstance(at, str): at = {}
        hp = [_norm_player(p) for p in (ht.get("players") or ht.get("boxscore") or [])]
        ap = [_norm_player(p) for p in (at.get("players") or at.get("boxscore") or [])]
        if hp or ap: return hp, ap

        # Struttura flat con teamSide
        players = node.get("players") or []
        if players:
            home = [_norm_player(p) for p in players if p.get("teamSide") in ("home","H",1,"1")]
            away = [_norm_player(p) for p in players if p.get("teamSide") in ("away","A",2,"2")]
            if home or away: return home, away

    return [], []


def _parse_boxscore_nextdata(nd: dict) -> tuple[list[dict], list[dict]]:
    try:
        props = nd.get("props", {}).get("pageProps", {})
        game  = props.get("game") or props.get("gameData") or props.get("data", {})
        if not game: return [], []
        return _parse_boxscore(game, [])
    except Exception:
        return [], []


def _parse_boxscore_html(html: str) -> tuple[list[dict], list[dict]]:
    soup    = BeautifulSoup(html, "html.parser")
    tables  = soup.find_all("table")
    results = []
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 3: continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th","td"])]
        if not any(h in headers for h in ["min","minuti","val","valutazione","pts","punti"]): continue
        players = []
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
            if len(cells) < 3: continue
            p = dict(zip(headers, cells))
            players.append({
                "name":    p.get("giocatore") or p.get("player") or p.get("nome") or cells[0],
                "number":  p.get("#") or p.get("n.") or "-",
                "minutes": p.get("min") or p.get("minuti") or "-",
                "rating":  p.get("val") or p.get("valutazione") or p.get("rating") or "-",
                "pts": p.get("pts") or "-", "reb": p.get("reb") or "-",
                "ast": p.get("ast") or "-", "stl": p.get("stl") or "-",
                "blk": p.get("blk") or "-", "to":  p.get("to")  or "-",
                "fg":  p.get("fg")  or p.get("tiri") or "-",
            })
        results.append(players)
    if len(results) >= 2: return results[0], results[1]
    if len(results) == 1: return results[0], []
    return [], []


def _norm_player(p: dict) -> dict:
    if not isinstance(p, dict): return {}
    name = (p.get("name") or p.get("playerName") or
            f"{p.get('lastName','')} {p.get('firstName','')}".strip())
    fg_m = p.get("fieldGoalsMade") or p.get("fgm")
    fg_a = p.get("fieldGoalsAttempted") or p.get("fga")
    fg   = f"{fg_m}/{fg_a}" if fg_m is not None and fg_a is not None else p.get("fg", "-")
    return {
        "name":    name or "–",
        "number":  p.get("number") or p.get("jersey") or p.get("shirtNumber") or "#",
        "minutes": p.get("minutes") or p.get("mins") or p.get("timePlayed") or p.get("min") or "-",
        "rating":  p.get("rating") or p.get("valutazione") or p.get("eval") or p.get("efficiency") or "-",
        "pts":     p.get("points") or p.get("pts") or "-",
        "reb":     p.get("totalRebounds") or p.get("rebounds") or p.get("reb") or "-",
        "ast":     p.get("assists") or p.get("ast") or "-",
        "stl":     p.get("steals") or p.get("stl") or "-",
        "blk":     p.get("blocks") or p.get("blk") or "-",
        "to":      p.get("turnovers") or p.get("to") or "-",
        "fg":      fg,
    }


# ─────────────────────────────────────────
# HTML output
# ─────────────────────────────────────────

def rating_class(val) -> str:
    try:
        v = float(str(val).replace(",", "."))
        return "pos" if v > 0 else ("neg" if v < 0 else "zero")
    except Exception:
        return "zero"


def players_to_table(players: list[dict]) -> str:
    if not players:
        return '<p class="no-data">Dati non disponibili</p>'
    rows = "".join(
        f'<tr>'
        f'<td class="num">{p["number"]}</td>'
        f'<td class="pname">{p["name"]}</td>'
        f'<td class="mc">{p["minutes"]}</td>'
        f'<td class="vc {rating_class(p["rating"])}">{p["rating"]}</td>'
        f'<td>{p["pts"]}</td><td>{p["reb"]}</td><td>{p["ast"]}</td>'
        f'<td>{p["stl"]}</td><td>{p["blk"]}</td><td>{p["to"]}</td>'
        f'<td class="fg">{p["fg"]}</td>'
        f'</tr>'
        for p in players if p.get("name")
    )
    return (f'<table><thead><tr>'
            f'<th>#</th><th class="th-l">Giocatore</th>'
            f'<th title="Minuti">MIN</th><th title="Valutazione">VAL</th>'
            f'<th>PTS</th><th>REB</th><th>AST</th>'
            f'<th>STL</th><th>BLK</th><th>TO</th><th>TT</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')


def build_html(giornata: int, matches: list[dict], cfg: dict) -> str:
    ts  = datetime.now().strftime("%d/%m/%Y %H:%M")
    ep_info = cfg.get("endpoints", {})
    disc = ep_info.get("game_boxscore", {}).get("discovered_at", "?")[:10]

    cards = ""
    for m in matches:
        sh, sa = m["score_home"], m["score_away"]
        wh = we = ""
        try:
            if int(sh) > int(sa): wh = "winner"
            elif int(sa) > int(sh): we = "winner"
        except Exception: pass
        cards += f"""
<section class="match-card">
  <div class="mh">
    <div class="ts {wh}"><span class="tn">{m['home']}</span><span class="sc">{sh if sh!='-' else '–'}</span></div>
    <div class="vs-sep">VS</div>
    <div class="ts r {we}"><span class="sc">{sa if sa!='-' else '–'}</span><span class="tn">{m['away']}</span></div>
  </div>
  <div class="bg">
    <div class="bt"><div class="tl">{m['home']}</div>{players_to_table(m['home_players'])}</div>
    <div class="bt"><div class="tl">{m['away']}</div>{players_to_table(m['away_players'])}</div>
  </div>
</section>"""

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LBA • Giornata {giornata}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700;800;900&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
:root{{--bg:#08090c;--surf:#111318;--card:#161b24;--brd:#1e2535;--acc:#e8a923;--txt:#dde4f0;--mut:#6a7899;--grn:#22c980;--red:#f05050;--blu:#5ba4f5}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:'Barlow',sans-serif}}
.ph{{background:linear-gradient(135deg,#08090c,#111827,#1a0f00);border-bottom:2px solid var(--acc);padding:2rem 1.5rem 1.5rem;text-align:center;position:relative;overflow:hidden}}
.ph::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 80% at 50% 0%,rgba(232,169,35,.15),transparent);pointer-events:none}}
.ph h1{{font-family:'Barlow Condensed',sans-serif;font-size:clamp(1.8rem,5vw,3rem);font-weight:900;text-transform:uppercase;letter-spacing:.06em;color:var(--acc)}}
.ph p{{color:var(--mut);font-size:.82rem;margin-top:.3rem;letter-spacing:.08em}}
.back{{display:inline-block;margin-bottom:.8rem;color:var(--mut);text-decoration:none;font-size:.82rem;letter-spacing:.05em;text-transform:uppercase;transition:color .2s}}
.back:hover{{color:var(--acc)}}
.ep-badge{{display:inline-block;margin-top:.6rem;padding:.2rem .7rem;background:#1e2535;border-radius:20px;font-size:.72rem;color:var(--mut);letter-spacing:.04em}}
.ep-badge span{{color:var(--grn)}}
main{{max-width:1400px;margin:0 auto;padding:2rem 1rem}}
.match-card{{background:var(--card);border:1px solid var(--brd);border-radius:12px;margin-bottom:2rem;overflow:hidden}}
.mh{{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.5rem;gap:1rem;background:linear-gradient(90deg,#0f1520,#161b28,#0f1520);border-bottom:1px solid var(--brd)}}
.ts{{display:flex;align-items:center;gap:.8rem;flex:1}}
.ts.r{{flex-direction:row-reverse}}
.tn{{font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:1.05rem;text-transform:uppercase}}
.sc{{font-family:'Barlow Condensed',sans-serif;font-size:2rem;font-weight:800;color:var(--mut)}}
.ts.winner .sc{{color:var(--acc)}}
.vs-sep{{color:var(--brd);font-family:'Barlow Condensed',sans-serif;font-size:.85rem;font-weight:700;letter-spacing:.1em}}
.bg{{display:grid;grid-template-columns:1fr 1fr;gap:0}}
@media(max-width:900px){{.bg{{grid-template-columns:1fr}}}}
.bt{{padding:1rem}}
.bt:first-child{{border-right:1px solid var(--brd)}}
@media(max-width:900px){{.bt:first-child{{border-right:none;border-bottom:1px solid var(--brd)}}}}
.tl{{font-family:'Barlow Condensed',sans-serif;font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--acc);margin-bottom:.5rem;padding-bottom:.4rem;border-bottom:1px solid var(--brd)}}
table{{width:100%;border-collapse:collapse;font-size:.77rem}}
thead th{{background:#0d1220;color:var(--mut);font-family:'Barlow Condensed',sans-serif;font-weight:600;font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;padding:.42rem .35rem;text-align:right;border-bottom:1px solid var(--brd);white-space:nowrap}}
th.th-l{{text-align:left}}
td{{padding:.38rem .35rem;text-align:right;border-bottom:1px solid #141920;color:var(--txt)}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:rgba(255,255,255,.03)}}
td.num{{color:var(--mut);width:1.8rem}}
td.pname{{text-align:left;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:130px}}
td.mc{{color:var(--blu);font-family:'Barlow Condensed',sans-serif;font-weight:600}}
td.vc{{font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:.86rem}}
td.vc.pos{{color:var(--grn)}}
td.vc.neg{{color:var(--red)}}
td.vc.zero{{color:var(--mut)}}
td.fg{{color:var(--mut);font-size:.7rem}}
.no-data{{color:var(--mut);font-style:italic;padding:.6rem;font-size:.82rem}}
footer{{text-align:center;color:var(--mut);font-size:.72rem;padding:1.5rem;border-top:1px solid var(--brd);margin-top:1rem;letter-spacing:.04em}}
a{{color:var(--acc);text-decoration:none}}
a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="ph">
  <a href="index.html" class="back">← Tutte le giornate</a>
  <h1>Giornata {giornata}</h1>
  <p>Lega Basket Serie A · Boxscore completo</p>
  <div class="ep-badge">endpoint scoperto: <span>{disc}</span></div>
</div>
<main>{cards or '<p style="color:var(--mut);text-align:center;padding:3rem">Nessuna partita trovata.</p>'}</main>
<footer>Dati estratti da <a href="https://www.legabasket.it" target="_blank">legabasket.it</a> · Generato {ts}</footer>
</body>
</html>"""


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--giornata",        type=int,   required=True)
    parser.add_argument("--stagione",        default=SEASON)
    parser.add_argument("--output",          default="")
    parser.add_argument("--force-discovery", action="store_true")
    parser.add_argument("--delay",           type=float, default=2.0)
    args = parser.parse_args()

    outfile = args.output or f"lba_giornata_{args.giornata}.html"

    # 1. Valida / scopri endpoint
    from endpoint_discovery import ensure_valid_endpoints
    cfg = ensure_valid_endpoints(force_discovery=args.force_discovery)

    # 2. Lista partite
    games = get_games_for_round(args.giornata, args.stagione, cfg)
    if not games:
        print("❌ Nessuna partita trovata.")
        Path(outfile).parent.mkdir(parents=True, exist_ok=True)
        Path(outfile).write_text(build_html(args.giornata, [], cfg), encoding="utf-8")
        sys.exit(1)

    print(f"\n✅ {len(games)} partite – scarico boxscore...")

    # 3. Boxscore
    matches = []
    for i, g in enumerate(games, 1):
        if i > 1: time.sleep(args.delay)
        hp, ap = get_boxscore(g, cfg)
        matches.append({**g, "home_players": hp, "away_players": ap})

    # 4. HTML
    html = build_html(args.giornata, matches, cfg)
    Path(outfile).parent.mkdir(parents=True, exist_ok=True)
    Path(outfile).write_text(html, encoding="utf-8")
    print(f"\n✅ Salvato: {outfile}")


if __name__ == "__main__":
    main()
