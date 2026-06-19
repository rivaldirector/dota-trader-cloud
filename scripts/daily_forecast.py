#!/usr/bin/env python3
"""
Daily Forecast — генерирует сигналы на сегодня и сразу считает стейк в баксах
по quarter-Kelly модели от текущего виртуального банка.

Логика поиска сигналов = signal_engine.py v2 (57.7% winrate, 168 ставок бэктест):
  - Pinnacle = sharp reference
  - Только Bet365 + GGBet
  - Edge >= 12%
  - Ставим только в сторону, куда уже двинулась линия Pinnacle

Результат: строки в Supabase.daily_signals (result='pending') + чистый
человекочитаемый список "ставим $X на ... сегодня".

Run:
    python3 scripts/daily_forecast.py

GitHub Actions / scheduled task: каждое утро.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))
from daily_pipeline_lib import (
    SB_HEADERS, SUPABASE_URL, SUPABASE_KEY, EDGE_THRESHOLD, SOFT_BOOKS,
    now_iso, sb_insert, get_bank, kelly_stake, fmt_usd,
    check_betsapi_alive, sb_set_pipeline_status,
)

BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")
BETS_BASE = "https://api.b365api.com"
SPORT_ID = 151
REQ_INTERVAL = 1.2

DISCIPLINES = {
    "dota2":    ["dota", "dota 2", "dota2"],
    "cs2":      ["cs2", "counter-strike", "csgo", "cs:go", "cs 2"],
    "lol":      ["league of legends", "lol", "league"],
    "valorant": ["valorant"],
    "r6":       ["rainbow six", "r6", "siege"],
}


class BetsAPI:
    def __init__(self, token):
        self.token = token
        self._last = 0.0
        self.session = requests.Session()

    def get(self, path, params=None):
        elapsed = time.time() - self._last
        if elapsed < REQ_INTERVAL:
            time.sleep(REQ_INTERVAL - elapsed)
        p = {"token": self.token, **(params or {})}
        for attempt in range(3):
            try:
                r = self.session.get(f"{BETS_BASE}{path}", params=p, timeout=20)
                self._last = time.time()
                if r.status_code == 429:
                    time.sleep(60)
                    continue
                r.raise_for_status()
                data = r.json()
                if not data.get("success"):
                    raise RuntimeError(str(data))
                return data
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5)
        raise RuntimeError("max retries")


def safe_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def detect_sport(event):
    league = (event.get("league") or {}).get("name", "").lower()
    for sport, kws in DISCIPLINES.items():
        if any(k in league for k in kws):
            return sport
    return None


def parse_odds_summary(data):
    result = {}
    for bm_name, bm_data in (data.get("results") or {}).items():
        odds = bm_data.get("odds", {}) or {}
        start = odds.get("start") or {}
        end = odds.get("end") or start
        def get_mk(d): return d.get("151_1") or d.get("1_1") or {}
        mk_s, mk_e = get_mk(start), get_mk(end) or get_mk(start)
        oh = safe_float(mk_s.get("home_od") or mk_s.get("1"))
        oa = safe_float(mk_s.get("away_od") or mk_s.get("2"))
        ch = safe_float(mk_e.get("home_od") or mk_e.get("1")) or oh
        ca = safe_float(mk_e.get("away_od") or mk_e.get("2")) or oa
        if ch and ca and ch > 1.0 and ca > 1.0:
            result[bm_name] = {"open_h": oh, "open_a": oa, "close_h": ch, "close_a": ca}
    return result


def find_signals(event, odds_by_bm, sport):
    signals = []
    pin = odds_by_bm.get("PinnacleSports")
    if not pin:
        return signals
    pin_h, pin_a = pin["close_h"], pin["close_a"]
    pin_open_h, pin_open_a = pin["open_h"] or pin_h, pin["open_a"] or pin_a
    pin_move_h, pin_move_a = pin_h - pin_open_h, pin_a - pin_open_a

    eid = str(event.get("id", ""))
    league = (event.get("league") or {}).get("name", "")
    home = (event.get("home") or {}).get("name", "")
    away = (event.get("away") or {}).get("name", "")
    start_ts = event.get("time")
    start_iso = (datetime.fromtimestamp(int(start_ts), tz=timezone.utc).isoformat()
                 if start_ts else None)

    for bm_name, bm in odds_by_bm.items():
        if bm_name not in SOFT_BOOKS:
            continue
        for side, bm_odds, pin_odds, pin_open, pin_move in [
            ("home", bm["close_h"], pin_h, pin_open_h, pin_move_h),
            ("away", bm["close_a"], pin_a, pin_open_a, pin_move_a),
        ]:
            if not bm_odds or not pin_odds:
                continue
            if not (1.01 <= bm_odds <= 15.0 and 1.01 <= pin_odds <= 15.0):
                continue
            edge = (bm_odds / pin_odds) - 1.0
            if not (EDGE_THRESHOLD <= edge <= 0.50):
                continue
            if pin_open <= 1.01 or pin_move >= 0:
                continue
            signals.append({
                "signal_date": datetime.now(timezone.utc).date().isoformat(),
                "event_id": eid, "sport": sport, "league": league,
                "home_team": home, "away_team": away, "start_time": start_iso,
                "bet_side": side, "bookmaker": bm_name,
                "bm_odds": round(bm_odds, 3), "pin_odds": round(pin_odds, 3),
                "pin_open": round(pin_open, 3), "pin_move": round(pin_move, 3),
                "edge_pct": round(edge * 100, 2),
            })
    return signals


def main():
    if not all([BETSAPI_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing env vars"); sys.exit(1)

    alive, why = check_betsapi_alive(BETSAPI_TOKEN)
    if not alive:
        print(f"ERROR: BetsAPI недоступен ({why}) — форкаст на сегодня пропущен.")
        sb_set_pipeline_status("daily_forecast", False, f"BetsAPI недоступен: {why}")
        sys.exit(1)

    api = BetsAPI(BETSAPI_TOKEN)
    bank = get_bank()
    bank_usd = float(bank["current_bank_usd"])

    print("=" * 60)
    print(f"DAILY FORECAST — {now_iso()}  |  bank: ${bank_usd:,.2f}")
    print("=" * 60)

    all_events = []
    page = 1
    try:
        while True:
            data = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
            items = data.get("results", [])
            if not items:
                break
            for e in items:
                sport = detect_sport(e)
                if sport:
                    all_events.append((e, sport))
            total = data.get("pager", {}).get("total", 0)
            if page * 50 >= total or page >= 200:
                break
            page += 1
    except Exception as ex:
        print(f"ERROR: BetsAPI отвалился во время сканирования событий: {ex}")
        sb_set_pipeline_status("daily_forecast", False, f"упал во время сканирования: {ex}")
        sys.exit(1)

    print(f"Upcoming events scanned: {len(all_events)}")

    found = []
    for event, sport in all_events:
        eid = str(event.get("id", ""))
        try:
            data = api.get("/v2/event/odds/summary", {"event_id": eid})
            odds_by_bm = parse_odds_summary(data)
            found.extend(find_signals(event, odds_by_bm, sport))
        except Exception as ex:
            print(f"  [WARN] {eid}: {ex}")

    if not found:
        print("\nСигналов нет — рынок эффективен сегодня. Это нормально:")
        print("при этом фильтре (edge>=12%, pin-move confirm) исторически ~2-3 сигнала/неделю.")
        sb_set_pipeline_status("daily_forecast", True, f"отработал, сигналов 0 (events: {len(all_events)})")
        return

    rows = []
    print(f"\n{len(found)} сигнал(ов) на сегодня:\n")
    for s in found:
        stake = kelly_stake(s["edge_pct"], s["bm_odds"], bank_usd)
        s["kelly_frac"] = 0.25
        s["stake_usd"] = stake
        s["bank_before_usd"] = round(bank_usd, 2)
        s["result"] = "pending"
        rows.append(s)
        pick = s["home_team"] if s["bet_side"] == "home" else s["away_team"]
        opp  = s["away_team"] if s["bet_side"] == "home" else s["home_team"]
        print(f"  СТАВИМ ${stake:,.2f} на {pick} (vs {opp})")
        print(f"    {s['league']} | @{s['bookmaker']} кф {s['bm_odds']} | "
              f"Pin {s['pin_odds']} | edge +{s['edge_pct']}% | старт {s['start_time']}")

    sb_insert("daily_signals", rows)
    sb_set_pipeline_status("daily_forecast", True, f"записано {len(rows)} сигналов (events: {len(all_events)})")
    print(f"\nЗаписано {len(rows)} сигналов в daily_signals. Банк не меняется до расчёта (settle).")


if __name__ == "__main__":
    main()
