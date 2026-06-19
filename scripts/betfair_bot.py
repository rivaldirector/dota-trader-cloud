#!/usr/bin/env python3
"""
Betfair Bot — автоматически ставит деньги на Betfair Exchange.

Логика:
  1. Берём текущие Pinnacle котировки из BetsAPI (sharp reference)
  2. Ищем те же матчи на Betfair Exchange
  3. Если Betfair back price > Pinnacle × (1 + EDGE_THRESHOLD) → ставим
  4. Размер ставки = Kelly / 4 (quarter Kelly для контроля риска)
  5. Логируем в Supabase таблицу betfair_bets

Run:
    python3 scripts/betfair_bot.py           # dry_run по умолчанию
    DRY_RUN=false python3 scripts/betfair_bot.py   # реальные ставки

GitHub Actions: каждые 2 часа.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "scripts"))

from betfair_client import BetfairClient, BetfairError, parse_market, MIN_AVAILABLE

# ── Config ────────────────────────────────────────────────────────────────────

BETSAPI_TOKEN  = os.getenv("BETSAPI_TOKEN", "")
SUPABASE_URL   = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY   = os.getenv("SUPABASE_ANON_KEY", "")

EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.05"))   # 5%
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))   # quarter Kelly
MAX_STAKE      = float(os.getenv("MAX_STAKE_GBP", "50"))      # hard cap per bet
MIN_STAKE      = 2.0                                           # Betfair minimum
DRY_RUN        = os.getenv("DRY_RUN", "true").lower() != "false"
HOURS_AHEAD    = int(os.getenv("HOURS_AHEAD", "12"))          # scan next N hours

BETS_BASE      = "https://api.b365api.com"
SPORT_ID       = 151  # esports

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}

# ── Supabase ──────────────────────────────────────────────────────────────────

def sb_insert(table: str, rows: list[dict]) -> None:
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

def sb_get_balance() -> float:
    """Get latest bankroll from betfair_bets table."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/betfair_bankroll?order=recorded_at.desc&limit=1",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=10,
    )
    if r.status_code == 200 and r.json():
        return float(r.json()[0].get("balance_gbp", 0))
    return 0.0

# ── BetsAPI — Pinnacle reference ──────────────────────────────────────────────

class BetsAPI:
    def __init__(self, token: str):
        self.token = token
        self._last = 0.0
        self.s = requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.time() - self._last
        if elapsed < 1.2:
            time.sleep(1.2 - elapsed)
        p = {"token": self.token, **(params or {})}
        r = self.s.get(f"{BETS_BASE}{path}", params=p, timeout=20)
        self._last = time.time()
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return {}
        return data


def get_pinnacle_odds(api: BetsAPI) -> dict[str, dict]:
    """
    Fetch upcoming esports events from BetsAPI and return Pinnacle odds.
    Returns: { event_id: { home: str, away: str, pin_h: float, pin_a: float, league: str } }
    """
    result = {}
    page = 1
    while True:
        data  = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
        items = data.get("results", [])
        if not items:
            break
        total = data.get("pager", {}).get("total", 0)

        for e in items:
            eid = str(e.get("id", ""))
            odds_data = api.get("/v2/event/odds/summary", {"event_id": eid})
            pin = (odds_data.get("results") or {}).get("PinnacleSports")
            if not pin:
                continue
            odds = pin.get("odds", {})
            end  = odds.get("end") or odds.get("start") or {}
            mk   = end.get("151_1") or end.get("1_1") or {}
            ph = _sf(mk.get("home_od") or mk.get("1"))
            pa = _sf(mk.get("away_od") or mk.get("2"))
            if ph and pa and ph > 1.0 and pa > 1.0:
                result[eid] = {
                    "event_id":  eid,
                    "league":    (e.get("league") or {}).get("name", ""),
                    "home":      (e.get("home") or {}).get("name", ""),
                    "away":      (e.get("away") or {}).get("name", ""),
                    "pin_h":     ph,
                    "pin_a":     pa,
                    "start_ts":  e.get("time", 0),
                }

        if page * 50 >= total or page >= 100:
            break
        page += 1

    return result


def _sf(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── Match Betfair markets to Pinnacle events ──────────────────────────────────

_JUNK = re.compile(r"\b(esports?|gaming|team|club|fc|org)\b", re.I)

def _norm(name: str) -> str:
    """Normalise team name for fuzzy matching."""
    return _JUNK.sub("", name).lower().strip()


def match_runners(pin_event: dict, bf_market: dict) -> list[dict] | None:
    """
    Try to match Pinnacle home/away to Betfair runners by name similarity.
    Returns list of { side, selection_id, bf_price, pin_price, edge } or None.
    """
    ph, pa = pin_event["pin_h"], pin_event["pin_a"]
    hn = _norm(pin_event["home"])
    an = _norm(pin_event["away"])

    matched = {}
    for r in bf_market["runners"]:
        rn = _norm(r["name"])
        if hn and (hn in rn or rn in hn):
            matched["home"] = r
        elif an and (an in rn or rn in an):
            matched["away"] = r

    if len(matched) < 2:
        return None

    signals = []
    for side, pin_odds in [("home", ph), ("away", pa)]:
        r    = matched[side]
        bp   = r["back_price"]
        avail = r["available"]
        if bp <= 1.0 or avail < MIN_AVAILABLE:
            continue
        edge = (bp / pin_odds) - 1.0
        if EDGE_THRESHOLD <= edge <= 0.50:
            signals.append({
                "side":         side,
                "selection_id": r["selection_id"],
                "runner_name":  r["name"],
                "bf_price":     bp,
                "pin_price":    pin_odds,
                "edge":         edge,
                "available":    avail,
            })
    return signals or None


# ── Kelly stake calculation ───────────────────────────────────────────────────

def kelly_stake(bankroll: float, odds: float, edge: float) -> float:
    """
    Quarter-Kelly stake.
    f* = edge / (odds - 1)  [full Kelly fraction of bankroll]
    stake = f* × KELLY_FRACTION × bankroll
    """
    b = odds - 1.0
    if b <= 0:
        return MIN_STAKE
    f_star = edge / b
    stake  = f_star * KELLY_FRACTION * bankroll
    stake  = max(MIN_STAKE, min(MAX_STAKE, round(stake, 2)))
    return stake


# ── Main ──────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    print("=" * 60)
    print(f"Betfair Bot — {now_iso()}")
    print(f"DRY RUN: {DRY_RUN}  |  Edge: {EDGE_THRESHOLD*100:.0f}%  |  Kelly: {KELLY_FRACTION}")
    print("=" * 60)

    if DRY_RUN:
        print("  [DRY RUN] Ставки не будут размещены. Передай DRY_RUN=false для реальных ставок.\n")

    # ── 1. Get Pinnacle odds via BetsAPI
    bapi = BetsAPI(BETSAPI_TOKEN)
    print("[1] Загружаю Pinnacle котировки...")
    pin_events = get_pinnacle_odds(bapi)
    print(f"    {len(pin_events)} событий с Pinnacle линией")

    if not pin_events:
        print("Нет данных от Pinnacle. Выход.")
        return

    # ── 2. Get Betfair markets
    print("[2] Ищу рынки на Betfair...")
    with BetfairClient() as bf:
        balance = bf.get_balance()
        print(f"    Баланс: £{balance:.2f}")

        if balance < MIN_STAKE and not DRY_RUN:
            print("    Недостаточно средств на Betfair. Выход.")
            return

        catalogues = bf.list_esports_markets(hours_ahead=HOURS_AHEAD)
        print(f"    {len(catalogues)} esports рынков на Betfair")

        if not catalogues:
            print("Нет рынков на Betfair. Выход.")
            return

        # Fetch books in batches of 50
        market_ids = [c["marketId"] for c in catalogues]
        books = []
        for i in range(0, len(market_ids), 50):
            books.extend(bf.get_market_book(market_ids[i:i+50]))

        book_map = {b["marketId"]: b for b in books}

        # Parse markets
        bf_markets = []
        for cat in catalogues:
            book = book_map.get(cat["marketId"])
            if not book:
                continue
            parsed = parse_market(cat, book)
            if parsed:
                bf_markets.append(parsed)

        print(f"    {len(bf_markets)} открытых рынков с котировками")

        # ── 3. Find signals
        print("[3] Ищу сигналы (Betfair vs Pinnacle)...")
        placed_bets = []

        for bf_mkt in bf_markets:
            # Try to match this Betfair market to a Pinnacle event
            best_match = None
            best_score = 0

            event_name = bf_mkt["event_name"].lower()
            for pin in pin_events.values():
                hn = _norm(pin["home"])
                an = _norm(pin["away"])
                score = 0
                if hn and hn in event_name: score += 1
                if an and an in event_name: score += 1
                if score > best_score:
                    best_score = score
                    best_match = pin

            if best_score < 1 or not best_match:
                continue

            signals = match_runners(best_match, bf_mkt)
            if not signals:
                continue

            for sig in signals:
                stake = kelly_stake(
                    balance if balance > 0 else 100.0,
                    sig["bf_price"],
                    sig["edge"],
                )

                print(f"\n  ⚡ СИГНАЛ: {best_match['home']} vs {best_match['away']}")
                print(f"     Ставка на {sig['side'].upper()}: {sig['runner_name']}")
                print(f"     Betfair: {sig['bf_price']:.2f}  |  Pinnacle: {sig['pin_price']:.2f}  |  Edge: +{sig['edge']*100:.1f}%")
                print(f"     Доступно на бирже: £{sig['available']:.0f}")
                print(f"     Ставка: £{stake:.2f}")

                bet_row = {
                    "placed_at":    now_iso(),
                    "market_id":    bf_mkt["market_id"],
                    "event_name":   bf_mkt["event_name"],
                    "bet_side":     sig["side"],
                    "runner_name":  sig["runner_name"],
                    "selection_id": sig["selection_id"],
                    "bf_price":     sig["bf_price"],
                    "pin_price":    sig["pin_price"],
                    "edge_pct":     round(sig["edge"] * 100, 2),
                    "stake_gbp":    stake,
                    "dry_run":      DRY_RUN,
                    "status":       "pending",
                    "league":       best_match.get("league", ""),
                }

                if not DRY_RUN:
                    try:
                        result = bf.place_bet(
                            market_id    = bf_mkt["market_id"],
                            selection_id = sig["selection_id"],
                            price        = sig["bf_price"],
                            size         = stake,
                        )
                        bet_row["bet_id"]       = result.get("bet_id")
                        bet_row["size_matched"] = result.get("size_matched", 0)
                        bet_row["avg_price"]    = result.get("avg_price")
                        bet_row["status"]       = "placed"
                        print(f"     ✅ Ставка размещена! ID: {result.get('bet_id')}")
                    except BetfairError as e:
                        bet_row["status"] = f"error: {e}"
                        print(f"     ❌ Ошибка: {e}")
                else:
                    print(f"     [DRY RUN] Ставка не размещена")

                placed_bets.append(bet_row)

        # ── 4. Save to Supabase
        if placed_bets:
            sb_insert("betfair_bets", placed_bets)
            print(f"\n[4] Сохранено {len(placed_bets)} ставок в Supabase")

        # Save balance snapshot
        sb_insert("betfair_bankroll", [{
            "recorded_at":  now_iso(),
            "balance_gbp":  balance,
            "dry_run":      DRY_RUN,
        }])

        print(f"\n{'='*60}")
        print(f"Итого: {len(placed_bets)} сигналов  |  Баланс: £{balance:.2f}")
        print(f"Finished: {now_iso()}")
        print("=" * 60)


if __name__ == "__main__":
    main()
