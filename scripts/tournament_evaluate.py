#!/usr/bin/env python3
"""
tournament_evaluate.py
Шаг 3: Считаем P&L, ROI, win_rate, CLV для каждой стратегии.

Запуск:
    PYTHONPATH=. python3 scripts/tournament_evaluate.py
    PYTHONPATH=. python3 scripts/tournament_evaluate.py --split TEST
"""
from __future__ import annotations
import sqlite3, os, argparse, math, datetime

TOURN_DB   = os.path.join(os.path.dirname(__file__), "../data/model_tournament.db")
HARVEST_DB = os.path.join(os.path.dirname(__file__), "../storage/betsapi_harvest.db")

BANK_START = 1000.0
STAKE_FLAT = 20.0


RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tournament_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name  TEXT NOT NULL,
    division       TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    split          TEXT NOT NULL,
    bookmaker      TEXT,
    bet            INTEGER DEFAULT 0,
    bet_team       TEXT,
    odds           REAL DEFAULT 0,
    stake_usd      REAL DEFAULT 0,
    outcome        TEXT,       -- 'win' / 'loss' / 'void'
    pnl            REAL DEFAULT 0,
    market_prob    REAL DEFAULT 0,
    model_prob     REAL DEFAULT 0,
    edge           REAL DEFAULT 0,
    close_home     REAL,
    close_away     REAL,
    clv            REAL,       -- close odds prob - open odds prob (positive=good)
    reason_code    TEXT,
    UNIQUE(strategy_name, event_id, bookmaker)
);

CREATE TABLE IF NOT EXISTS tournament_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name  TEXT NOT NULL,
    division       TEXT NOT NULL,
    split          TEXT NOT NULL,
    total_bets     INTEGER DEFAULT 0,
    total_matches  INTEGER DEFAULT 0,
    wins           INTEGER DEFAULT 0,
    losses         INTEGER DEFAULT 0,
    voids          INTEGER DEFAULT 0,
    win_rate       REAL DEFAULT 0,
    total_stake    REAL DEFAULT 0,
    gross_pnl      REAL DEFAULT 0,
    roi_pct        REAL DEFAULT 0,
    avg_odds       REAL DEFAULT 0,
    avg_edge       REAL DEFAULT 0,
    avg_clv        REAL DEFAULT 0,
    bank_final     REAL DEFAULT 0,
    max_drawdown   REAL DEFAULT 0,
    sharpe         REAL DEFAULT 0,
    calculated_at  TEXT,
    UNIQUE(strategy_name, division, split)
);
"""


def get_winner(event_id: str, hcur) -> tuple:
    """Возвращает (winner: 'home'/'away'/None, close_home, close_away)."""
    row = hcur.execute("""
        SELECT score, winner
        FROM raw_events
        WHERE event_id = ?
        LIMIT 1
    """, (event_id,)).fetchone()

    winner = None
    if row:
        score_col, winner_col = row
        if winner_col and str(winner_col).strip() not in ('', 'null', 'None'):
            winner = str(winner_col).strip()
            # 'home'/'away' direct
            if winner.lower() in ('home', 'away'):
                winner = winner.lower()
            else:
                # может быть название команды — определяем по счёту
                winner = None

        if winner is None and score_col:
            s = str(score_col).strip().lower()
            if s in ('home', 'away'):
                winner = s
            elif s not in ('', '0-0', '1-1', '2-2', 'draw', 'null'):
                parts = s.split('-')
                if len(parts) == 2:
                    try:
                        h, a = int(parts[0]), int(parts[1])
                        if h > a: winner = 'home'
                        elif a > h: winner = 'away'
                    except: pass

    # Close odds для CLV
    close_row = hcur.execute("""
        SELECT close_home, close_away
        FROM odds_summary
        WHERE event_id = ? AND bookmaker = 'PinnacleSports' AND market = '151_1'
        LIMIT 1
    """, (event_id,)).fetchone()

    if close_row and close_row[0] and close_row[1]:
        return winner, float(close_row[0]), float(close_row[1])
    return winner, None, None


def calc_clv(bet_team: str, open_odds: float, close_home: float, close_away: float) -> float | None:
    """
    CLV = prob_close - prob_open (положительное = мы взяли значение).
    Ноविг-откорректированные вероятности.
    """
    if not close_home or not close_away or open_odds <= 1:
        return None
    # open prob (novig)
    if bet_team == 'home':
        raw_o = 1.0 / open_odds
        raw_other = 1.0 / (1.0 / (1.0 - 1.0 / open_odds)) if open_odds < 100 else 0
        # Используем только открытые котировки той же стороны
        # Для novig нужны обе стороны — берём из bm данных если есть, иначе NA
        return None  # упрощённо без novig CLV
    return None


def novig_prob(h_odds, a_odds):
    if not h_odds or not a_odds or h_odds <= 1 or a_odds <= 1:
        return None, None
    raw_h = 1.0 / h_odds
    raw_a = 1.0 / a_odds
    tot = raw_h + raw_a
    return raw_h / tot, raw_a / tot


def calc_clv_v2(bet_team: str, open_odds: float, open_other: float,
                close_home: float, close_away: float, is_home_bet: bool) -> float | None:
    try:
        # Open novig prob for bet side
        op_h, op_a = novig_prob(
            open_odds if is_home_bet else open_other,
            open_other if is_home_bet else open_odds
        )
        if op_h is None: return None
        open_prob = op_h if is_home_bet else op_a

        # Close novig prob for bet side
        cl_h, cl_a = novig_prob(close_home, close_away)
        if cl_h is None: return None
        close_prob = cl_h if is_home_bet else cl_a

        return close_prob - open_prob
    except:
        return None


def calc_sharpe(pnl_series: list[float]) -> float:
    if len(pnl_series) < 2:
        return 0.0
    n = len(pnl_series)
    mean = sum(pnl_series) / n
    var = sum((x - mean) ** 2 for x in pnl_series) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0
    return (mean / std) * math.sqrt(n) if std > 0 else 0.0


def calc_max_drawdown(cumulative: list[float]) -> float:
    peak = cumulative[0]
    mdd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        mdd = max(mdd, dd)
    return mdd


def evaluate(split_filter: str = 'all'):
    tcon = sqlite3.connect(TOURN_DB)
    hcon = sqlite3.connect(HARVEST_DB)
    hcur = hcon.cursor()

    tcon.executescript(
        "DROP TABLE IF EXISTS tournament_results;"
        "DROP TABLE IF EXISTS tournament_metrics;"
    )
    tcon.executescript(RESULTS_SCHEMA)
    tcon.commit()
    tcur = tcon.cursor()

    # Загружаем все ставки
    split_clause = "" if split_filter == 'all' else f"AND split='{split_filter}'"
    decisions = tcur.execute(f"""
        SELECT strategy_name, division, event_id, bookmaker, split,
               bet, bet_team, odds, stake_usd, reason_code,
               market_prob, model_prob, edge
        FROM tournament_decisions
        WHERE bet = 1
        {split_clause}
        ORDER BY strategy_name, division, event_id
    """).fetchall()

    print(f"Ставок для оценки: {len(decisions):,}", flush=True)

    # Открытые котировки для CLV (из odds_summary)
    open_odds_map: dict[str, tuple] = {}
    for eid, oh, oa in hcon.execute("""
        SELECT event_id, open_home, open_away
        FROM odds_summary
        WHERE bookmaker='PinnacleSports' AND market='151_1'
        AND open_home > 1 AND open_away > 1
    """).fetchall():
        open_odds_map[str(eid)] = (float(oh), float(oa))

    results_batch = []
    for row in decisions:
        (strat, division, event_id, bookmaker, split,
         bet, bet_team, odds, stake, reason,
         mkt_prob, mdl_prob, edge) = row

        winner, close_h, close_a = get_winner(str(event_id), hcur)

        # P&L
        if winner is None:
            outcome = 'void'
            pnl = 0.0
        elif bet_team == winner:
            outcome = 'win'
            pnl = (odds - 1.0) * stake
        else:
            outcome = 'loss'
            pnl = -stake

        # CLV
        clv = None
        open_h, open_a = open_odds_map.get(str(event_id), (None, None))
        if open_h and open_a:
            is_home = (bet_team == 'home')
            clv = calc_clv_v2(bet_team, odds, open_a if is_home else open_h,
                              close_h, close_a, is_home)

        results_batch.append((
            strat, division, str(event_id), split, bookmaker,
            bet, bet_team, odds, stake, outcome, pnl,
            mkt_prob, mdl_prob, edge, close_h, close_a, clv, reason
        ))

    tcur.executemany("""
        INSERT OR IGNORE INTO tournament_results
        (strategy_name,division,event_id,split,bookmaker,
         bet,bet_team,odds,stake_usd,outcome,pnl,
         market_prob,model_prob,edge,close_home,close_away,clv,reason_code)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, results_batch)
    tcon.commit()

    print(f"Результатов записано: {len(results_batch):,}")

    # ── Агрегированные метрики ────────────────────────────────────────────────
    now = datetime.datetime.utcnow().isoformat()
    metrics_batch = []

    strat_groups = tcur.execute("""
        SELECT DISTINCT strategy_name, division, split FROM tournament_results
        ORDER BY strategy_name, division, split
    """).fetchall()

    for strat, division, split in strat_groups:
        rows = tcur.execute("""
            SELECT outcome, pnl, odds, edge, clv, stake_usd
            FROM tournament_results
            WHERE strategy_name=? AND division=? AND split=?
            ORDER BY rowid ASC
        """, (strat, division, split)).fetchall()

        total_bets = len(rows)
        wins = sum(1 for r in rows if r[0] == 'win')
        losses = sum(1 for r in rows if r[0] == 'loss')
        voids = sum(1 for r in rows if r[0] == 'void')
        win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0
        total_stake = sum(r[5] for r in rows if r[0] != 'void')
        gross_pnl = sum(r[1] for r in rows)
        roi = (gross_pnl / total_stake * 100) if total_stake > 0 else 0
        avg_odds = sum(r[2] for r in rows if r[2] > 0) / total_bets if total_bets > 0 else 0
        avg_edge = sum(r[3] for r in rows if r[3]) / total_bets if total_bets > 0 else 0
        clv_vals = [r[4] for r in rows if r[4] is not None]
        avg_clv = sum(clv_vals) / len(clv_vals) if clv_vals else 0

        # Total matches in this division/split
        total_m_row = tcur.execute("""
            SELECT COUNT(*) FROM tournament_decisions
            WHERE strategy_name=? AND division=? AND split=?
        """, (strat, division, split)).fetchone()
        total_matches = total_m_row[0] if total_m_row else 0

        # Cumulative P&L for drawdown/sharpe
        pnl_series = [r[1] for r in rows]
        cum = []
        running = BANK_START
        for p in pnl_series:
            running += p
            cum.append(running)
        bank_final = cum[-1] if cum else BANK_START
        mdd = calc_max_drawdown(cum) if cum else 0.0
        sharpe = calc_sharpe(pnl_series) if pnl_series else 0.0

        metrics_batch.append((
            strat, division, split,
            total_bets, total_matches, wins, losses, voids,
            round(win_rate, 4), round(total_stake, 2), round(gross_pnl, 2),
            round(roi, 2), round(avg_odds, 4), round(avg_edge, 4),
            round(avg_clv, 4), round(bank_final, 2),
            round(mdd * 100, 2), round(sharpe, 4), now
        ))

    tcur.executemany("""
        INSERT OR REPLACE INTO tournament_metrics
        (strategy_name,division,split,
         total_bets,total_matches,wins,losses,voids,
         win_rate,total_stake,gross_pnl,roi_pct,avg_odds,avg_edge,
         avg_clv,bank_final,max_drawdown,sharpe,calculated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, metrics_batch)
    tcon.commit()

    # Быстрая сводка
    print(f"\n{'='*80}")
    print(f"{'STRATEGY':<22} {'DIV':<4} {'SPLIT':<6} {'BETS':>5} {'WIN%':>6} {'ROI%':>7} {'P&L':>8} {'SHARPE':>7}")
    print(f"{'-'*80}")
    for row in tcur.execute("""
        SELECT strategy_name, division, split,
               total_bets, win_rate, roi_pct, gross_pnl, sharpe
        FROM tournament_metrics
        ORDER BY split, roi_pct DESC
    """).fetchall():
        strat, div, spl, bets, wr, roi, pnl, sh = row
        print(f"  {strat:<20} {div:<4} {spl:<6} {bets:>5} {wr*100:>5.1f}% {roi:>6.1f}% {pnl:>8.2f} {sh:>7.3f}")
    print(f"{'='*80}")

    tcon.close()
    hcon.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=['all', 'TRAIN', 'VAL', 'TEST'])
    args = ap.parse_args()
    evaluate(args.split)
