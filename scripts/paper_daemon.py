#!/usr/bin/env python3
"""
Paper Trading Daemon — Задача 1 + 5 (сигналы + алерты)
======================================================
Каждый день сканирует upcoming матчи, проверяет Rule C, сохраняет сигналы.

Rule C (FROZEN):
  edge_adj > 0  AND  elo_diff >= 75  AND  odds < 2.0  AND  market_prob 60-70%

Запуск:
  PYTHONPATH=. python3 scripts/paper_daemon.py
  PYTHONPATH=. python3 scripts/paper_daemon.py --dry-run   # не сохранять
"""
from __future__ import annotations

import sys, json, argparse, sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts._paper_core import (
    get_paper_conn, MAIN_DB, RULE_C,
    build_model_state, evaluate_match, best_odds_for_match,
    get_mp, team_match,
)


def scan_upcoming(dry_run=False):
    main_conn = sqlite3.connect(MAIN_DB)
    main_conn.row_factory = sqlite3.Row
    paper_conn = get_paper_conn()

    now_dt = datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    now_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\n{'='*70}")
    print(f"PAPER DAEMON — {now_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")

    # Build frozen model
    print("Загружаем модель...", end=" ", flush=True)
    state = build_model_state(main_conn)
    n_elo = sum(1 for v in state["elo"].values())
    print(f"OK  ({n_elo} команд в рейтинге)")

    # Fetch upcoming matches with DOTA2 league
    upcoming = main_conn.execute("""
        SELECT external_id, league_name, begin_at,
               team_1_name, team_2_name, raw_json
        FROM matches
        WHERE status = 'not_started'
          AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()

    print(f"Upcoming матчей: {len(upcoming)}\n")

    new_signals = []
    skipped_dup = 0
    skipped_noodds = 0
    skipped_games = 0
    skipped_rule = 0

    for m in upcoming:
        t1 = m["team_1_name"]
        t2 = m["team_2_name"]
        eid = str(m["external_id"])
        ln  = m["league_name"] or "?"
        bat = m["begin_at"] or ""

        # Get odds
        od = best_odds_for_match(main_conn, eid)
        if not od:
            skipped_noodds += 1
            continue

        # Align teams with odds snapshot
        mp1, mp2, valid = get_mp(t1, t2, od["t1"], od["t2"], od["oh"], od["oa"])
        if not valid or mp1 is None:
            skipped_noodds += 1
            continue

        # Determine bet direction (we bet on whoever adj_prob > 0.5 for)
        # First evaluate with t1 perspective
        try: match_ts = datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except: match_ts = now_ts

        metrics = evaluate_match(t1, t2, mp1, match_ts, state)
        if metrics is None:
            skipped_games += 1
            continue

        # Determine bet_team and odds
        if metrics["adj_prob"] >= 0.5:
            bet_team  = t1
            opp_team  = t2
            bet_odds  = od["oh"] if team_match(t1, od["t1"]) else od["oa"]
            market_p  = mp1
            edge_adj  = metrics["edge_adj"]    # adj_prob - mp1 (for t1)
        else:
            bet_team  = t2
            opp_team  = t1
            # adj_prob for t2
            adj_t2    = 1.0 - metrics["adj_prob"]
            mp2_val   = 1.0 - mp1
            bet_odds  = od["oa"] if team_match(t1, od["t1"]) else od["oh"]
            market_p  = mp2_val
            edge_adj  = adj_t2 - mp2_val
            metrics["adj_prob"]  = adj_t2
            metrics["elo_prob"]  = 1.0 - metrics["elo_prob"]
            metrics["h2h_delta"] = -metrics["h2h_delta"]
            metrics["edge_adj"]  = edge_adj

        # Check Rule C
        if not RULE_C(edge_adj, metrics["elo_diff"], bet_odds, market_p):
            skipped_rule += 1
            continue

        # Deduplication check
        existing = paper_conn.execute(
            "SELECT id FROM paper_trades WHERE match_id=? AND bet_team=?",
            (eid, bet_team)
        ).fetchone()
        if existing:
            skipped_dup += 1
            continue

        # Build record
        raw = {
            "match_external_id": eid,
            "bookmaker": od["bm"],
            "open": {"h": od["oh"], "a": od["oa"], "t1": od["t1"], "t2": od["t2"]},
            "close": {"h": od["ch"], "a": od["ca"]},
            "metrics": metrics,
        }

        record = dict(
            created_at    = now_str,
            match_id      = eid,
            league        = ln.replace("DOTA2 - ","").replace("DOTA2",""),
            team_1        = t1,
            team_2        = t2,
            bet_team      = bet_team,
            opponent_team = opp_team,
            start_time    = bat,
            bookmaker     = od["bm"],
            open_odds     = round(bet_odds, 3),
            current_odds  = round(bet_odds, 3),
            market_prob   = round(market_p, 4),
            elo_prob      = round(metrics["elo_prob"], 4),
            adj_prob      = round(metrics["adj_prob"], 4),
            edge_adj      = round(metrics["edge_adj"], 4),
            elo_diff      = round(metrics["elo_diff"], 1),
            h2h_n         = metrics["h2h_n"],
            h2h_delta     = round(metrics["h2h_delta"], 4),
            status        = "PENDING",
            raw_json      = json.dumps(raw),
        )

        if not dry_run:
            paper_conn.execute("""
                INSERT INTO paper_trades
                (created_at, match_id, league, team_1, team_2, bet_team, opponent_team,
                 start_time, bookmaker, open_odds, current_odds, market_prob,
                 elo_prob, adj_prob, edge_adj, elo_diff, h2h_n, h2h_delta,
                 status, raw_json)
                VALUES
                (:created_at,:match_id,:league,:team_1,:team_2,:bet_team,:opponent_team,
                 :start_time,:bookmaker,:open_odds,:current_odds,:market_prob,
                 :elo_prob,:adj_prob,:edge_adj,:elo_diff,:h2h_n,:h2h_delta,
                 :status,:raw_json)
                ON CONFLICT(match_id, bet_team) DO NOTHING
            """, record)
            paper_conn.commit()

        new_signals.append(record)

        # ── PAPER SIGNAL ALERT ──────────────────────────────────────────────
        h2h_tag = "+" if record["h2h_delta"] > 0.02 else "~"
        print(f"{'─'*60}")
        print(f"  ★  PAPER SIGNAL  {'(DRY RUN)' if dry_run else ''}")
        print(f"{'─'*60}")
        print(f"  match:      {t1} vs {t2}")
        print(f"  league:     {record['league']}")
        print(f"  bet_team:   {bet_team}")
        print(f"  odds:       {record['open_odds']}")
        print(f"  market_prob:{record['market_prob']:.4f}")
        print(f"  adj_prob:   {record['adj_prob']:.4f}")
        print(f"  edge_adj:   {record['edge_adj']:+.4f}")
        print(f"  elo_diff:   {record['elo_diff']:.0f}")
        print(f"  h2h_n:      {record['h2h_n']}  ({h2h_tag})")
        print(f"  h2h_delta:  {record['h2h_delta']:+.4f}")
        print(f"  bookmaker:  {record['bookmaker']}")
        print(f"  start_time: {bat}")
        print(f"  ──")
        print(f"  Не ставить реальные деньги. Только paper.")
        print()

    # Summary
    total_pending = paper_conn.execute(
        "SELECT COUNT(*) as n FROM paper_trades WHERE status='PENDING'"
    ).fetchone()["n"]
    total_settled = paper_conn.execute(
        "SELECT COUNT(*) as n FROM paper_trades WHERE status IN ('WON','LOST')"
    ).fetchone()["n"]

    print(f"{'─'*60}")
    if new_signals:
        print(f"  ✓ Новых сигналов: {len(new_signals)}")
    else:
        print(f"  Новых сигналов нет.")
    print(f"  Пропущено: дубли={skipped_dup} нет_коэф={skipped_noodds} "
          f"нет_игр={skipped_games} не_Rule_C={skipped_rule}")
    print(f"  Всего в paper_trades: pending={total_pending}  settled={total_settled}")
    milestone_target = ((total_settled // 10) + 1) * 10
    remaining = milestone_target - total_settled
    print(f"  До следующего milestone ({milestone_target}): {remaining} ставок")
    if total_settled >= 30:
        print(f"\n  ★★★ MILESTONE 30 ДОСТИГНУТ. Можно начинать следующий этап. ★★★")
    print()

    main_conn.close()
    paper_conn.close()
    return new_signals


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Daemon")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не сохранять в БД, только показать сигналы")
    args = parser.parse_args()
    scan_upcoming(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
