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
# Для серий ждём дольше — BO3 может идти 4-5 часов
SERIES_SETTLE_BUFFER_SEC = 6 * 3600


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


def settle_series_bet(bet: dict, home: str, away: str, st: int,
                      pro_matches: list) -> tuple[str | None, float | None]:
    """
    Для ставок bet_market='series': смотрим ВСЕ матчи команд в окне ±8ч
    от start_time, определяем победителя серии по большинству карт.
    Возвращает (winner_side, None) или (None, None) если не можем определить.
    """
    team_wins: dict[str, int] = {"home": 0, "away": 0}
    found_any = False
    for pm in pro_matches:
        rn, dn = pm.get("radiant_name"), pm.get("dire_name")
        if not rn or not dn:
            continue
        pm_st = pm.get("start_time", 0)
        if abs(pm_st - st) > 8 * 3600:
            continue
        s_direct = fuzzy(home, rn) + fuzzy(away, dn)
        s_cross  = fuzzy(home, dn) + fuzzy(away, rn)
        score, orient = (s_direct, "direct") if s_direct >= s_cross else (s_cross, "cross")
        if score < 1.3:
            continue
        found_any = True
        radiant_win = bool(pm.get("radiant_win"))
        home_won = radiant_win if orient == "direct" else (not radiant_win)
        if home_won:
            team_wins["home"] += 1
        else:
            team_wins["away"] += 1

    if not found_any:
        return None, None
    # Нужно знать, что серия завершена: один из участников набрал 2+ карт (BO3) или 3+ (BO5)
    max_wins = max(team_wins.values())
    if max_wins < 2:
        # Серия ещё не завершена — подождём следующего прогона
        return None, None
    winner_side = "home" if team_wins["home"] >= team_wins["away"] else "away"
    print(f"    [series] {home} vs {away}: {team_wins['home']}-{team_wins['away']} карт → {winner_side}")
    return winner_side, None


def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    now_ts = int(datetime.now(timezone.utc).timestamp())

    # Pending ставки: series ждут 6ч, map/moneyline — 3ч
    # Используем or(... is.null) чтобы захватить старые ставки без bet_market
    pending_map = sb_get(
        "elo_paper_bets",
        f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.false"
        f"&start_time=lte.{now_ts - SETTLE_BUFFER_SEC}"
        f"&or=(bet_market.neq.series,bet_market.is.null)&select=*",
    )
    pending_series = sb_get(
        "elo_paper_bets",
        f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.false"
        f"&start_time=lte.{now_ts - SERIES_SETTLE_BUFFER_SEC}"
        f"&bet_market=eq.series&select=*",
    )
    pending = pending_map + pending_series
    print(f"Pending map/mono: {len(pending_map)}, series: {len(pending_series)}")
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
        bet_market = bet.get("bet_market") or "moneyline"

        if bet_market == "series":
            # Settle по результату серии
            winner_side, _ = settle_series_bet(bet, home, away, st, pro_matches)
            if winner_side is None:
                print(f"  ? [series] {home} vs {away} — серия ещё не завершена или не найдена")
                continue
        else:
            # Settle по результату конкретной карты (текущее поведение)
            best, best_score, best_orient = None, 0.0, "direct"
            for pm in pro_matches:
                rn, dn = pm.get("radiant_name"), pm.get("dire_name")
                if not rn or not dn:
                    continue
                pm_st = pm.get("start_time", 0) + (pm.get("duration") or 0)
                if abs(pm_st - st) > 6 * 3600:
                    continue
                s_direct = fuzzy(home, rn) + fuzzy(away, dn)
                s_cross  = fuzzy(home, dn) + fuzzy(away, rn)
                score, orient = (s_direct, "direct") if s_direct >= s_cross else (s_cross, "cross")
                if score > best_score:
                    best, best_score, best_orient = pm, score, orient

            if best_score < 1.3 or not best:
                print(f"  ? {home} vs {away} — не нашли совпадение в OpenDota (score={best_score:.2f})")
                continue

            radiant_win = bool(best.get("radiant_win"))
            home_won = radiant_win if best_orient == "direct" else (not radiant_win)
            winner_side = "home" if home_won else "away"

        odds  = bet.get("real_odds") or bet["odds"]
        stake = bet["stake_usd"]
        bet_team = bet["bet_team"]
        if bet_team == winner_side:
            outcome, pnl = "win", round((odds - 1.0) * stake, 2)
        else:
            outcome, pnl = "loss", -stake

        sb_patch("elo_paper_bets", f"id=eq.{bet['id']}", {
            "settled": True, "outcome": outcome, "pnl": pnl, "settled_ts": now_iso(),
        })
        settled_n += 1
        mkt_tag = f"[{bet_market}] " if bet_market != "moneyline" else ""
        print(f"  ✓ {mkt_tag}{home} vs {away} → {winner_side} ({outcome}, pnl=${pnl:+.2f})")

    if settled_n:
        new_total_pnl = sum(
            float(x["pnl"]) for x in sb_get(
                "elo_paper_bets",
                f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.true&stake_usd=gt.0&select=pnl",
            )
        )
        n_settled = sum(
            1 for _ in sb_get(
                "elo_paper_bets",
                f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.true&stake_usd=gt.0&select=id",
            )
        )
        new_bank = round(1000.0 + new_total_pnl, 2)
        sb_patch("bankroll_state", f"strategy=eq.{STRATEGY_NAME}", {
            "balance": new_bank, "total_bets": n_settled,
            "updated_at": now_iso(),
        })
        print(f"\nБанк обновлён: ${new_bank:,.2f} (P&L ${new_total_pnl:+.2f})")

    print(f"\nУрегулировано: {settled_n}/{len(pending)}")


if __name__ == "__main__":
    main()
