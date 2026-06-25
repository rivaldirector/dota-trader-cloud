#!/usr/bin/env python3
"""
backfill_real_odds_elo.py
Для каждого уникального матча в elo_paper_bets за последние 7 дней:
  - ищет событие в BetsAPI (ended + upcoming)
  - тянет реальные коэфы через /v2/event/odds/summary
  - обновляет real_odds + real_bookmaker во всех строках матча
"""

import os, time, re
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_ANON_KEY"]
BETSAPI_TOKEN     = os.environ.get("BETSAPI_TOKEN", "")
FUZZY_MIN         = 0.60   # минимальный порог совпадения имени команды
TIME_WINDOW_H     = 8      # ±8ч окно матча
PREFERRED_BM      = ["PinnacleSports", "Pinnacle", "Bet365", "GGBet", "MelBet", "1xBet"]

# ─── Supabase helpers ───────────────────────────────────────────────────────

def sb(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = getattr(requests, method.lower())(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return []

# ─── BetsAPI helpers ────────────────────────────────────────────────────────

def bapi(path, params=None):
    if not BETSAPI_TOKEN:
        return {}
    url = f"https://api.betsapi.com{path}"
    p = {"token": BETSAPI_TOKEN, **(params or {})}
    for attempt in range(3):
        try:
            r = requests.get(url, params=p, timeout=15)
            if r.status_code == 429:
                time.sleep(6)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                print(f"  [BetsAPI err] {path}: {e}")
                return {}
        time.sleep(1.2)
    return {}

def clean(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def fuzzy(a, b):
    return SequenceMatcher(None, a, b).ratio()

def best_match_event(events, home, away, start_ts):
    """Return (event, reversed_sides) or (None, False)."""
    best, best_score, best_rev = None, 0.0, False
    for ev in events:
        ev_ts = int(ev.get("time", 0))
        if abs(ev_ts - start_ts) > TIME_WINDOW_H * 3600:
            continue
        h = clean(ev.get("home", {}).get("name", ""))
        a = clean(ev.get("away", {}).get("name", ""))
        score_norm = fuzzy(home, h) + fuzzy(away, a)
        score_rev  = fuzzy(home, a) + fuzzy(away, h)
        if score_norm >= score_rev:
            score, rev = score_norm, False
        else:
            score, rev = score_rev, True
        if score > best_score and score >= FUZZY_MIN * 2:
            best_score = score
            best = ev
            best_rev = rev
    return best, best_rev

def extract_odds(odds_data, bet_side):
    """
    Вернуть (odds_float, bookmaker_name) для нашей стороны ставки.
    bet_side: 'home' или 'away'
    """
    results = odds_data.get("results", {})
    if not isinstance(results, dict):
        return None, None

    candidates = []
    for bm_name, bm_data in results.items():
        if not isinstance(bm_data, dict):
            continue
        for _market, mdata in bm_data.items():
            if not isinstance(mdata, dict):
                continue
            odds_list = mdata.get("odds", [])
            if not isinstance(odds_list, list) or len(odds_list) < 2:
                continue
            try:
                def to_float(x):
                    return float(x["odds"] if isinstance(x, dict) else x)
                o_home = to_float(odds_list[0])
                o_away = to_float(odds_list[1])
            except Exception:
                continue
            if o_home <= 1.0 or o_away <= 1.0:
                continue
            our = o_home if bet_side == "home" else o_away
            prio = next((i for i, p in enumerate(PREFERRED_BM) if p.lower() in bm_name.lower()), len(PREFERRED_BM))
            candidates.append((prio, our, bm_name))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    _, best_odds, best_bm = candidates[0]
    return round(best_odds, 4), best_bm

# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    if not BETSAPI_TOKEN:
        print("BETSAPI_TOKEN не задан — выход")
        return

    now_ts      = int(datetime.now(timezone.utc).timestamp())
    week_ago_ts = now_ts - 7 * 24 * 3600

    # Загрузить ставки за 7 дней
    print("Загружаю ставки AUTO_ELO_FLAT за 7 дней…")
    raw = sb("GET", (
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&select=id,home_team,away_team,start_time,bet_team,odds,outcome,settled,real_odds"
        "&order=start_time.desc&limit=1000"
    ))
    bets_in_window = [b for b in raw if (b.get("start_time") or 0) >= week_ago_ts]
    print(f"  строк в окне 7 дней: {len(bets_in_window)}")

    # Уникальные матчи
    seen_matches: dict = {}
    for b in bets_in_window:
        key = (clean(b["home_team"]), clean(b["away_team"]), b["start_time"])
        if key not in seen_matches:
            seen_matches[key] = b
    unique = list(seen_matches.values())
    print(f"  уникальных матчей: {len(unique)}")

    # Отфильтровать уже с реальными одсами
    need_update = [b for b in unique if not b.get("real_odds")]
    print(f"  нужно обновить: {len(need_update)}")
    if not need_update:
        print("Все матчи уже имеют real_odds — выход")
        return

    # Загрузить события из BetsAPI
    print("\nТяну события BetsAPI (ended + upcoming)…")
    all_events = []

    # ended — до 4 страниц
    for page in range(1, 5):
        data = bapi("/v3/events/ended", {"sport_id": 151, "page": page})
        evs = data.get("results", [])
        if not evs:
            break
        all_events.extend(evs)
        print(f"  ended page {page}: +{len(evs)} (итого {len(all_events)})")
        time.sleep(1.2)
        if len(evs) < 50:
            break

    # upcoming — 1 страница
    data = bapi("/v3/events/upcoming", {"sport_id": 151})
    upk  = data.get("results", [])
    all_events.extend(upk)
    print(f"  upcoming: +{len(upk)}")
    time.sleep(1.2)

    print(f"Всего BetsAPI событий: {len(all_events)}")

    # Проходим по матчам, ищем реальные одсы
    updated = 0
    for bet in need_update:
        home = clean(bet["home_team"])
        away = clean(bet["away_team"])
        st   = bet["start_time"]
        dt   = datetime.fromtimestamp(st, tz=timezone.utc).strftime("%d.%m %H:%M")

        ev, reversed_sides = best_match_event(all_events, home, away, st)
        if ev is None:
            print(f"  [не найден] {bet['home_team']} vs {bet['away_team']} {dt}")
            continue

        ev_id = ev.get("id")
        ev_home = ev.get("home", {}).get("name", "?")
        ev_away = ev.get("away", {}).get("name", "?")

        time.sleep(1.2)
        odds_data = bapi("/v2/event/odds/summary", {"event_id": ev_id})

        # Скорректировать сторону если имена перевёрнуты
        bet_side = bet["bet_team"]  # 'home' или 'away'
        if reversed_sides:
            bet_side = "away" if bet_side == "home" else "home"

        real_odds, real_bm = extract_odds(odds_data, bet_side)

        if real_odds:
            notional = round(float(bet.get("odds") or 0), 3)
            outcome  = bet.get("outcome") or "—"
            # P&L с реальными одсами
            stake = 20.0
            pnl_real = round(stake * (real_odds - 1), 2) if outcome == "win" else (-stake if outcome == "loss" else None)
            pnl_not  = round(stake * (notional - 1),  2) if outcome == "win" else (-stake if outcome == "loss" else None)

            print(
                f"  ✓ {bet['home_team']} vs {bet['away_team']} {dt} "
                f"[{bet['bet_team']}] "
                f"notional={notional} → real={real_odds} ({real_bm}) "
                f"| outcome={outcome} pnl_notional={pnl_not} pnl_real={pnl_real}"
            )

            # Обновить все строки этого матча
            url_filter = (
                f"elo_paper_bets"
                f"?strategy_name=eq.AUTO_ELO_FLAT"
                f"&home_team=eq.{bet['home_team']}"
                f"&away_team=eq.{bet['away_team']}"
                f"&start_time=eq.{st}"
            )
            sb("PATCH", url_filter, {"real_odds": real_odds, "real_bookmaker": real_bm})
            updated += 1
        else:
            print(f"  [нет одсов в BetsAPI] {bet['home_team']} vs {bet['away_team']} {dt} (event_id={ev_id})")

    print(f"\nГотово. Обновлено матчей: {updated}/{len(need_update)}")


if __name__ == "__main__":
    main()
