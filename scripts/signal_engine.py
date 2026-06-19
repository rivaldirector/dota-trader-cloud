#!/usr/bin/env python3
"""
Signal Engine v2 — ищет value на предстоящих матчах по всем дисциплинам.

Дисциплины: Dota2, CS2, LoL, Valorant, Rainbow Six (все esports sport_id=151)

Логика (v2 — улучшенная после бэктеста):
  1. Берём upcoming матчи через BetsAPI (все esports)
  2. Для каждого матча получаем odds/summary (все буки сразу)
  3. Pinnacle = sharp reference
  4. Фильтр движения Pinnacle: ставим только туда, куда двинулась Pin линия
     - HOME сигнал: Pin.open_home > Pin.close_home (шарпы снизили home = ставим home)
     - AWAY сигнал: Pin.open_away > Pin.close_away (шарпы снизили away = ставим away)
  5. Edge 12%+ (было 5%) — только реальные ошибки буков, без шума
  6. Только Bet365 + GGBet (самые точные и медленные к обновлению)
  7. Сохраняем в Supabase таблицу signals

Результат бэктеста v2 (май 2025 — июнь 2026):
  - 168 ставок (было 1697)
  - Win rate 57.7% (было 44.3%)
  - P&L: +54.4 unit (было -70.3 unit)

Run:
    python3 scripts/signal_engine.py           # все дисциплины
    SPORT_FILTER=dota2 python3 scripts/signal_engine.py  # только Dota2

GitHub Actions: каждые 2 часа автоматически.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_ANON_KEY", "")

if not all([BETSAPI_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Missing env vars. Check .env")
    sys.exit(1)

BETS_BASE = "https://api.b365api.com"
SPORT_ID  = 151  # esports

# Фильтр дисциплин по ключевым словам в названии лиги
DISCIPLINES: dict[str, list[str]] = {
    "dota2":    ["dota", "dota 2", "dota2"],
    "cs2":      ["cs2", "counter-strike", "csgo", "cs:go", "cs 2"],
    "lol":      ["league of legends", "lol", "league"],
    "valorant": ["valorant"],
    "r6":       ["rainbow six", "r6", "siege"],
}

# Если задан через env — только эта дисциплина
SPORT_FILTER = os.getenv("SPORT_FILTER", "all").lower()

# Порог edge v2: 12%+ (было 5%). Доказано бэктестом — ниже 12% шум, не сигнал.
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.12"))

# v2: Только Bet365 + GGBet — самые медленные к обновлению, дают реальный edge.
# YSB88, MelBet, FonBet, CashPoint, Mansion исключены — создавали ложные сигналы.
SOFT_BOOKS = {"GGBet", "Bet365"}

# v2: Требовать подтверждение от движения Pinnacle (True по умолчанию)
# Если False — старое поведение (ставим при любом edge, без фильтра направления)
REQUIRE_PIN_MOVE = os.getenv("REQUIRE_PIN_MOVE", "true").lower() != "false"

REQ_INTERVAL = 1.2  # секунды между BetsAPI запросами

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}


# ── BetsAPI ───────────────────────────────────────────────────────────────────

class BetsAPI:
    def __init__(self, token: str):
        self.token = token
        self._last = 0.0
        self.session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.time() - self._last
        if elapsed < REQ_INTERVAL:
            time.sleep(REQ_INTERVAL - elapsed)
        p = {"token": self.token, **(params or {})}
        for attempt in range(3):
            try:
                r = self.session.get(f"{BETS_BASE}{path}", params=p, timeout=20)
                self._last = time.time()
                if r.status_code == 429:
                    print("  [429] rate limit — sleep 60s")
                    time.sleep(60)
                    continue
                r.raise_for_status()
                data = r.json()
                if not data.get("success"):
                    raise RuntimeError(f"API error: {data.get('error', data)}")
                return data
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  [WARN] attempt {attempt+1}: {e}")
                time.sleep(5)
        raise RuntimeError("Max retries exceeded")


# ── Supabase ──────────────────────────────────────────────────────────────────

def sb_upsert(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SB_HEADERS,
        json=rows,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"  [SB ERROR] {table}: {r.status_code} {r.text[:200]}")

def sb_get(table: str, qs: str) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

def detect_sport(event: dict) -> str | None:
    """Return sport tag if event matches a known discipline, else None."""
    league = (event.get("league") or {}).get("name", "").lower()
    for sport, keywords in DISCIPLINES.items():
        if any(kw in league for kw in keywords):
            if SPORT_FILTER == "all" or SPORT_FILTER == sport:
                return sport
    return None

def parse_odds_summary(data: dict) -> dict[str, dict]:
    """Returns {bookmaker: {open_h, open_a, close_h, close_a}}"""
    result = {}
    for bm_name, bm_data in (data.get("results") or {}).items():
        odds  = bm_data.get("odds", {}) or {}
        start = odds.get("start") or {}
        end   = odds.get("end") or start

        def get_mk(mk_dict):
            return mk_dict.get("151_1") or mk_dict.get("1_1") or {}

        mk_s = get_mk(start)
        mk_e = get_mk(end) or mk_s

        oh = safe_float(mk_s.get("home_od") or mk_s.get("1"))
        oa = safe_float(mk_s.get("away_od") or mk_s.get("2"))
        ch = safe_float(mk_e.get("home_od") or mk_e.get("1")) or oh
        ca = safe_float(mk_e.get("away_od") or mk_e.get("2")) or oa

        if ch and ca and ch > 1.0 and ca > 1.0:
            result[bm_name] = {
                "open_h": oh, "open_a": oa,
                "close_h": ch, "close_a": ca,
            }
    return result


# ── Core logic ────────────────────────────────────────────────────────────────

def find_signals(event: dict, odds_by_bm: dict[str, dict], sport: str = "dota2") -> list[dict]:
    """
    Compare Pinnacle vs soft books, return list of signal dicts.

    v2 logic:
    - Edge threshold: 12%+ (env EDGE_THRESHOLD, default 0.12)
    - Only Bet365 + GGBet (SOFT_BOOKS)
    - Pinnacle move filter (REQUIRE_PIN_MOVE=true by default):
        HOME signal only when Pin LOWERED home odds (open_home > close_home)
        — means sharpies bet HOME, soft book is slow to react
        AWAY signal only when Pin LOWERED away odds (open_away > close_away)
    """
    signals = []

    pin = odds_by_bm.get("PinnacleSports")
    if not pin:
        return signals

    pin_h      = pin["close_h"]
    pin_a      = pin["close_a"]
    pin_open_h = pin["open_h"] or pin_h
    pin_open_a = pin["open_a"] or pin_a
    pin_move_h = pin_h - pin_open_h   # negative = Pin lowered home odds = sharpies on home
    pin_move_a = pin_a - pin_open_a   # negative = Pin lowered away odds = sharpies on away

    eid        = str(event.get("id", ""))
    league     = (event.get("league") or {}).get("name", "")
    home_team  = (event.get("home") or {}).get("name", "")
    away_team  = (event.get("away") or {}).get("name", "")
    start_time = event.get("time")

    for bm_name, bm in odds_by_bm.items():
        if bm_name not in SOFT_BOOKS:
            continue

        for side, bm_odds, pin_odds, pin_open, pin_move in [
            ("home", bm["close_h"], pin_h, pin_open_h, pin_move_h),
            ("away", bm["close_a"], pin_a, pin_open_a, pin_move_a),
        ]:
            if not bm_odds or not pin_odds:
                continue
            # Sanity check: ignore corrupted odds
            if not (1.01 <= bm_odds <= 15.0 and 1.01 <= pin_odds <= 15.0):
                continue

            edge = (bm_odds / pin_odds) - 1.0
            if not (EDGE_THRESHOLD <= edge <= 0.50):
                continue

            # v2: Pinnacle move filter — only bet in direction sharpies already moved
            # pin_move < 0 means Pinnacle LOWERED the odds = sharpies bet this side
            if REQUIRE_PIN_MOVE:
                if pin_open <= 1.01:
                    continue   # no open odds → can't confirm direction
                if pin_move >= 0:
                    continue   # Pin didn't move this direction → skip

            signals.append({
                "captured_at": now_iso(),
                "event_id":    eid,
                "league":      league,
                "home_team":   home_team,
                "away_team":   away_team,
                "start_time":  start_time,
                "bet_side":    side,
                "bookmaker":   bm_name,
                "bm_odds":     round(bm_odds, 3),
                "pin_odds":    round(pin_odds, 3),
                "pin_open":    round(pin_open, 3) if pin_open else None,
                "edge_pct":    round(edge * 100, 2),
                "pin_move":    round(pin_move, 3) if pin_move else None,
                "sport":       sport,
            })

    return signals


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    api = BetsAPI(BETSAPI_TOKEN)

    print("=" * 60)
    print(f"Signal Engine — {now_iso()}")
    print(f"Edge threshold: {EDGE_THRESHOLD*100:.0f}%  |  Sport filter: {SPORT_FILTER}")
    print("=" * 60)

    # 1. Get ALL upcoming esports events (one pass, all pages)
    all_events: list[tuple[dict, str]] = []  # (event, sport_tag)
    page = 1
    while True:
        data  = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
        items = data.get("results", [])
        if not items:
            break
        total = data.get("pager", {}).get("total", 0)
        for e in items:
            sport = detect_sport(e)
            if sport:
                all_events.append((e, sport))
        if page * 50 >= total or page >= 200:
            break
        page += 1

    # Count by discipline
    from collections import Counter
    counts = Counter(sport for _, sport in all_events)
    print(f"Found {len(all_events)} upcoming events: " +
          ", ".join(f"{s}={n}" for s, n in sorted(counts.items())))

    # 2. Fetch odds + find signals
    all_signals = []
    for i, (event, sport) in enumerate(all_events):
        eid  = str(event.get("id", ""))
        home = (event.get("home") or {}).get("name", "?")
        away = (event.get("away") or {}).get("name", "?")

        try:
            data       = api.get("/v2/event/odds/summary", {"event_id": eid})
            odds_by_bm = parse_odds_summary(data)
            signals    = find_signals(event, odds_by_bm, sport)
            all_signals.extend(signals)

            bm_count = len(odds_by_bm)
            sig_mark = f" ⚡ {len(signals)} signal(s)!" if signals else ""
            print(f"  [{i+1}/{len(all_events)}] [{sport}] {home} vs {away}: {bm_count} books{sig_mark}")

        except Exception as ex:
            print(f"  [WARN] {home} vs {away}: {ex}")

    # 3. Save signals
    print(f"\n{'='*60}")
    print(f"Total signals found: {len(all_signals)}")

    if all_signals:
        sb_upsert("signals", all_signals)
        print("\nSignals:")
        for s in all_signals:
            sport_label = s.get('sport', '').upper()
            print(f"  [{sport_label}] {s['home_team']} vs {s['away_team']}  ({s['league']})")
            print(f"    BET {s['bet_side'].upper()} @ {s['bookmaker']}: {s['bm_odds']} (Pin: {s['pin_odds']}, edge: +{s['edge_pct']}%)")
            if s.get('pin_move'):
                print(f"    Pin move: {s['pin_move']:+.3f}")
    else:
        print("No signals above threshold — market is efficient right now.")

    print(f"\nFinished: {now_iso()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
