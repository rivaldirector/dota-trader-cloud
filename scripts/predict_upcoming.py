#!/usr/bin/env python3
"""
Live edge: предстоящие Dota 2 матчи + текущие odds + model_prob → edge прямо сейчас.

Что делает:
  1. Берёт upcoming Dota 2 матчи из BetsAPI (реальное время)
  2. Считает model_prob через walk-forward Elo (обученный на всей истории)
  3. Сравнивает с текущими odds по всем букмекерам
  4. Выводит таблицу: match | model | market | edge | odds | stake_kelly

Запуск:
    PYTHONPATH=. python3 scripts/predict_upcoming.py
    PYTHONPATH=. python3 scripts/predict_upcoming.py --min-edge 0.05
    PYTHONPATH=. python3 scripts/predict_upcoming.py --all-bookmakers
"""
from __future__ import annotations

import sys, argparse, sqlite3
from collections import defaultdict
from math import pow, log
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from adapters.betsapi import BetsAPIClient, _extract_moneyline, _is_dota2

DB_PATH = PROJECT_ROOT / settings.database_path
K         = 32
START_ELO = 1500.0
PREFERRED_BM = ["Bet365", "Pinnacle", "GGBet", "FonBet", "YSB88"]
BANKROLL  = settings.start_bank


# ── Elo (обучаем на всей истории) ────────────────────────────────────────────

def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def build_elo(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, int]]:
    rows = conn.execute("""
        SELECT team_1_name, team_2_name, winner_name
        FROM matches
        WHERE status='finished'
          AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL
          AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()

    elo: dict[str, float]  = defaultdict(lambda: START_ELO)
    games: dict[str, int]  = defaultdict(int)

    for r in rows:
        t1, t2, winner = r[0], r[1], r[2]
        e1, e2 = elo[t1], elo[t2]
        ea = expected(e1, e2)
        s1 = 1 if winner == t1 else 0
        elo[t1] = e1 + K * (s1 - ea)
        elo[t2] = e2 + K * ((1 - s1) - (1 - ea))
        games[t1] += 1
        games[t2] += 1

    return dict(elo), dict(games)


def model_prob(elo: dict, games: dict, t1: str, t2: str) -> tuple[float, int]:
    """Возвращает (prob_t1_wins, min_games)."""
    e1 = elo.get(t1, START_ELO)
    e2 = elo.get(t2, START_ELO)
    g1 = games.get(t1, 0)
    g2 = games.get(t2, 0)
    return expected(e1, e2), min(g1, g2)


# ── Fuzzy team match ──────────────────────────────────────────────────────────

def norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_in_elo(elo: dict, name: str) -> str | None:
    n = norm(name)
    for k in elo:
        if norm(k) == n:
            return k
    for k in elo:
        nk = norm(k)
        if n in nk or nk in n:
            return k
    return None


# ── Kelly stake ───────────────────────────────────────────────────────────────

def kelly(prob: float, odds: float, fraction: float = 0.25) -> float:
    """Дробный Kelly (fraction=0.25 = четверть Kelly)."""
    edge = prob * odds - 1.0
    if edge <= 0:
        return 0.0
    k = edge / (odds - 1.0)
    return round(k * fraction * 100, 2)  # % от банка


# ── No-vig ────────────────────────────────────────────────────────────────────

def novig(oh: float, oa: float) -> tuple[float, float]:
    ih, ia = 1.0 / oh, 1.0 / oa
    t = ih + ia
    return round(ih / t, 4), round(ia / t, 4)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-edge", type=float, default=0.0,
                        help="Минимальный edge для вывода (default: 0)")
    parser.add_argument("--min-games", type=int, default=10,
                        help="Минимум матчей у каждой команды (default: 10)")
    parser.add_argument("--all-bookmakers", action="store_true",
                        help="Показать все букмекеры, не только лучший")
    args = parser.parse_args()

    # 1. Строим Elo по истории
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("Строим Elo по истории...", flush=True)
    elo, games = build_elo(conn)
    conn.close()
    print(f"  Команд в рейтинге: {len(elo)}", flush=True)

    # 2. Берём upcoming Dota 2 из BetsAPI
    print("Запрашиваем upcoming Dota 2 из BetsAPI...", flush=True)
    client = BetsAPIClient()
    events = client.get_upcoming_dota2()
    print(f"  Матчей: {len(events)}", flush=True)

    if not events:
        print("\nНет предстоящих Dota 2 матчей.")
        return

    # 3. Для каждого матча — odds + model_prob
    rows = []
    for event in events:
        home = event.get("home", {}).get("name", "")
        away = event.get("away", {}).get("name", "")
        league = event.get("league", {}).get("name", "")
        event_time = event.get("time", "")

        # Время матча
        try:
            dt = datetime.fromtimestamp(int(event_time), tz=timezone.utc)
            time_str = dt.strftime("%m-%d %H:%M")
        except Exception:
            time_str = "?"

        # Fuzzy match к нашему Elo
        t1 = find_in_elo(elo, home)
        t2 = find_in_elo(elo, away)

        if t1 is None or t2 is None:
            prob_t1 = 0.5
            min_g = 0
            model_known = False
        else:
            prob_t1, min_g = model_prob(elo, games, t1, t2)
            model_known = True

        # Odds
        try:
            summary = client.get_odds_summary(str(event.get("id", "")))
            bms = _extract_moneyline(summary)
        except Exception:
            bms = []

        if not bms:
            # Нет odds, но показываем матч
            rows.append({
                "time": time_str,
                "home": home, "away": away, "league": league,
                "prob_home": prob_t1,
                "min_games": min_g,
                "known": model_known,
                "bookmaker": "-",
                "open_h": None, "open_a": None,
                "mkt_home": None,
                "edge": None,
                "odds_home": None,
                "kelly_pct": 0,
            })
            continue

        # Выбираем букмекера
        chosen = None
        for pref in PREFERRED_BM:
            match = next((b for b in bms if b["bookmaker"] == pref), None)
            if match:
                chosen = match
                break
        if chosen is None:
            chosen = bms[0]

        oh = chosen["close_home"]  # текущие (close = последние)
        oa = chosen["close_away"]
        mkt_h, mkt_a = novig(oh, oa)
        edge = round(prob_t1 - mkt_h, 4) if model_known else None
        k_pct = kelly(prob_t1, oh) if model_known and edge and edge > 0 else 0.0

        rows.append({
            "time": time_str,
            "home": home, "away": away, "league": league,
            "prob_home": prob_t1,
            "min_games": min_g,
            "known": model_known,
            "bookmaker": chosen["bookmaker"],
            "open_h": oh, "open_a": oa,
            "mkt_home": mkt_h,
            "edge": edge,
            "odds_home": oh,
            "kelly_pct": k_pct,
        })

        if args.all_bookmakers and len(bms) > 1:
            for bm in bms[1:]:
                oh2, oa2 = bm["close_home"], bm["close_away"]
                mh2, _ = novig(oh2, oa2)
                e2 = round(prob_t1 - mh2, 4) if model_known else None
                k2 = kelly(prob_t1, oh2) if model_known and e2 and e2 > 0 else 0.0
                rows.append({
                    "time": time_str,
                    "home": home, "away": away, "league": league,
                    "prob_home": prob_t1,
                    "min_games": min_g,
                    "known": model_known,
                    "bookmaker": bm["bookmaker"],
                    "open_h": oh2, "open_a": oa2,
                    "mkt_home": mh2,
                    "edge": e2,
                    "odds_home": oh2,
                    "kelly_pct": k2,
                })

    # Фильтр по min-edge
    if args.min_edge > 0:
        rows = [r for r in rows if r["edge"] is not None and r["edge"] >= args.min_edge]

    # Сортируем: сначала с edge, потом по времени
    rows.sort(key=lambda r: (-(r["edge"] or -99), r["time"]))

    # Вывод
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*90}")
    print(f"  UPCOMING DOTA 2 — LIVE EDGE   [{now_str}]")
    print(f"  Матчей: {len(events)}   Команд в Elo: {len(elo)}")
    print(f"{'='*90}")
    print(f"{'Time':8} {'Home':22} {'Away':22} {'Mdl':5} {'Mkt':5} {'Edge':6} "
          f"{'Odds':5} {'Kelly%':6} {'BM':10} {'Games':5}")
    print(f"{'-'*90}")

    value_count = 0
    for r in rows:
        mdl  = f"{r['prob_home']:.3f}" if r["known"] else "  ?  "
        mkt  = f"{r['mkt_home']:.3f}"  if r["mkt_home"] else "  ?  "
        edge = r["edge"]
        if edge is not None:
            edge_str = f"{edge:+.3f}"
            if edge >= 0.05:
                edge_str = f"★{edge_str}"
                value_count += 1
            elif edge > 0:
                edge_str = f"+{edge_str[1:]}"
        else:
            edge_str = "  ?  "
        odds_str  = f"{r['odds_home']:.2f}" if r["odds_home"] else "  ?"
        kelly_str = f"{r['kelly_pct']:.1f}%" if r["kelly_pct"] > 0 else "  -"
        games_str = str(r["min_games"]) if r["known"] else "new"

        print(f"{r['time']:8} {r['home']:22} {r['away']:22} "
              f"{mdl:5} {mkt:5} {edge_str:7} "
              f"{odds_str:5} {kelly_str:6} {r['bookmaker']:10} {games_str:5}")

    print(f"{'-'*90}")
    print(f"  Матчей с edge ≥ 5%: {value_count}")
    if args.min_games > 0:
        low_data = [r for r in rows if r["min_games"] < args.min_games and r["known"]]
        if low_data:
            print(f"  ⚠ {len(low_data)} матчей с <{args.min_games} игр у команды (низкая надёжность)")
    print(f"\n  Kelly рассчитан от ${BANKROLL:.0f} (¼ Kelly, только для home стороны)")
    print(f"  Stake в $: Kelly% × ${BANKROLL:.0f} / 100")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
