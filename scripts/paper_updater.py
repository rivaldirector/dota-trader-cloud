#!/usr/bin/env python3
"""
Paper Updater — Задача 2 (обновление результатов)
==================================================
Для всех PENDING ставок проверяет: завершился ли матч?
Если да — обновляет result, close_odds, CLV, profit_flat, status.

Запуск:
  PYTHONPATH=. python3 scripts/paper_updater.py
  PYTHONPATH=. python3 scripts/paper_updater.py --dry-run
"""
from __future__ import annotations

import sys, json, argparse, sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts._paper_core import (
    get_paper_conn, MAIN_DB,
    get_mp, team_match, best_odds_for_match,
)


def update_results(dry_run=False):
    main_conn  = sqlite3.connect(MAIN_DB)
    main_conn.row_factory = sqlite3.Row
    paper_conn = get_paper_conn()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\n{'='*70}")
    print(f"PAPER UPDATER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")

    # Load all PENDING trades
    pending = paper_conn.execute(
        "SELECT * FROM paper_trades WHERE status='PENDING'"
    ).fetchall()

    print(f"PENDING ставок: {len(pending)}")
    if not pending:
        print("Нечего обновлять.\n")
        main_conn.close(); paper_conn.close()
        return

    updated = []
    not_finished = []

    for trade in pending:
        eid = trade["match_id"]

        # Check if match is now finished in main DB
        match = main_conn.execute("""
            SELECT status, winner_name, team_1_name, team_2_name
            FROM matches
            WHERE external_id=?
        """, (eid,)).fetchone()

        if not match:
            not_finished.append(trade)
            continue

        if match["status"] != "finished":
            not_finished.append(trade)
            continue

        # Match is finished
        winner = match["winner_name"]
        if not winner:
            # VOID
            if not dry_run:
                paper_conn.execute("""
                    UPDATE paper_trades
                    SET status='VOID', result='no_winner'
                    WHERE id=?
                """, (trade["id"],))
                paper_conn.commit()
            print(f"  VOID  {trade['bet_team']} vs {trade['opponent_team']} "
                  f"(no winner)")
            continue

        # Get close odds for CLV
        od = best_odds_for_match(main_conn, eid)
        t1 = match["team_1_name"]
        t2 = match["team_2_name"]

        close_odds = None
        clv        = None

        if od:
            # Find close odds for bet_team
            if team_match(trade["bet_team"], od["t1"]):
                close_odds = od["ch"]
            elif team_match(trade["bet_team"], od["t2"]):
                close_odds = od["ca"]

            if close_odds and close_odds > 1:
                # CLV = market_prob at close - market_prob at open for bet_team
                # market_prob at open  = trade["market_prob"]
                # market_prob at close = novig from close odds
                if team_match(trade["bet_team"], od["t1"]):
                    mp_close_bet, _, _ = get_mp(t1, t2, od["t1"], od["t2"],
                                                od["ch"], od["ca"])
                    if mp_close_bet is None:
                        mp_close_bet, _ = __import__("scripts._paper_core",
                                                     fromlist=["novig"]).novig(od["ch"], od["ca"])
                        mp_close_bet = mp_close_bet or 0.0
                else:
                    _, mp_close_bet, _ = get_mp(t1, t2, od["t1"], od["t2"],
                                                od["ch"], od["ca"])
                    if mp_close_bet is None: mp_close_bet = 0.0

                clv = round(mp_close_bet - trade["market_prob"], 4) if mp_close_bet else None

        # Result
        is_win = team_match(winner, trade["bet_team"])
        status = "WON" if is_win else "LOST"
        profit = round((trade["open_odds"] - 1), 4) if is_win else -1.0

        if not dry_run:
            paper_conn.execute("""
                UPDATE paper_trades
                SET status=?, result=?, close_odds=?, clv=?,
                    profit_flat=?, current_odds=?
                WHERE id=?
            """, (status, winner,
                  round(close_odds, 3) if close_odds else None,
                  clv, profit,
                  round(close_odds, 3) if close_odds else trade["open_odds"],
                  trade["id"]))
            paper_conn.commit()

        mark = "W ✓" if is_win else "L ✗"
        clv_str = f"CLV={clv:+.4f}" if clv is not None else "CLV=n/a"
        print(f"  [{mark}]  {trade['start_time'][:10]}  "
              f"{trade['bet_team'][:20]:20}  "
              f"odds={trade['open_odds']}  profit={profit:+.3f}  {clv_str}")
        updated.append(trade)

    print(f"\n  Обновлено: {len(updated)}  |  Ещё ожидают: {len(not_finished)}")

    # Trigger milestone check
    total_settled = paper_conn.execute(
        "SELECT COUNT(*) as n FROM paper_trades WHERE status IN ('WON','LOST')"
    ).fetchone()["n"]

    prev_settled = total_settled - len(updated)
    # Check if we crossed a milestone
    for milestone in range(10, 61, 10):
        if prev_settled < milestone <= total_settled:
            print(f"\n  ★ MILESTONE {milestone} ДОСТИГНУТ!")
            _print_milestone(paper_conn, milestone, total_settled)

    if total_settled >= 30:
        print(f"\n  ★★★ 30 новых settled сигналов. "
              f"Можно начинать следующий этап исследований. ★★★")

    main_conn.close()
    paper_conn.close()


def _print_milestone(paper_conn, milestone, n_total):
    from scripts._paper_core import compute_stats, signal_status, bayesian_wr
    trades = paper_conn.execute(
        "SELECT * FROM paper_trades WHERE status IN ('WON','LOST') "
        "ORDER BY start_time ASC LIMIT ?", (n_total,)
    ).fetchall()

    s = compute_stats(trades)
    if not s: return

    lo_s = f"{s['lo']:+.3f}" if s["lo"]==s["lo"] else " nan"
    hi_s = f"{s['hi']:+.3f}" if s["hi"]==s["hi"] else " nan"
    stat = signal_status(s)

    print(f"\n  {'─'*50}")
    print(f"  MILESTONE {milestone}  —  {stat}")
    print(f"  {'─'*50}")
    print(f"  n={s['n']}  WR={s['wr']:.3f}  ROI={s['roi']:+.4f}  CI=[{lo_s},{hi_s}]")
    print(f"  CLV+={s['clvpos']:.3f}  avgCLV={s['avg_clv']:+.4f}")
    print(f"  Bayesian WR={s['bay_mean']:.3f}  [{s['bay_lo']:.3f},{s['bay_hi']:.3f}]")
    print(f"  P(edge>0)={s['p_edge_pos']:.1%}")
    print(f"  max_DD={s['max_dd']:.3f}  max_losing_streak={s['max_ls']}")

    if s["max_ls"] >= 5 and s["n"] >= 20:
        print(f"\n  ⚠️  СТОП-СИГНАЛ: {s['max_ls']} убытков подряд при n≥20!")
    print()


def main():
    parser = argparse.ArgumentParser(description="Paper Updater")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    update_results(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
