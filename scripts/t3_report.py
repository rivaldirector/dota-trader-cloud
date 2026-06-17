#!/usr/bin/env python3
"""
Terminal 3 — Live Report Dashboard
Читает predictions.db, обновляет экран каждые 30 секунд. Без API-вызовов.

Usage:
    python3 scripts/t3_report.py
    python3 scripts/t3_report.py --once     # один снимок и выход
    python3 scripts/t3_report.py --interval 30
"""
import argparse, os, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT    = Path(__file__).parent.parent
PRED_DB = ROOT / "storage" / "predictions.db"
STARTING_BANK = 1000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE, home_team TEXT, away_team TEXT, league TEXT,
    start_time INTEGER, elo_home REAL, elo_away REAL, elo_diff REAL,
    model_prob REAL, bookmaker TEXT, open_odds_h REAL, open_odds_a REAL,
    mkt_prob REAL, edge REAL, rule_c INTEGER DEFAULT 0,
    pick TEXT, pick_odds REAL, stake REAL,
    close_odds REAL, clv REAL, result TEXT, correct INTEGER, profit REAL,
    created_at TEXT, settled_at TEXT
);
CREATE TABLE IF NOT EXISTS bankroll (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, event_id TEXT, action TEXT, amount REAL, balance REAL, note TEXT
);
"""


def open_db():
    if not PRED_DB.exists():
        return None
    c = sqlite3.connect(f"file:{PRED_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def bar(value, max_val, width=20, fill="█", empty="░"):
    if max_val == 0: return empty * width
    filled = int(round(value / max_val * width))
    return fill * filled + empty * (width - filled)


def print_report(db):
    os.system("clear")
    now = datetime.now().strftime("%d.%m.%Y  %H:%M:%S")

    if db is None:
        print(f"  [{now}]  predictions.db не найден")
        print(f"  Запусти: python3 scripts/t2_predict.py")
        return

    # ── Банк ─────────────────────────────────────────────────────────────────
    bank_rows = db.execute(
        "SELECT * FROM bankroll ORDER BY id ASC"
    ).fetchall()
    balance   = bank_rows[-1]["balance"] if bank_rows else STARTING_BANK
    bets_n    = sum(1 for r in bank_rows if r["action"] == "bet")
    wins_n    = sum(1 for r in bank_rows if r["action"] == "win")
    roi       = (balance - STARTING_BANK) / STARTING_BANK * 100

    # ── Предикты ─────────────────────────────────────────────────────────────
    all_p     = db.execute("SELECT * FROM predictions ORDER BY start_time DESC").fetchall()
    settled   = [p for p in all_p if p["settled_at"] is not None]
    pending   = [p for p in all_p if p["settled_at"] is None]
    rc_all    = [p for p in settled if p["rule_c"] == 1]
    rc_wins   = [p for p in rc_all  if p["correct"] == 1]
    rc_losses = [p for p in rc_all  if p["correct"] == 0]

    total_profit  = sum(p["profit"] for p in rc_all if p["profit"] is not None)
    clvs          = [p["clv"] for p in settled if p["clv"] is not None]
    avg_clv       = sum(clvs)/len(clvs) if clvs else 0.0
    pos_clv       = sum(1 for c in clvs if c > 0)

    # ── Шапка ────────────────────────────────────────────────────────────────
    print(f"╔{'═'*68}╗")
    print(f"║  Terminal 3 — Live Report                    {now}  ║")
    print(f"╠{'═'*68}╣")

    # ── Банк блок ────────────────────────────────────────────────────────────
    roi_sign = f"{roi:+.1f}%"
    roi_bar  = bar(balance, STARTING_BANK * 1.5, width=24)
    print(f"║  БАНК                                                              ║")
    print(f"║  Старт:  {STARTING_BANK:>8.2f}   Текущий: {balance:>10.2f}   ROI: {roi_sign:>7}       ║")
    print(f"║  [{roi_bar}]  Ставок: {bets_n}  Выигр: {wins_n}              ║")
    print(f"╠{'═'*68}╣")

    # ── P&L блок ─────────────────────────────────────────────────────────────
    print(f"║  RULE C  (elo_diff≥75, edge>0, odds<2.0, mkt 60-70%)              ║")
    if rc_all:
        wr = len(rc_wins)/len(rc_all)*100 if rc_all else 0
        wr_bar = bar(len(rc_wins), len(rc_all), width=16)
        print(f"║  Settled: {len(rc_all):>3}  Win: {len(rc_wins):>3}  Loss: {len(rc_losses):>3}  "
              f"WR: {wr:>5.1f}%  [{wr_bar}]  ║")
        print(f"║  P&L:  {total_profit:>+9.2f}   Avg CLV: {avg_clv:>+.4f}   "
              f"CLV+: {pos_clv}/{len(clvs)}                ║")
    else:
        print(f"║  Нет settled Rule C ставок                                        ║")
    print(f"╠{'═'*68}╣")

    # ── Pending ставки ───────────────────────────────────────────────────────
    rc_pending = [p for p in pending if p["rule_c"] == 1]
    print(f"║  ОТКРЫТЫЕ СТАВКИ  ({len(rc_pending)})                                          ║")
    if rc_pending:
        for p in rc_pending[:6]:
            start = datetime.fromtimestamp(p["start_time"], tz=timezone.utc).strftime("%d.%m %H:%M") \
                    if p["start_time"] else "?"
            name  = f"{p['home_team'][:16]} vs {p['away_team'][:16]}"
            odds  = p["pick_odds"] or 0
            stk   = p["stake"] or 0
            pick  = p["pick"] or "?"
            print(f"║  {start}  {name:<35}  {pick:>4} @{odds:.2f}  ×{stk:>6.2f}  ║")
    else:
        print(f"║  Нет открытых ставок                                              ║")
    print(f"╠{'═'*68}╣")

    # ── Последние settled ─────────────────────────────────────────────────────
    print(f"║  ПОСЛЕДНИЕ РЕЗУЛЬТАТЫ                                              ║")
    last_settled = sorted(settled, key=lambda p: p["settled_at"] or "", reverse=True)[:5]
    if last_settled:
        for p in last_settled:
            name  = f"{p['home_team'][:14]} vs {p['away_team'][:14]}"
            res   = p["result"] or "?"
            sym   = "✓" if p["correct"]==1 else ("✗" if p["correct"]==0 else "—")
            prof  = p["profit"]
            ps    = f"{prof:>+7.2f}" if prof is not None else "      —"
            clv_s = f"{p['clv']:>+.3f}" if p["clv"] else "    ?"
            rc_s  = "★" if p["rule_c"] else " "
            print(f"║  {rc_s} {name:<31}  {res:>4} {sym}  P&L{ps}  CLV{clv_s}  ║")
    else:
        print(f"║  Нет завершённых матчей                                           ║")
    print(f"╠{'═'*68}╣")

    # ── Все предикты (не Rule C) ──────────────────────────────────────────────
    non_rc_settled = [p for p in settled if p["rule_c"] == 0]
    if non_rc_settled:
        non_rc_wins = sum(1 for p in non_rc_settled if p["correct"]==1)
        wr2 = non_rc_wins/len(non_rc_settled)*100
        print(f"║  ВСЕ ПРЕДИКТЫ (без фильтра):  {len(non_rc_settled)} матчей  "
              f"WR: {wr2:.1f}%                     ║")
        print(f"╠{'═'*68}╣")

    # ── История банка (последние 5 строк) ────────────────────────────────────
    print(f"║  ИСТОРИЯ БАНКА (последние операции)                                ║")
    last_bank = bank_rows[-5:]
    for r in last_bank:
        amt = f"{r['amount']:>+8.2f}" if r["amount"] is not None else "        "
        bal = f"{r['balance']:>8.2f}"
        act = r["action"] or "?"
        note = (r["note"] or "")[:36]
        print(f"║  {r['ts'][5:16]:>11}  {act:>5}  {amt}  →{bal}  {note:<36}  ║")

    print(f"╚{'═'*68}╝")
    print(f"  Обновление каждые 30s  |  Ctrl+C для выхода  |  DB: {PRED_DB.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",     action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    db = open_db()
    try:
        # Пробуем инициализировать схему если DB пуста
        if db is None and PRED_DB.exists():
            db = sqlite3.connect(PRED_DB)
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.commit()
    except Exception:
        pass

    print_report(db)
    if args.once: return

    while True:
        try:
            time.sleep(args.interval)
            if db is None: db = open_db()
            print_report(db)
        except KeyboardInterrupt:
            print("\n  Остановлено.")
            break


if __name__ == "__main__":
    main()
