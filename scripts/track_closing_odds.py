#!/usr/bin/env python3
"""
track_closing_odds.py — захватывает closing odds для ставок AUTO_ELO_FLAT.

Запускается каждые 5 минут. Ищет ставки у которых:
  - settled=False
  - stake_usd > 0  (реальные ставки, не трекинг)
  - real_odds IS NOT NULL
  - closing_odds IS NULL
  - start_time через <= 20 минут ИЛИ уже прошёл <= 5 минут назад
    (за 5 мин до старта рынок считается "закрытым")

Для каждой такой ставки:
  1. Запрашивает BetsAPI upcoming за текущими odds
  2. Сохраняет closing_odds
  3. Вычисляет CLV = real_odds / closing_odds - 1
     > 0 означает что мы взяли odds лучше рынка — sharper signal
     < 0 означает что рынок нас "переоценил" — слабый сигнал

CLV (Closing Line Value) — ведущий индикатор качества модели.
На длинной дистанции положительный CLV = системное преимущество.
Не зависит от результата конкретного матча, только от качества ценообразования.

Run:
    python3 scripts/track_closing_odds.py

GitHub Actions: closing_odds.yml — каждые 5 минут.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_ANON_KEY"]
BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

STRATEGY_NAME  = "AUTO_ELO_FLAT"
WINDOW_BEFORE  = 20 * 60   # секунд до старта — начинаем ловить closing
WINDOW_AFTER   = 5  * 60   # секунд после старта — ещё принимаем как closing
PREFERRED_BM   = ["PinnacleSports", "Pinnacle", "Bet365", "GGBet", "MelBet", "1xBet"]
ODDS_FUZZY_MIN = 0.55


def sb(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    r = getattr(requests, method.lower())(url, headers=HEADERS, json=body, timeout=30)
    r.raise_for_status()
    try: return r.json()
    except Exception: return []


def bapi(path, params=None):
    if not BETSAPI_TOKEN: return {}
    url = f"https://api.betsapi.com{path}"
    p = {"token": BETSAPI_TOKEN, **(params or {})}
    for attempt in range(3):
        try:
            r = requests.get(url, params=p, timeout=12)
            if r.status_code == 429: time.sleep(6); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2: print(f"  [BetsAPI] {path}: {e}"); return {}
        time.sleep(1.2)
    return {}


def clean(s): return re.sub(r"\s+", " ", (s or "").strip().lower())
def fuzzy(a, b): return SequenceMatcher(None, a, b).ratio()


def fetch_betsapi_upcoming():
    events, page = [], 1
    while page <= 3:
        data = bapi("/v3/events/upcoming", {"sport_id": 151, "page": page})
        res = data.get("results", [])
        if not res: break
        events.extend(res)
        time.sleep(1.0)
        if len(res) < 50: break
        page += 1
    return events


def find_event(events, home, away, start_ts):
    """Fuzzy match по имени + ±8ч окно."""
    home_c, away_c = clean(home), clean(away)
    best, best_score, best_rev = None, 0.0, False
    for ev in events:
        ev_ts = int(ev.get("time", 0))
        if abs(ev_ts - start_ts) > 8 * 3600: continue
        h = clean(ev.get("home", {}).get("name", ""))
        a = clean(ev.get("away", {}).get("name", ""))
        sn = fuzzy(home_c, h) + fuzzy(away_c, a)
        sr = fuzzy(home_c, a) + fuzzy(away_c, h)
        score, rev = (sn, False) if sn >= sr else (sr, True)
        if score > best_score and score >= ODDS_FUZZY_MIN * 2:
            best_score, best, best_rev = score, ev, rev
    return best, best_rev


def extract_odds(odds_data, bet_side):
    results = odds_data.get("results", {})
    if not isinstance(results, dict): return None, None
    candidates = []
    for bm_name, bm_data in results.items():
        if not isinstance(bm_data, dict): continue
        for _, mdata in bm_data.items():
            if not isinstance(mdata, dict): continue
            olist = mdata.get("odds", [])
            if not isinstance(olist, list) or len(olist) < 2: continue
            try:
                def _f(x): return float(x["odds"] if isinstance(x, dict) else x)
                o_home, o_away = _f(olist[0]), _f(olist[1])
            except Exception: continue
            if o_home <= 1.0 or o_away <= 1.0: continue
            our = o_home if bet_side == "home" else o_away
            prio = next((i for i, p in enumerate(PREFERRED_BM)
                         if p.lower() in bm_name.lower()), len(PREFERRED_BM))
            candidates.append((prio, our, bm_name))
    if not candidates: return None, None
    _, best_odds, best_bm = sorted(candidates)[0]
    return round(best_odds, 4), best_bm


def main():
    if not BETSAPI_TOKEN:
        print("BETSAPI_TOKEN не задан — выход")
        return

    now_ts = int(datetime.now(timezone.utc).timestamp())

    # Ставки которым нужен closing odds (в window ±20мин/5мин от старта)
    bets = sb("GET",
        f"elo_paper_bets"
        f"?strategy_name=eq.{STRATEGY_NAME}"
        f"&settled=eq.false"
        f"&stake_usd=gt.0"
        f"&closing_odds=is.null"
        f"&select=id,home_team,away_team,start_time,bet_team,real_odds"
        f"&order=start_time.asc&limit=50"
    )

    in_window = [
        b for b in bets
        if b.get("start_time") and
           -WINDOW_AFTER <= (b["start_time"] - now_ts) <= WINDOW_BEFORE
    ]

    print(f"Ставок в closing-window: {len(in_window)} / {len(bets)} pending")
    if not in_window:
        return

    events = fetch_betsapi_upcoming()
    print(f"BetsAPI upcoming events: {len(events)}")

    updated = 0
    for bet in in_window:
        home = bet["home_team"]
        away = bet["away_team"]
        st   = bet["start_time"]
        bet_side = bet.get("bet_team", "home")

        ev, rev = find_event(events, home, away, st)
        if not ev:
            print(f"  [не найден] {home} vs {away}")
            continue

        time.sleep(1.0)
        odds_data = bapi("/v2/event/odds/summary", {"event_id": ev.get("id")})
        eff_side  = ("away" if bet_side == "home" else "home") if rev else bet_side
        closing, _ = extract_odds(odds_data, eff_side)

        if not closing:
            print(f"  [нет closing] {home} vs {away}")
            continue

        real_o = bet.get("real_odds")
        clv    = round(float(real_o) / closing - 1, 4) if real_o else None
        clv_s  = f"{clv:+.2%}" if clv is not None else "—"
        print(f"  ✓ {home} vs {away}  real={real_o} → closing={closing}  CLV={clv_s}")

        sb("PATCH",
            f"elo_paper_bets?id=eq.{bet['id']}",
            {"closing_odds": closing, "clv": clv}
        )
        updated += 1

    print(f"\nClosing odds сохранено: {updated}/{len(in_window)}")


if __name__ == "__main__":
    main()
