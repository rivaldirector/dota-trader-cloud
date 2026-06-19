#!/usr/bin/env python3
"""
Daily Settle — расчитывает результаты вчерашних (и любых ещё не рассчитанных)
сигналов по факту matches, обновляет виртуальный банк и пишет отчёт в
Supabase.daily_reports.

Run:
    python3 scripts/daily_settle.py

GitHub Actions / scheduled task: каждое утро, после daily_forecast.py.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from daily_pipeline_lib import (
    sb_get, sb_patch, sb_upsert, get_bank, set_bank, fmt_usd, now_iso,
    sb_set_pipeline_status,
)


def settle_pending():
    pending = sb_get("daily_signals", "result=eq.pending&order=start_time.asc")
    if not pending:
        return []

    event_ids = sorted({s["event_id"] for s in pending})
    events = {}
    for i in range(0, len(event_ids), 100):
        chunk = event_ids[i:i + 100]
        ids_str = ",".join(f'"{e}"' for e in chunk)
        rows = sb_get("betsapi_events",
                       f"event_id=in.({ids_str})&status=eq.ended&winner=neq.&select=event_id,home_team,away_team,winner")
        for r in rows:
            events[r["event_id"]] = r

    bank = get_bank()
    bank_usd = float(bank["current_bank_usd"])
    peak_usd = float(bank["peak_bank_usd"])

    settled = []
    for s in pending:
        ev = events.get(s["event_id"])
        if not ev or not ev.get("winner"):
            continue  # match not finished yet, leave pending

        pick = s["home_team"] if s["bet_side"] == "home" else s["away_team"]
        won = (ev["winner"] == pick)
        stake = float(s["stake_usd"])
        odds = float(s["bm_odds"])
        profit = stake * (odds - 1) if won else -stake

        bank_before = bank_usd
        bank_usd = max(0.0, bank_usd + profit)
        peak_usd = max(peak_usd, bank_usd)

        sb_patch("daily_signals", f"id=eq.{s['id']}", {
            "result": "win" if won else "loss",
            "profit_usd": round(profit, 2),
            "bank_after_usd": round(bank_usd, 2),
            "settled_at": now_iso(),
        })
        s["result"] = "win" if won else "loss"
        s["profit_usd"] = round(profit, 2)
        s["bank_before_usd"] = round(bank_before, 2)
        s["bank_after_usd"] = round(bank_usd, 2)
        settled.append(s)

    if settled:
        set_bank(bank_usd, peak_usd)
    return settled


def build_report_for_date(date_str: str, all_signals_for_date: list, bank_before_day: float,
                           bank_after_day: float, peak_usd: float):
    decided = [s for s in all_signals_for_date if s["result"] in ("win", "loss")]
    wins = sum(1 for s in decided if s["result"] == "win")
    losses = sum(1 for s in decided if s["result"] == "loss")
    staked = sum(float(s["stake_usd"]) for s in decided)
    profit = sum(float(s["profit_usd"]) for s in decided)
    winrate = round(100 * wins / len(decided), 1) if decided else None
    roi = round(100 * profit / staked, 1) if staked else None
    change_pct = round(100 * (bank_after_day - bank_before_day) / bank_before_day, 2) if bank_before_day else 0
    drawdown = round(100 * (peak_usd - bank_after_day) / peak_usd, 2) if peak_usd else 0

    by_sport = defaultdict(lambda: {"bets": 0, "wins": 0, "profit": 0.0})
    for s in decided:
        d = by_sport[s["sport"]]
        d["bets"] += 1
        d["wins"] += 1 if s["result"] == "win" else 0
        d["profit"] += float(s["profit_usd"])

    best = max(decided, key=lambda s: float(s["profit_usd"]), default=None)
    worst = min(decided, key=lambda s: float(s["profit_usd"]), default=None)

    def describe(s):
        if not s:
            return None
        pick = s["home_team"] if s["bet_side"] == "home" else s["away_team"]
        return f"{pick} @{s['bookmaker']} {s['bm_odds']} -> {fmt_usd(float(s['profit_usd']))}"

    report = {
        "report_date": date_str,
        "bets_count": len(decided), "wins": wins, "losses": losses, "voids": 0,
        "winrate_pct": winrate, "staked_usd": round(staked, 2), "profit_usd": round(profit, 2),
        "roi_pct": roi, "bank_start_usd": round(bank_before_day, 2),
        "bank_end_usd": round(bank_after_day, 2), "bank_change_pct": change_pct,
        "cumulative_profit_usd": None, "peak_bank_usd": round(peak_usd, 2),
        "drawdown_pct": drawdown, "best_bet": describe(best), "worst_bet": describe(worst),
        "by_sport_json": {k: {**v, "profit": round(v["profit"], 2)} for k, v in by_sport.items()},
    }
    return report


def main():
    settled = settle_pending()
    if not settled:
        print("Нет новых рассчитанных ставок (матчи ещё не закончились или сигналов не было).")
        sb_set_pipeline_status("daily_settle", True, "нет новых расчитанных ставок")
        return

    # Recompute the FULL day's report from all decided bets for that date (not just the
    # ones settled in this run) — idempotent, safe to run multiple times a day as cron does.
    affected_dates = sorted({s["signal_date"] for s in settled})

    for date_str in affected_dates:
        day_signals = sb_get("daily_signals",
                              f"signal_date=eq.{date_str}&result=neq.pending&order=start_time.asc")
        if not day_signals:
            continue
        bank_before = float(day_signals[0]["bank_before_usd"])
        bank_after = float(day_signals[-1]["bank_after_usd"])
        bank = get_bank()
        report = build_report_for_date(date_str, day_signals, bank_before, bank_after,
                                        float(bank["peak_bank_usd"]))
        sb_upsert("daily_reports", [report], on_conflict="report_date")

        print("=" * 60)
        print(f"ОТЧЁТ ЗА {date_str}")
        print("=" * 60)
        print(f"Ставок: {report['bets_count']}  (W{report['wins']}/L{report['losses']})  "
              f"винрейт: {report['winrate_pct']}%")
        print(f"Стейкано: ${report['staked_usd']:,.2f}  Профит: {fmt_usd(report['profit_usd'])}  "
              f"ROI: {report['roi_pct']}%")
        print(f"Банк: ${report['bank_start_usd']:,.2f} -> ${report['bank_end_usd']:,.2f} "
              f"({'+' if report['bank_change_pct']>=0 else ''}{report['bank_change_pct']}%)")
        print(f"Просадка от пика: {report['drawdown_pct']}%")
        if report["best_bet"]:
            print(f"Лучшая ставка:  {report['best_bet']}")
        if report["worst_bet"]:
            print(f"Худшая ставка:  {report['worst_bet']}")
        print(f"По дисциплинам: {report['by_sport_json']}")
        print()

    sb_set_pipeline_status("daily_settle", True, f"расчитано {len(settled)} ставок")


if __name__ == "__main__":
    main()
