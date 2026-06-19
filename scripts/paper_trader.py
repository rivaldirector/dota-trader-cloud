#!/usr/bin/env python3
"""
Paper Trader — эмулирует ставки с виртуальным банком $1,000.

Запускается вместе с signal_engine — находит сигналы и записывает
виртуальные ставки в Supabase. После завершения матчей автоматически
считает результат и обновляет банк.

Run:
    python3 scripts/paper_trader.py            # найти сигналы + записать ставки
    python3 scripts/paper_trader.py settle     # засчитать результаты
    python3 scripts/paper_trader.py status     # показать текущий P&L
"""
from __future__ import annotations

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

BETS_BASE = "https://api.b365api.com"
SPORT_ID  = 151

EDGE_THRESHOLD   = 0.12        # v2: 12%+ (было 5%)
STAKE_PCT        = 0.03        # 3% банка на ставку
MAX_STAKE        = 30.0        # v2: hard cap $30 независимо от банка
SOFT_BOOKS       = {"GGBet", "Bet365"}  # v2: только топ-2 бука
REQUIRE_PIN_MOVE = True        # v2: ставить только по направлению Pinnacle

DISCIPLINES = {
    "dota2":    ["dota", "dota 2", "dota2"],
    "cs2":      ["cs2", "counter-strike", "csgo", "cs:go", "cs 2"],
    "lol":      ["league of legends", "lol"],
    "valorant": ["valorant"],
}

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def sf(v):
    try: return float(v) if v not in (None,"") else None
    except: return None

# ── Supabase ──────────────────────────────────────────────────────────────────

def sb_get(table, qs=""):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def sb_post(table, rows):
    if not rows: return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SB_HEADERS, json=rows, timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"  [SB ERR] {table}: {r.status_code} {r.text[:150]}")

def sb_patch(table, qs, data):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers=SB_HEADERS, json=data, timeout=15,
    )
    if r.status_code not in (200, 204):
        print(f"  [SB ERR] PATCH {table}: {r.status_code} {r.text[:150]}")

def get_bank() -> float:
    rows = sb_get("paper_bankroll", "order=recorded_at.desc&limit=1")
    return float(rows[0]["balance_usd"]) if rows else 1000.0

def save_bank(balance: float, note: str = ""):
    sb_post("paper_bankroll", [{"balance_usd": balance, "note": note, "recorded_at": now_iso()}])

# ── BetsAPI ───────────────────────────────────────────────────────────────────

class API:
    def __init__(self):
        self._last = 0.0
        self.s = requests.Session()
    def get(self, path, params=None):
        elapsed = time.time() - self._last
        if elapsed < 1.2: time.sleep(1.2 - elapsed)
        p = {"token": BETSAPI_TOKEN, **(params or {})}
        r = self.s.get(f"{BETS_BASE}{path}", params=p, timeout=20)
        self._last = time.time()
        r.raise_for_status()
        d = r.json()
        return d if d.get("success") else {}

def detect_sport(league_name: str) -> str | None:
    ln = league_name.lower()
    for sport, kws in DISCIPLINES.items():
        if any(k in ln for k in kws):
            return sport
    return None

def parse_odds(data: dict) -> dict[str, dict]:
    result = {}
    for bm, bd in (data.get("results") or {}).items():
        odds = bd.get("odds") or {}
        s = odds.get("start") or {}
        e = odds.get("end") or s
        def mk(d): return d.get("151_1") or d.get("1_1") or {}
        ms, me = mk(s), mk(e) or mk(s)
        oh = sf(ms.get("home_od") or ms.get("1"))
        oa = sf(ms.get("away_od") or ms.get("2"))
        ch = sf(me.get("home_od") or me.get("1")) or oh
        ca = sf(me.get("away_od") or me.get("2")) or oa
        if ch and ca and ch > 1.0 and ca > 1.0:
            result[bm] = {"open_h": oh, "open_a": oa, "close_h": ch, "close_a": ca}
    return result

# ── Find signals ──────────────────────────────────────────────────────────────

def find_and_place(api: API):
    bank = get_bank()
    print(f"Виртуальный банк: ${bank:,.2f}")

    # Get upcoming events
    events = []
    page = 1
    while True:
        d = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
        items = d.get("results", [])
        if not items: break
        total = d.get("pager", {}).get("total", 0)
        for e in items:
            sport = detect_sport((e.get("league") or {}).get("name", ""))
            if sport:
                events.append((e, sport))
        if page * 50 >= total or page >= 100: break
        page += 1

    print(f"Найдено {len(events)} предстоящих матчей")

    new_bets = []
    for event, sport in events:
        eid  = str(event.get("id", ""))
        home = (event.get("home") or {}).get("name", "?")
        away = (event.get("away") or {}).get("name", "?")
        league = (event.get("league") or {}).get("name", "")

        # Skip if already have a trade for this event
        existing = sb_get("paper_trades", f"event_id=eq.{eid}&limit=1")
        if existing:
            continue

        d = api.get("/v2/event/odds/summary", {"event_id": eid})
        odds = parse_odds(d)

        pin = odds.get("PinnacleSports")
        if not pin: continue

        pin_h, pin_a = pin["close_h"], pin["close_a"]
        pin_open_h = pin["open_h"] or pin_h
        pin_open_a = pin["open_a"] or pin_a
        pm_h = pin_h - pin_open_h   # < 0 = Pin снизил home = шарпы на home
        pm_a = pin_a - pin_open_a   # < 0 = Pin снизил away = шарпы на away

        for bm_name, bm in odds.items():
            if bm_name not in SOFT_BOOKS: continue
            for side, bm_odds, pin_odds, pin_open, pm in [
                ("home", bm["close_h"], pin_h, pin_open_h, pm_h),
                ("away", bm["close_a"], pin_a, pin_open_a, pm_a),
            ]:
                if not bm_odds or not pin_odds: continue
                if not (1.01 <= bm_odds <= 15.0 and 1.01 <= pin_odds <= 15.0): continue
                edge = (bm_odds / pin_odds) - 1.0
                if not (EDGE_THRESHOLD <= edge <= 0.50): continue

                # v2: Pin move filter — ставим только по направлению шарпов
                if REQUIRE_PIN_MOVE:
                    if pin_open <= 1.01 or pm >= 0:
                        continue   # Pin не двигался в нужную сторону — пропускаем

                stake = round(min(bank * STAKE_PCT, MAX_STAKE), 2)  # v2: hard cap

                bet = {
                    "placed_at":  now_iso(),
                    "event_id":   eid,
                    "sport":      sport,
                    "league":     league,
                    "home_team":  home,
                    "away_team":  away,
                    "start_time": event.get("time"),
                    "bet_side":   side,
                    "bookmaker":  bm_name,
                    "bm_odds":    round(bm_odds, 3),
                    "pin_odds":   round(pin_odds, 3),
                    "pin_move":   round(pm, 3) if pm else None,
                    "edge_pct":   round(edge * 100, 2),
                    "stake_usd":  stake,
                    "bank_before": bank,
                }
                new_bets.append(bet)
                print(f"\n  ⚡ [{sport.upper()}] {home} vs {away}")
                print(f"     BET {side.upper()} @ {bm_name}: {bm_odds:.2f} | Pin: {pin_odds:.2f} | Edge: +{edge*100:.1f}%")
                print(f"     Ставка: ${stake:.2f} из банка ${bank:,.2f}")

    if new_bets:
        sb_post("paper_trades", new_bets)
        print(f"\nЗаписано {len(new_bets)} виртуальных ставок")
    else:
        print("Новых сигналов нет")

# ── Settle results ────────────────────────────────────────────────────────────

def settle(api: API):
    """Check finished matches and settle paper trades."""
    print("Проверяю результаты незакрытых ставок...")

    open_trades = sb_get("paper_trades", "result=is.null&order=placed_at.asc")
    if not open_trades:
        print("Нет открытых ставок")
        return

    print(f"Открытых ставок: {len(open_trades)}")
    bank = get_bank()
    settled = 0

    for trade in open_trades:
        eid = trade["event_id"]
        # Check if event is finished in betsapi_events
        events = sb_get("betsapi_events", f"event_id=eq.{eid}&select=winner,home_team,away_team,status")
        if not events: continue
        ev = events[0]
        if ev.get("status") != "ended" or not ev.get("winner"):
            continue

        winner = ev["winner"]
        home   = ev.get("home_team", "")
        side   = trade["bet_side"]

        # Determine win/loss
        if side == "home":
            won = (winner == home or winner == "1")
        else:
            won = (winner != home and winner != "" and winner != "1") or winner == "2"

        odds   = trade["bm_odds"]
        stake  = trade["stake_usd"]
        profit = round(stake * (odds - 1), 2) if won else round(-stake, 2)
        bank   = round(bank + profit, 2)
        result = "win" if won else "loss"

        sb_patch("paper_trades", f"id=eq.{trade['id']}", {
            "result":     result,
            "profit_usd": profit,
            "bank_after": bank,
            "settled_at": now_iso(),
        })

        mark = "✅ WIN" if won else "❌ LOSS"
        print(f"  {mark} {trade['home_team']} vs {trade['away_team']} | "
              f"bet {side} @ {odds:.2f} | P&L: ${profit:+.2f} | Bank: ${bank:,.2f}")
        settled += 1

    if settled:
        save_bank(bank, f"After settling {settled} trades")
        print(f"\nЗакрыто ставок: {settled} | Новый банк: ${bank:,.2f}")
    else:
        print("Матчи ещё не завершены")

# ── Status ────────────────────────────────────────────────────────────────────

def status():
    bank = get_bank()
    trades = sb_get("paper_trades", "order=placed_at.desc&limit=100")
    settled = [t for t in trades if t.get("result")]
    wins  = [t for t in settled if t["result"] == "win"]
    total_profit = sum(t.get("profit_usd", 0) for t in settled)
    roi = (total_profit / sum(t["stake_usd"] for t in settled) * 100) if settled else 0

    print("=" * 50)
    print(f"PAPER TRADING STATUS — {now_iso()}")
    print("=" * 50)
    print(f"Виртуальный банк:  ${bank:,.2f}")
    print(f"Старт:             $1,000.00")
    print(f"P&L:               ${bank-1000:+,.2f} ({(bank/1000-1)*100:+.1f}%)")
    print(f"Ставок всего:      {len(trades)}")
    print(f"Закрытых:          {len(settled)}")
    print(f"Побед:             {len(wins)} ({len(wins)/len(settled)*100:.0f}%)" if settled else "Побед: 0")
    print(f"ROI per bet:       {roi:.1f}%")
    print()
    print("Последние ставки:")
    for t in trades[:10]:
        res = t.get("result", "⏳ pending").upper()
        pnl = f"${t['profit_usd']:+.2f}" if t.get("profit_usd") is not None else ""
        print(f"  [{t.get('sport','?').upper():8}] {t['home_team'][:15]} vs {t['away_team'][:15]} "
              f"| {t['bet_side']} @ {t['bm_odds']:.2f} | {res} {pnl}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "trade"
    api  = API()

    print("=" * 50)
    print(f"Paper Trader — {now_iso()}")
    print("=" * 50)

    if mode == "settle":
        settle(api)
    elif mode == "status":
        status()
    else:
        find_and_place(api)
        settle(api)   # also try to settle any open trades
        status()

if __name__ == "__main__":
    main()
