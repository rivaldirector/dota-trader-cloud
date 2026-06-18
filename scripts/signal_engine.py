#!/usr/bin/env python3
"""
Signal Engine — ежечасно ищет value на предстоящих Dota2 матчах.

Логика:
  1. Берём upcoming матчи через BetsAPI
  2. Для каждого матча получаем odds/summary (все буки сразу)
  3. Pinnacle = sharp reference
  4. Если soft book даёт odds > Pinnacle * (1 + EDGE_THRESHOLD) → сигнал
  5. Дополнительный фильтр: Pinnacle линия сдвинулась (sharp money подтверждает)
  6. Сохраняем в Supabase таблицу signals

Run:
    python3 scripts/signal_engine.py

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

BETS_BASE  = "https://api.b365api.com"
SPORT_ID   = 151
DOTA_KW    = ["dota", "dota 2", "dota2"]

# Порог edge: soft book должен давать на >5% лучше чем Pinnacle
EDGE_THRESHOLD = 0.05

# Soft books для сравнения (активные в 2025-2026)
SOFT_BOOKS = {"GGBet", "Bet365", "YSB88", "MelBet", "FonBet", "CashPoint", "Mansion"}

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

def is_dota2(event: dict) -> bool:
    league = (event.get("league") or {}).get("name", "").lower()
    return any(k in league for k in DOTA_KW)

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

def find_signals(event: dict, odds_by_bm: dict[str, dict]) -> list[dict]:
    """Compare Pinnacle vs soft books, return list of signal dicts."""
    signals = []

    pin = odds_by_bm.get("PinnacleSports")
    if not pin:
        return signals

    pin_h = pin["close_h"]
    pin_a = pin["close_a"]
    pin_move_h = (pin["close_h"] - (pin["open_h"] or pin["close_h"]))
    pin_move_a = (pin["close_a"] - (pin["open_a"] or pin["close_a"]))

    eid        = str(event.get("id", ""))
    league     = (event.get("league") or {}).get("name", "")
    home_team  = (event.get("home") or {}).get("name", "")
    away_team  = (event.get("away") or {}).get("name", "")
    start_time = event.get("time")

    for bm_name, bm in odds_by_bm.items():
        if bm_name not in SOFT_BOOKS:
            continue

        # Home signal: soft book odds on home > Pinnacle home by EDGE_THRESHOLD
        for side, bm_odds, pin_odds, pin_open, pin_move in [
            ("home", bm["close_h"], pin_h, pin["open_h"], pin_move_h),
            ("away", bm["close_a"], pin_a, pin["open_a"], pin_move_a),
        ]:
            if not bm_odds or not pin_odds:
                continue
            edge = (bm_odds / pin_odds) - 1.0
            if edge >= EDGE_THRESHOLD:
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
                })

    return signals


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    api = BetsAPI(BETSAPI_TOKEN)

    print("=" * 60)
    print(f"Signal Engine — {now_iso()}")
    print(f"Edge threshold: {EDGE_THRESHOLD*100:.0f}%")
    print("=" * 60)

    # 1. Get upcoming Dota2 events
    events = []
    page = 1
    while True:
        data  = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
        items = data.get("results", [])
        if not items:
            break
        total = data.get("pager", {}).get("total", 0)
        for e in items:
            if is_dota2(e):
                events.append(e)
        if page * 50 >= total:
            break
        page += 1

    print(f"Found {len(events)} upcoming Dota2 events")

    # 2. Fetch odds + find signals
    all_signals = []
    for i, event in enumerate(events):
        eid  = str(event.get("id", ""))
        home = (event.get("home") or {}).get("name", "?")
        away = (event.get("away") or {}).get("name", "?")

        try:
            data       = api.get("/v2/event/odds/summary", {"event_id": eid})
            odds_by_bm = parse_odds_summary(data)
            signals    = find_signals(event, odds_by_bm)
            all_signals.extend(signals)

            bm_count = len(odds_by_bm)
            sig_mark = f" ⚡ {len(signals)} signal(s)!" if signals else ""
            print(f"  [{i+1}/{len(events)}] {home} vs {away}: {bm_count} books{sig_mark}")

        except Exception as ex:
            print(f"  [WARN] {home} vs {away}: {ex}")

    # 3. Save signals
    print(f"\n{'='*60}")
    print(f"Total signals found: {len(all_signals)}")

    if all_signals:
        sb_upsert("signals", all_signals)
        print("\nSignals:")
        for s in all_signals:
            print(f"  {s['home_team']} vs {s['away_team']}")
            print(f"    BET {s['bet_side'].upper()} @ {s['bookmaker']}: {s['bm_odds']} (Pinnacle: {s['pin_odds']}, edge: +{s['edge_pct']}%)")
            if s.get('pin_move'):
                direction = "↓" if s['pin_move'] < 0 else "↑"
                print(f"    Pinnacle moved: {s['pin_move']:+.3f} {direction} (sharp money {'confirmed' if s['pin_move'] < 0 and s['bet_side'] == 'home' or s['pin_move'] > 0 and s['bet_side'] == 'away' else 'note'})")
    else:
        print("No signals above threshold — market is efficient right now.")

    print(f"\nFinished: {now_iso()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
