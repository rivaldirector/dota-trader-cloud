#!/usr/bin/env python3
"""
Edge table: match | model_prob | open_market_prob | close_market_prob | edge_open | edge_close | result

Логика:
  1. Walk-forward Elo по всем finished матчам (та же формула что в backtest)
  2. JOIN с odds_snapshots по match_external_id
  3. No-vig конвертация: p = (1/odd1) / (1/odd1 + 1/odd2)
  4. Вычисляем edge = model_prob - market_prob_novig
  5. Выводим таблицу + статистику

Запуск:
    PYTHONPATH=. python3 scripts/edge_report.py
    PYTHONPATH=. python3 scripts/edge_report.py --bookmaker Bet365
    PYTHONPATH=. python3 scripts/edge_report.py --min-edge 0.05 --csv
"""
from __future__ import annotations

import sys, argparse, sqlite3, csv, io
from collections import defaultdict
from math import pow
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

DB_PATH = PROJECT_ROOT / settings.database_path
K         = 32
START_ELO = 1500.0
PREFERRED_BM = ["Bet365", "Pinnacle", "GGBet", "FonBet"]


# ── Elo engine (walk-forward, same as backtest) ───────────────────────────────

def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def build_walkforward_predictions(conn: sqlite3.Connection) -> list[dict]:
    """
    Для каждого finished матча, предсказываем вероятность ДО обновления Elo.
    Возвращаем список {external_id, match_name, team_1, team_2,
                        model_prob_t1, result_t1, begin_at}.
    """
    rows = conn.execute("""
        SELECT external_id, name, begin_at,
               team_1_name, team_2_name, winner_name
        FROM matches
        WHERE status='finished'
          AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL
          AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()

    elo: dict[str, float] = defaultdict(lambda: START_ELO)
    games: dict[str, int] = defaultdict(int)
    predictions = []

    for r in rows:
        t1, t2, winner = r["team_1_name"], r["team_2_name"], r["winner_name"]
        e1 = elo[t1]
        e2 = elo[t2]

        prob_t1 = expected(e1, e2)
        result_t1 = 1 if winner == t1 else 0

        if games[t1] >= 5 and games[t2] >= 5:
            predictions.append({
                "external_id": r["external_id"],
                "match_name":  r["name"],
                "team_1":      t1,
                "team_2":      t2,
                "model_prob":  round(prob_t1, 4),
                "result":      result_t1,
                "begin_at":    r["begin_at"],
            })

        # Обновляем Elo
        s1 = result_t1
        ea = expected(e1, e2)
        elo[t1] = e1 + K * (s1 - ea)
        elo[t2] = e2 + K * ((1 - s1) - (1 - ea))
        games[t1] += 1
        games[t2] += 1

    return predictions


# ── Odds lookup ───────────────────────────────────────────────────────────────

def get_odds_for_matches(conn: sqlite3.Connection,
                         ext_ids: list[str],
                         preferred_bm: str = "Bet365") -> dict[str, dict]:
    """
    Для каждого external_id возвращаем:
      {open_home, open_away, close_home, close_away, bookmaker, team_1_name, team_2_name}

    Приоритет: preferred_bm → первый доступный с opening odds.
    captured_at заканчивается на _open или _close.
    """
    if not ext_ids:
        return {}

    placeholders = ",".join("?" * len(ext_ids))
    rows = conn.execute(f"""
        SELECT match_external_id, bookmaker, captured_at,
               team_1_name, team_2_name,
               team_1_odds, team_2_odds
        FROM odds_snapshots
        WHERE match_external_id IN ({placeholders})
          AND source = 'betsapi'
          AND team_1_odds IS NOT NULL
          AND team_2_odds IS NOT NULL
        ORDER BY match_external_id, bookmaker, captured_at
    """, ext_ids).fetchall()

    # Группируем по match_id → bookmaker → {open, close}
    by_match: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        mid = r["match_external_id"]
        bm  = r["bookmaker"]
        cap = r["captured_at"]
        tag = "open" if cap.endswith("_open") else "close"
        by_match[mid][bm][tag] = {
            "h": r["team_1_odds"],
            "a": r["team_2_odds"],
            "t1": r["team_1_name"],
            "t2": r["team_2_name"],
        }

    result = {}
    for mid, bm_dict in by_match.items():
        # Выбрать лучшего букмекера
        chosen_bm = None
        for bm in [preferred_bm] + PREFERRED_BM:
            if bm in bm_dict and "open" in bm_dict[bm]:
                chosen_bm = bm
                break
        if chosen_bm is None:
            for bm, data in bm_dict.items():
                if "open" in data:
                    chosen_bm = bm
                    break
        if chosen_bm is None:
            continue

        open_data  = bm_dict[chosen_bm].get("open", {})
        close_data = bm_dict[chosen_bm].get("close", open_data)

        result[mid] = {
            "bookmaker":  chosen_bm,
            "open_h":     open_data.get("h"),
            "open_a":     open_data.get("a"),
            "close_h":    close_data.get("h"),
            "close_a":    close_data.get("a"),
            "odds_t1":    open_data.get("t1", ""),
            "odds_t2":    open_data.get("t2", ""),
        }

    return result


def novig(odd_h: float, odd_a: float) -> tuple[float, float]:
    """No-vig вероятности: p = (1/o) / (1/o_h + 1/o_a)."""
    ih = 1.0 / odd_h
    ia = 1.0 / odd_a
    total = ih + ia
    return round(ih / total, 4), round(ia / total, 4)


def team_side(pred_t1: str, odds_t1: str, odds_t2: str) -> str:
    """Определяем сторону нашей команды team_1 в odds_snapshots."""
    def norm(s): return s.lower().strip()
    if norm(pred_t1) == norm(odds_t1):
        return "home"
    if norm(pred_t1) == norm(odds_t2):
        return "away"
    # Нечёткий поиск
    n = norm(pred_t1)
    n1 = norm(odds_t1)
    n2 = norm(odds_t2)
    if n in n1 or n1 in n:
        return "home"
    if n in n2 or n2 in n:
        return "away"
    return "home"  # default: считаем home = team_1


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bookmaker", default="Bet365",
                        help="Предпочтительный букмекер (default: Bet365)")
    parser.add_argument("--min-edge", type=float, default=0.0,
                        help="Минимальный |edge_open| для вывода")
    parser.add_argument("--csv", action="store_true",
                        help="Вывести CSV вместо таблицы")
    parser.add_argument("--top", type=int, default=50,
                        help="Сколько строк показать (0 = все)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("Строим walk-forward предсказания...", flush=True)
    preds = build_walkforward_predictions(conn)
    print(f"  Предсказаний: {len(preds)}", flush=True)

    ext_ids = [p["external_id"] for p in preds if p["external_id"]]
    print(f"Ищем odds для {len(ext_ids)} матчей...", flush=True)
    odds_map = get_odds_for_matches(conn, ext_ids, preferred_bm=args.bookmaker)
    print(f"  Найдено с odds: {len(odds_map)}", flush=True)

    conn.close()

    # Строим таблицу
    rows = []
    for p in preds:
        eid = p["external_id"]
        if eid not in odds_map:
            continue

        od = odds_map[eid]
        if not od["open_h"] or not od["open_a"]:
            continue

        side = team_side(p["team_1"], od["odds_t1"], od["odds_t2"])

        # No-vig prob для нашей стороны (team_1)
        open_p_h, open_p_a  = novig(od["open_h"],  od["open_a"])
        close_p_h, close_p_a = novig(od["close_h"], od["close_a"]) \
            if od["close_h"] and od["close_a"] else (open_p_h, open_p_a)

        open_mkt  = open_p_h  if side == "home" else open_p_a
        close_mkt = close_p_h if side == "home" else close_p_a

        edge_open  = round(p["model_prob"] - open_mkt,  4)
        edge_close = round(p["model_prob"] - close_mkt, 4)

        if abs(edge_open) < args.min_edge:
            continue

        rows.append({
            "begin_at":         p["begin_at"][:10],
            "match":            p["match_name"][:40],
            "team_1":           p["team_1"][:20],
            "team_2":           p["team_2"][:20],
            "model_prob":       p["model_prob"],
            "open_mkt":         open_mkt,
            "close_mkt":        close_mkt,
            "edge_open":        edge_open,
            "edge_close":       edge_close,
            "result":           p["result"],
            "bookmaker":        od["bookmaker"],
            "open_odds_t1":     od["open_h"] if side == "home" else od["open_a"],
            "close_odds_t1":    od["close_h"] if side == "home" else od["close_a"],
        })

    rows.sort(key=lambda r: r["begin_at"])

    if args.csv:
        out = io.StringIO()
        if rows:
            writer = csv.DictWriter(out, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(out.getvalue())
        return

    # Таблица
    display = rows[-args.top:] if args.top and len(rows) > args.top else rows
    hdr = (f"{'Date':10} {'Match':40} {'Mdl':5} {'Opn':5} {'Cls':5} "
           f"{'EdgeO':6} {'EdgeC':6} {'Res':3} {'BM':10}")
    print(f"\n{'='*len(hdr)}")
    print(hdr)
    print(f"{'='*len(hdr)}")
    for r in display:
        res_str = "WIN" if r["result"] == 1 else "los"
        eo = r["edge_open"]
        ec = r["edge_close"]
        eo_str = f"+{eo:.3f}" if eo >= 0 else f"{eo:.3f}"
        ec_str = f"+{ec:.3f}" if ec >= 0 else f"{ec:.3f}"
        print(f"{r['begin_at']:10} {r['match']:40} "
              f"{r['model_prob']:.3f} {r['open_mkt']:.3f} {r['close_mkt']:.3f} "
              f"{eo_str:6} {ec_str:6} {res_str:3} {r['bookmaker']:10}")

    # Статистика
    if not rows:
        print("\nНет матчей с odds. Сначала запусти backfill:")
        print("  PYTHONPATH=. python3 scripts/fetch_betsapi_history.py --pages 100")
        return

    n = len(rows)
    pos_edge = [r for r in rows if r["edge_open"] > 0]
    neg_edge = [r for r in rows if r["edge_open"] < 0]
    pos_wins  = sum(r["result"] for r in pos_edge)
    neg_wins  = sum(r["result"] for r in neg_edge)

    avg_edge_o = sum(r["edge_open"]  for r in rows) / n
    avg_edge_c = sum(r["edge_close"] for r in rows) / n

    print(f"\n{'='*len(hdr)}")
    print(f"  Всего матчей с odds: {n}")
    print(f"  Средний edge_open:  {avg_edge_o:+.4f}")
    print(f"  Средний edge_close: {avg_edge_c:+.4f}")
    print(f"")
    print(f"  Матчей с +edge_open:  {len(pos_edge):4}  WR={pos_wins/len(pos_edge):.3f}"
          if pos_edge else f"  Матчей с +edge_open:  0")
    print(f"  Матчей с -edge_open:  {len(neg_edge):4}  WR={neg_wins/len(neg_edge):.3f}"
          if neg_edge else f"  Матчей с -edge_open:  0")
    print(f"")

    # CLV (Closing Line Value) — если model ближе к close чем к open
    clv_pos = sum(1 for r in rows if abs(r["edge_close"]) < abs(r["edge_open"]))
    print(f"  CLV (edge_close < edge_open): {clv_pos}/{n} = {clv_pos/n:.1%}")
    print(f"  (>50% = модель опережает рынок на открытии)")

    print(f"\n  Вывод: показано {len(display)} из {n} строк")
    if args.top and len(rows) > args.top:
        print(f"  (последние {args.top} по дате; --top 0 чтобы показать все)")
    print(f"\nCSV: PYTHONPATH=. python3 scripts/edge_report.py --csv > edge.csv")
    print(f"{'='*len(hdr)}")


if __name__ == "__main__":
    main()
