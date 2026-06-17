#!/usr/bin/env python3
"""
predict_manual.py — виртуальный paper trading без API.

Режимы:
  --scan    Сохранить предикты по всем upcoming матчам (запускать каждый день)
  --update  Проверить результаты и обновить P&L
  --report  Показать текущий отчёт

Запуск:
  PYTHONPATH=. python3 scripts/predict_manual.py --scan
  PYTHONPATH=. python3 scripts/predict_manual.py --update
  PYTHONPATH=. python3 scripts/predict_manual.py --report
"""
from __future__ import annotations

import sys, sqlite3, argparse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts._paper_core import (
    build_model_state, evaluate_match, MAIN_DB,
)

VIRTUAL_STAKE = 1.0   # 1 юнит на ставку
MANUAL_DB = ROOT / "data" / "manual_trades.db"


# ── Schema ────────────────────────────────────────────────────────────────────

def _init_manual_schema(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS manual_trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT NOT NULL,
        match_id      TEXT NOT NULL,
        league        TEXT,
        team_1        TEXT,
        team_2        TEXT,
        begin_at      TEXT,
        pick_team     TEXT NOT NULL,
        pick_prob     REAL NOT NULL,
        opp_prob      REAL NOT NULL,
        elo_diff      REAL NOT NULL,
        h2h_n         INTEGER,
        h2h_delta     REAL,
        rule_c_cand   INTEGER DEFAULT 0,
        status        TEXT DEFAULT 'PENDING',
        result_winner TEXT,
        correct       INTEGER,
        profit_flat   REAL,
        UNIQUE(match_id)
    )
    """)
    conn.commit()


def get_manual_conn():
    MANUAL_DB.parent.mkdir(parents=True, exist_ok=True)
    for suf in ["-journal", "-wal", "-shm"]:
        s = Path(str(MANUAL_DB) + suf)
        try:
            if s.exists(): s.unlink()
        except Exception:
            pass
    if MANUAL_DB.exists() and MANUAL_DB.stat().st_size == 0:
        try: MANUAL_DB.unlink()
        except Exception: pass
    try:
        conn = sqlite3.connect(str(MANUAL_DB))
        conn.row_factory = sqlite3.Row
        _init_manual_schema(conn)
        return conn
    except sqlite3.OperationalError:
        try: MANUAL_DB.unlink()
        except Exception: pass
        conn = sqlite3.connect(str(MANUAL_DB))
        conn.row_factory = sqlite3.Row
        _init_manual_schema(conn)
        return conn


# ── Scan ──────────────────────────────────────────────────────────────────────

def cmd_scan():
    main_conn = sqlite3.connect(str(MAIN_DB))
    main_conn.row_factory = sqlite3.Row
    paper_conn = get_manual_conn()

    now_dt  = datetime.now(timezone.utc)
    now_ts  = now_dt.timestamp()
    now_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\n{'='*70}")
    print(f"PREDICT SCAN — {now_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")

    print("Строим модель...", end=" ", flush=True)
    state = build_model_state(main_conn)
    print(f"OK ({len(state['elo'])} команд)\n")

    matches = main_conn.execute("""
        SELECT external_id, league_name, begin_at, team_1_name, team_2_name
        FROM matches
        WHERE status = 'not_started'
          AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()

    print(f"Upcoming матчей: {len(matches)}\n")

    new_preds = 0
    skip_dup  = 0
    skip_nodata = 0

    print(f"{'#':>2}  {'Начало':16}  {'Пик':22}  {'p':>5}  {'EloDiff':>7}  {'H2H':>3}  {'Флаг':6}")
    print("-" * 75)

    for i, m in enumerate(matches, 1):
        t1  = m["team_1_name"]
        t2  = m["team_2_name"]
        eid = str(m["external_id"])
        ln  = m["league_name"] or "?"
        bat = m["begin_at"] or ""

        try:
            bat_ts = datetime.fromisoformat(bat.replace("Z", "+00:00")).timestamp()
        except Exception:
            bat_ts = now_ts

        met = evaluate_match(t1, t2, 0.5, bat_ts, state)
        if met is None:
            skip_nodata += 1
            continue

        adj1 = met["adj_prob"]
        adj2 = 1.0 - adj1

        pick  = t1 if adj1 >= adj2 else t2
        opp   = t2 if adj1 >= adj2 else t1
        p_pick = adj1 if adj1 >= adj2 else adj2
        p_opp  = 1.0 - p_pick

        ediff = met["elo_diff"]
        h2h_n = met["h2h_n"]
        h2h_d = met["h2h_delta"]

        # Rule C candidate: elo_diff>=75 AND adj_prob 60-70% (proxy for market_prob)
        rule_c = 1 if ediff >= 75 and 0.60 <= p_pick < 0.70 else 0
        flag   = "★ RC?" if rule_c else ("·" if ediff >= 75 else "")

        # Dedup
        ex = paper_conn.execute(
            "SELECT id FROM manual_trades WHERE match_id=?", (eid,)
        ).fetchone()
        if ex:
            skip_dup += 1
            continue

        paper_conn.execute("""
            INSERT INTO manual_trades
            (created_at, match_id, league, team_1, team_2, begin_at,
             pick_team, pick_prob, opp_prob, elo_diff, h2h_n, h2h_delta,
             rule_c_cand, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING')
        """, (now_str, eid, ln, t1, t2, bat,
              pick, round(p_pick, 4), round(p_opp, 4),
              round(ediff, 1), h2h_n, round(h2h_d, 4), rule_c))

        h2h_str = f"+{h2h_n}" if h2h_n > 0 else "0"
        print(f"{i:>2}  {bat[:16]}  {pick[:22]:22}  {p_pick:>5.3f}  {ediff:>7.0f}  {h2h_str:>3}  {flag}")
        new_preds += 1

    paper_conn.commit()
    main_conn.close(); paper_conn.close()

    print(f"\n  Сохранено новых предиктов: {new_preds}")
    if skip_dup:     print(f"  Пропущено (дубли): {skip_dup}")
    if skip_nodata:  print(f"  Пропущено (нет данных): {skip_nodata}")
    print(f"\n  ★ RC? = кандидат Rule C (elo_diff>=75 AND adj_prob 60-70%)")
    print(f"  Завтра запусти --update чтобы проверить результаты.\n")


# ── Update ────────────────────────────────────────────────────────────────────

def cmd_update():
    main_conn  = sqlite3.connect(str(MAIN_DB))
    main_conn.row_factory = sqlite3.Row
    paper_conn = get_manual_conn()

    print(f"\n{'='*70}")
    print(f"PREDICT UPDATE — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")

    pending = paper_conn.execute(
        "SELECT * FROM manual_trades WHERE status='PENDING'"
    ).fetchall()

    print(f"PENDING предиктов: {len(pending)}\n")
    updated = 0

    for t in pending:
        match = main_conn.execute("""
            SELECT status, winner_name FROM matches WHERE external_id=?
        """, (t["match_id"],)).fetchone()

        if not match or match["status"] != "finished":
            continue

        winner = match["winner_name"]
        if not winner:
            paper_conn.execute(
                "UPDATE manual_trades SET status='VOID' WHERE id=?", (t["id"],)
            )
            print(f"  VOID   {t['begin_at'][:10]}  {t['pick_team'][:22]}")
            updated += 1
            continue

        correct = 1 if winner == t["pick_team"] else 0
        profit  = round(VIRTUAL_STAKE if correct else -VIRTUAL_STAKE, 2)
        mark    = "W ✓" if correct else "L ✗"

        paper_conn.execute("""
            UPDATE manual_trades
            SET status='SETTLED', result_winner=?, correct=?, profit_flat=?
            WHERE id=?
        """, (winner, correct, profit, t["id"]))

        rc_tag = " [RC?]" if t["rule_c_cand"] else ""
        print(f"  [{mark}]  {t['begin_at'][:10]}  "
              f"{t['pick_team'][:22]:22}  p={t['pick_prob']:.3f}  "
              f"P&L={profit:+.1f}{rc_tag}")
        updated += 1

    paper_conn.commit()
    main_conn.close(); paper_conn.close()

    print(f"\n  Обновлено: {updated}  |  Осталось ждать: {len(pending)-updated}\n")


# ── Report ────────────────────────────────────────────────────────────────────

def cmd_report():
    paper_conn = get_manual_conn()

    all_trades = paper_conn.execute(
        "SELECT * FROM manual_trades ORDER BY begin_at ASC"
    ).fetchall()
    paper_conn.close()

    settled = [t for t in all_trades if t["status"] == "SETTLED"]
    pending = [t for t in all_trades if t["status"] == "PENDING"]
    rc_trades = [t for t in settled if t["rule_c_cand"]]

    print(f"\n{'='*70}")
    print(f"VIRTUAL PAPER TRADING REPORT")
    print(f"{'='*70}")
    print(f"Всего предиктов: {len(all_trades)}  |  "
          f"Settled: {len(settled)}  |  Pending: {len(pending)}")
    print()

    if not settled:
        print("Нет settled предиктов. Запусти --update после завершения матчей.\n")
        return

    def stats(trades):
        if not trades: return None
        n = len(trades)
        wins = sum(t["correct"] for t in trades)
        profits = [t["profit_flat"] for t in trades]
        roi = sum(profits) / n
        bankroll = 100.0 + sum(profits)
        return dict(n=n, wins=wins, wr=wins/n, roi=roi,
                    bankroll=bankroll, profits=profits)

    def print_stats(label, trades):
        s = stats(trades)
        if not s:
            print(f"{label}: нет данных")
            return
        print(f"{label}")
        print(f"  n={s['n']}  WR={s['wr']:.1%}  ROI={s['roi']:+.3f}  "
              f"Банкролл={s['bankroll']:.1f} (старт 100)")

    print_stats("ВСЕ ПРЕДИКТЫ", settled)
    print_stats("Rule C кандидаты (elo_diff>=75, p=60-70%)", rc_trades)
    non_rc = [t for t in settled if not t["rule_c_cand"]]
    print_stats("Остальные матчи", non_rc)

    print()
    print(f"{'#':>3}  {'Дата':10}  {'Пик':22}  {'p':>5}  {'EloDiff':>7}  "
          f"{'H2H':>3}  {'Рез':>4}  {'P&L':>4}  Флаг")
    print("-" * 80)

    cum = 100.0
    for i, t in enumerate(settled, 1):
        mark = "W" if t["correct"] else "L"
        pnl  = t["profit_flat"]
        cum += pnl
        rc   = " RC?" if t["rule_c_cand"] else ""
        h2h  = f"+{t['h2h_n']}" if t["h2h_n"] else "0"
        print(f"{i:>3}  {str(t['begin_at'])[:10]}  "
              f"{t['pick_team'][:22]:22}  {t['pick_prob']:>5.3f}  "
              f"{t['elo_diff']:>7.0f}  {h2h:>3}  "
              f"{mark:>4}  {pnl:>+.0f}  {cum:.0f}{rc}")

    if pending:
        print(f"\nОЖИДАЮТ ({len(pending)}):")
        for t in pending:
            rc = " [RC?]" if t["rule_c_cand"] else ""
            print(f"  {str(t['begin_at'])[:16]}  "
                  f"{t['pick_team'][:22]:22}  p={t['pick_prob']:.3f}  "
                  f"elo={t['elo_diff']:.0f}{rc}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Virtual paper trading без API")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--scan",   action="store_true", help="Сохранить предикты")
    grp.add_argument("--update", action="store_true", help="Обновить результаты")
    grp.add_argument("--report", action="store_true", help="Показать отчёт")
    args = parser.parse_args()

    if args.scan:   cmd_scan()
    if args.update: cmd_update()
    if args.report: cmd_report()


if __name__ == "__main__":
    main()
