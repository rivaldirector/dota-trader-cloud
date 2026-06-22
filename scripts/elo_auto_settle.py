#!/usr/bin/env python3
"""
elo_auto_settle.py — автономный сеттлинг ставок AUTO_ELO_FLAT (см.
elo_auto_bet.py) через бесплатный OpenDota /api/proMatches (без BetsAPI,
без ключа). Сверяет команды по имени (fuzzy) + близости времени старта.

Обновляет elo_bankroll (current/peak) по факту реальных исходов — банк
двигается ТОЛЬКО на условных $ из elo_auto_bet.py (notional odds), не на
рыночных. Это честно помечено в /dashboard как "иллюстрация", а не edge.

Run:
    python3 scripts/elo_auto_settle.py

GitHub Actions: каждые 2 часа (см. elo_auto_pipeline.yml), перед bet-шагом.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

OPENDOTA_PROMATCHES_URL = "https://api.opendota.com/api/proMatches"
STRATEGY_NAME = "AUTO_ELO_FLAT"
FUZZY_MIN = 0.72
SETTLE_BUFFER_SEC = 3 * 3600  # ждём минимум 3ч после старта, чтобы матч точно закончился


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sb_get(table: str, qs: str) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=SB_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_patch(table: str, qs: str, payload: dict) -> None:
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=SB_HEADERS, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] patch {table}: {r.status_code} {r.text[:200]}")


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()


def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    pending = sb_get(
        "elo_paper_bets",
        f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.false&"
        f"start_time=lte.{now_ts - SETTLE_BUFFER_SEC}&select=*",
    )
    print(f"Pending ставок (старше {SETTLE_BUFFER_SEC//3600}ч): {len(pending)}")
    if not pending:
        return

    try:
        r = requests.get(OPENDOTA_PROMATCHES_URL, timeout=20)
        r.raise_for_status()
        pro_matches = r.json()
    except Exception as ex:
        print(f"ERROR: OpenDota proMatches недоступен: {ex}")
        sys.exit(1)
    print(f"OpenDota proMatches вернул: {len(pro_matches)} матчей")

    settled_n = 0
    for bet in pending:
        home, away = bet["home_team"], bet["away_team"]
        st = bet["start_time"]
        best, best_score = None, 0.0
        for pm in pro_matches:
            rn, dn = pm.get("radiant_name"), pm.get("dire_name")
            if not rn or not dn:
                continue
            pm_st = pm.get("start_time", 0) + (pm.get("duration") or 0)
            if abs(pm_st - st) > 6 * 3600:  # вне разумного окна — не тот матч
                continue
            s_direct = fuzzy(home, rn) + fuzzy(away, dn)
            s_cross = fuzzy(home, dn) + fuzzy(away, rn)
            score, orient = (s_direct, "direct") if s_direct >= s_cross else (s_cross, "cross")
            if score > best_score:
                best, best_score, best_orient = pm, score, orient

        if best_score < 1.3 or not best:
            print(f"  ? {home} vs {away} — не нашли совпадение в OpenDota (score={best_score:.2f})")
            continue

        radiant_win = bool(best.get("radiant_win"))
        # direct: home=radiant, away=dire | cross: home=dire, away=radiant
        home_won = radiant_win if best_orient == "direct" else (not radiant_win)
        winner_side = "home" if home_won else "away"

        odds, stake, bet_team = bet["odds"], bet["stake_usd"], bet["bet_team"]
        if bet_team == winner_side:
            outcome, pnl = "win", round((odds - 1.0) * stake, 2)
        else:
            outcome, pnl = "loss", -stake

        sb_patch("elo_paper_bets", f"id=eq.{bet['id']}", {
            "settled": True, "outcome": outcome, "pnl": pnl, "settled_ts": now_iso(),
        })
        settled_n += 1
        print(f"  ✓ {home} vs {away} -> winner={winner_side} ({outcome}, pnl=${pnl:+.2f})")

    if settled_n:
        bankroll = sb_get("elo_bankroll", "select=*&limit=1")
        if bankroll:
            b = bankroll[0]
            new_total_pnl = sum(
                float(x["pnl"]) for x in sb_get(
                    "elo_paper_bets",
                    f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.true&select=pnl",
                )
            )
            new_bank = round(float(b["start_bank_usd"]) + new_total_pnl, 2)
            new_peak = max(new_bank, float(b["peak_bank_usd"]))
            sb_patch("elo_bankroll", "id=eq.1", {
                "current_bank_usd": new_bank, "peak_bank_usd": new_peak, "updated_at": now_iso(),
            })
            print(f"\nБанк обновлён: ${new_bank:,.2f} (peak ${new_peak:,.2f})")

    print(f"\nУрегулировано: {settled_n}/{len(pending)}")


if __name__ == "__main__":
    main()
