#!/usr/bin/env python3
"""
Stress test для стратегии: edge_adj > 0 AND elo_diff >= 150 AND bet_odds < 2.0

Анализы:
  1. Threshold sweep elo_diff: 50 75 100 125 150 175 200 225 250
  2. Bootstrap stability: 10 000 resamples, распределение ROI
  3. Leave-one-month-out CV
  4. League breakdown (лиги с n>=5 в сегменте)
  5. Region breakdown (WEU / EEU / SEA / CN / SA / NA / Global)

Запуск:
    PYTHONPATH=. python3 scripts/stress_test.py
    PYTHONPATH=. python3 scripts/stress_test.py --no-plot   (без matplotlib)
"""
from __future__ import annotations

import sys, re, sqlite3, random, argparse
from collections import defaultdict
from datetime import datetime, timezone
from math import pow
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from models.team_rating import _tier_k, _time_weight, BASE_K

DB_PATH      = PROJECT_ROOT / settings.database_path
START_ELO    = 1500.0
MIN_GAMES    = 3          # min матчей у каждой команды чтобы включить в анализ
H2H_MAX_W    = 0.40       # максимальный вес H2H
H2H_CONF_N   = 5.0        # при скольких H2H встречах вес достигает максимума
SEED         = 42
N_BOOTSTRAP  = 10_000

# ── helpers ──────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def team_match(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

def novig(h: float, a: float):
    if not h or not a or h <= 1 or a <= 1:
        return None, None
    ph, pa = 1/h, 1/a
    s = ph + pa
    return ph/s, pa/s

def get_mp_for_team1(m_t1, m_t2, o_t1, o_t2, odds1, odds2):
    """Возвращает (mp_team1, mp_team2, valid)."""
    if team_match(m_t1, o_t1) and team_match(m_t2, o_t2):
        p1, p2 = novig(odds1, odds2)
        return p1, p2, (p1 is not None)
    if team_match(m_t1, o_t2) and team_match(m_t2, o_t1):
        p2, p1 = novig(odds1, odds2)
        return p1, p2, (p1 is not None)
    return None, None, False

def elo_prob(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))

def bootstrap_ci(values: list[float], n: int = N_BOOTSTRAP,
                 alpha: float = 0.05, seed: int = SEED):
    rng = random.Random(seed)
    means = [
        sum(rng.choices(values, k=len(values))) / len(values)
        for _ in range(n)
    ]
    means.sort()
    lo = means[int(alpha/2 * n)]
    hi = means[int((1 - alpha/2) * n)]
    return lo, hi, means

def stats(bets: list[dict]) -> dict | None:
    if not bets:
        return None
    n       = len(bets)
    wins    = sum(b["win"] for b in bets)
    profits = [b["profit"] for b in bets]
    roi     = sum(profits) / n
    clv_pos = sum(1 for b in bets if b["clv"] > 0)
    avg_clv = sum(b["clv"] for b in bets) / n
    # accuracy
    acc_our = wins / n
    acc_mkt = sum(b["mkt_correct"] for b in bets) / n
    # max drawdown
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in profits:
        cum += p
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    return dict(n=n, wr=wins/n, roi=roi, clv_pos=clv_pos/n,
                avg_clv=avg_clv, acc_our=acc_our, acc_mkt=acc_mkt,
                max_dd=max_dd, profits=profits)


# ── Region mapping ────────────────────────────────────────────────────────────

REGION_MAP = {
    # WEU
    "european pro league": "WEU",
    "epl championship":    "WEU",
    "epl world series":    "WEU",  # generic — override below for SEA/SA/NA
    "blast slam":          "WEU",
    "winline":             "WEU",
    "betboom":             "EEU",
    "1win":                "EEU",
    "cis":                 "EEU",
    "dreamleague quals - weu": "WEU",
    "dreamleague quals - eeu": "EEU",
    "dreamleague quals - sea": "SEA",
    "dreamleague quals - cn":  "CN",
    "dreamleague quals - sa":  "SA",
    "dreamleague quals - na":  "NA",
    "epl world series sea":    "SEA",
    "esl challenger china":    "CN",
    "acl":                     "CN",
    "cct":                     "SA",
    "ewc quals sa":            "SA",
    "ewc quals eu":            "WEU",
    "ewc quals meswa":         "Global",
    "esports world cup":       "Global",
    "the international":       "Global",
    "esl one":                 "Global",
    "dreamleague":             "Global",   # main event = mixed regions
    "pgl":                     "Global",
    "wallachia":               "Global",
    "lunar":                   "CN",
    "fissure":                 "CN",
    "tennisi":                 "EEU",
    "premier series":          "EEU",
}

def infer_region(league_name: str) -> str:
    ln = (league_name or "").lower()
    # EPL World Series regional variants
    if "epl world series" in ln:
        if "sea" in ln:  return "SEA"
        if "sa" in ln:   return "SA"
        if "na" in ln:   return "NA"
        if "am" in ln:   return "SA"
        return "WEU"
    for key, region in REGION_MAP.items():
        if key in ln:
            return region
    return "Other"


# ── Core pipeline: build all bets ─────────────────────────────────────────────

def build_all_bets(conn: sqlite3.Connection) -> list[dict]:
    """
    Walk-forward Elo + H2H + odds alignment.
    Возвращает список всех матчей с достаточно данных и odds.
    Каждый элемент: {eid, begin_at, month, league, region, team1, team2,
                     elo1, elo2, elo_diff, elo_prob,
                     h2h_n, h2h_wr_d, h2h_conf, adj_prob,
                     mp_open, mp_close, edge_adj,
                     bet_odds_t1, result, bookmaker}
    """
    print("Загружаем матчи...", flush=True)
    matches = conn.execute("""
        SELECT external_id, name, league_name, begin_at,
               team_1_name, team_2_name, winner_name
        FROM matches
        WHERE status='finished'
          AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL
          AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()
    print(f"  Матчей: {len(matches)}", flush=True)

    print("Загружаем odds...", flush=True)
    # Загружаем все Dota odds snapshots сразу
    snaps = conn.execute("""
        SELECT match_external_id, bookmaker, captured_at,
               team_1_name, team_2_name, team_1_odds, team_2_odds
        FROM odds_snapshots
        WHERE league_name LIKE 'DOTA2%'
          AND team_1_odds IS NOT NULL AND team_2_odds IS NOT NULL
          AND team_1_odds > 1 AND team_2_odds > 1
        ORDER BY match_external_id, bookmaker
    """).fetchall()
    print(f"  Snapshots: {len(snaps)}", flush=True)

    # Индексируем odds: match_id → bm → {open, close}
    PREFERRED = ["Bet365", "Pinnacle", "GGBet", "10Bet", "188Bet", "FonBet"]
    odds_idx: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    for s in snaps:
        mid = s["match_external_id"]
        bm  = s["bookmaker"]
        tag = "open" if s["captured_at"].endswith("_open") else "close"
        odds_idx[mid][bm][tag] = {
            "h": s["team_1_odds"], "a": s["team_2_odds"],
            "t1": s["team_1_name"], "t2": s["team_2_name"],
        }

    def best_odds(mid: str):
        if mid not in odds_idx:
            return None
        bm_data = odds_idx[mid]
        chosen = None
        for bm in PREFERRED:
            if bm in bm_data and "open" in bm_data[bm]:
                chosen = bm; break
        if not chosen:
            for bm, d in bm_data.items():
                if "open" in d:
                    chosen = bm; break
        if not chosen:
            return None
        od = bm_data[chosen]
        return {
            "bm":      chosen,
            "open_h":  od["open"].get("h"),  "open_a":  od["open"].get("a"),
            "close_h": od.get("close", od["open"]).get("h"),
            "close_a": od.get("close", od["open"]).get("a"),
            "t1":      od["open"]["t1"],      "t2":      od["open"]["t2"],
        }

    # Walk-forward state
    now_dt  = datetime.now(timezone.utc)
    elo: dict[str, float] = defaultdict(lambda: START_ELO)
    games: dict[str, int] = defaultdict(int)
    # H2H: (team_a, team_b) → list of (timestamp, win_for_a)
    h2h: dict[tuple, list] = defaultdict(list)

    results = []
    no_odds = 0

    for r in matches:
        t1  = r["team_1_name"]
        t2  = r["team_2_name"]
        win = r["winner_name"]
        eid = str(r["external_id"])
        ln  = r["league_name"] or ""
        bat = r["begin_at"] or ""

        e1, e2   = elo[t1], elo[t2]
        ep       = elo_prob(e1, e2)
        elo_diff = abs(e1 - e2)
        g1, g2   = games[t1], games[t2]
        result   = 1 if win == t1 else 0

        # H2H (decay-weighted) — до предсказания
        key = (min(t1,t2), max(t1,t2))
        h2h_entries = h2h[key]
        if h2h_entries:
            try:
                bat_ts = datetime.fromisoformat(bat.replace("Z", "+00:00")).timestamp()
            except Exception:
                bat_ts = now_dt.timestamp()
            w_wins = w_total = 0.0
            for (ts, win_t1) in h2h_entries:
                w = 0.5 ** ((bat_ts - ts) / 365 / 86400)
                w_total += w
                if (win_t1 and t1 < t2) or (not win_t1 and t1 >= t2):
                    # win for t1 in our perspective
                    if (win_t1 and t1 == min(t1,t2)) or (not win_t1 and t1 != min(t1,t2)):
                        w_wins += w
            # Re-do properly
            w_wins = 0.0
            for (ts, w1_wins) in h2h_entries:
                w = 0.5 ** ((bat_ts - ts) / 365 / 86400)
                w_total += 0  # already counted above
                if w1_wins:
                    w_wins += w
            # h2h_entries stores (ts, t1_won_flag) where t1 = min(t1,t2) = key[0]
            # so w1_wins means key[0] won
            # for our t1: if t1 == key[0], wins when key[0] won
            w_wins2 = 0.0
            w_tot2  = 0.0
            for (ts, k0_won) in h2h_entries:
                w = 0.5 ** ((bat_ts - ts) / 365 / 86400)
                w_tot2 += w
                if (k0_won and t1 == key[0]) or (not k0_won and t1 == key[1]):
                    w_wins2 += w
            h2h_n      = len(h2h_entries)
            h2h_wr_d   = w_wins2 / w_tot2 if w_tot2 > 0 else 0.5
        else:
            h2h_n    = 0
            h2h_wr_d = 0.5

        h2h_conf = min(h2h_n / H2H_CONF_N, 1.0)
        adj_prob = ep * (1 - H2H_MAX_W * h2h_conf) + h2h_wr_d * (H2H_MAX_W * h2h_conf)

        # Odds
        if g1 >= MIN_GAMES and g2 >= MIN_GAMES:
            od = best_odds(eid)
            if od:
                mp1_open, mp2_open, valid = get_mp_for_team1(
                    t1, t2, od["t1"], od["t2"], od["open_h"], od["open_a"])
                mp1_close, _, _ = get_mp_for_team1(
                    t1, t2, od["t1"], od["t2"],
                    od["close_h"] or od["open_h"], od["close_a"] or od["open_a"])
                if valid and mp1_open is not None:
                    edge_adj = adj_prob - mp1_open
                    # Ставим на t1 если edge_adj > 0 (наша вероятность выше рынка)
                    bet_on_t1 = True  # edge_adj > 0 means we bet on t1
                    bet_odds  = od["open_h"] if team_match(t1, od["t1"]) else od["open_a"]
                    month     = bat[:7] if bat else "unknown"
                    region    = infer_region(ln)

                    results.append(dict(
                        eid=eid, begin_at=bat, month=month,
                        league=ln, region=region,
                        team1=t1, team2=t2,
                        elo1=round(e1,1), elo2=round(e2,1),
                        elo_diff=round(elo_diff,1),
                        elo_prob=round(ep,4),
                        h2h_n=h2h_n, h2h_wr_d=round(h2h_wr_d,4),
                        h2h_conf=round(h2h_conf,4),
                        adj_prob=round(adj_prob,4),
                        mp_open=round(mp1_open,4),
                        mp_close=round(mp1_close,4) if mp1_close else round(mp1_open,4),
                        edge_adj=round(edge_adj,4),
                        bet_odds=round(bet_odds,3),
                        result=result,
                        bookmaker=od["bm"],
                    ))
                else:
                    no_odds += 1
            else:
                no_odds += 1

        # --- Update state AFTER prediction ---
        k   = _tier_k(ln)
        w   = _time_weight(bat, now_dt, 365)
        kef = k * w
        exp1 = elo_prob(e1, e2)
        elo[t1] = e1 + kef * (result - exp1)
        elo[t2] = e2 + kef * ((1-result) - (1-exp1))
        games[t1] += 1
        games[t2] += 1

        # H2H update: store (ts, key0_won)
        try:
            ts = datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except Exception:
            ts = now_dt.timestamp()
        k0_won = (win == key[0])
        h2h[key].append((ts, k0_won))

    print(f"  Всего в анализе: {len(results)} (no_odds: {no_odds})", flush=True)
    return results


def compute_bet(b: dict) -> dict:
    """Вычисляет profit/clv/win для одной строки."""
    bet_on_t1 = b["edge_adj"] > 0
    result     = b["result"]
    bet_odds   = b["bet_odds"]
    mp_open    = b["mp_open"]
    mp_close   = b["mp_close"]

    win    = (result == 1) if bet_on_t1 else (result == 0)
    profit = (bet_odds - 1) if win else -1.0
    clv    = (mp_close - mp_open) if bet_on_t1 else (mp_open - mp_close)
    # market accuracy: did the market favorite (mp_open > 0.5) win?
    mkt_fav_t1 = mp_open > 0.5
    mkt_correct = int((mkt_fav_t1 and result==1) or (not mkt_fav_t1 and result==0))

    return dict(win=int(win), profit=profit, clv=clv, mkt_correct=mkt_correct)


# ── Analysis 1: Threshold sweep ───────────────────────────────────────────────

def analysis_threshold_sweep(all_rows: list[dict]):
    thresholds = [50, 75, 100, 125, 150, 175, 200, 225, 250]
    print("\n" + "="*90)
    print("АНАЛИЗ 1: THRESHOLD SWEEP elo_diff (edge_adj > 0, odds < 2.0)")
    print("="*90)
    hdr = f"{'elo_diff':>10}  {'n':>5}  {'ROI':>7}  {'CI_lo':>7}  {'CI_hi':>7}  {'CLV+':>6}  {'avgCLV':>7}  {'acc_our':>8}  {'acc_mkt':>8}"
    print(hdr)
    print("-"*90)
    for thr in thresholds:
        bets = []
        for b in all_rows:
            if b["edge_adj"] > 0 and b["elo_diff"] >= thr and b["bet_odds"] < 2.0:
                cb = compute_bet(b)
                bets.append(cb)
        if not bets:
            print(f"  >= {thr:<6}  {'0':>5}")
            continue
        s = stats(bets)
        if len(s["profits"]) < 5:
            ci_lo, ci_hi = float("nan"), float("nan")
        else:
            ci_lo, ci_hi, _ = bootstrap_ci(s["profits"], n=5000)
        star = " ✓" if ci_lo > 0 else "  "
        print(f"  >= {thr:<6}  {s['n']:>5}  {s['roi']:>+7.3f}  {ci_lo:>+7.3f}  {ci_hi:>+7.3f}  "
              f"{s['clv_pos']:>6.3f}  {s['avg_clv']:>+7.4f}  {s['acc_our']:>8.3f}  {s['acc_mkt']:>8.3f}{star}")


# ── Analysis 2: Bootstrap stability ──────────────────────────────────────────

def analysis_bootstrap(all_rows: list[dict]):
    print("\n" + "="*90)
    print("АНАЛИЗ 2: BOOTSTRAP STABILITY (10 000 resamples, locked rule: edge_adj>0 & elo_diff>=150 & odds<2.0)")
    print("="*90)

    bets = []
    for b in all_rows:
        if b["edge_adj"] > 0 and b["elo_diff"] >= 150 and b["bet_odds"] < 2.0:
            bets.append(compute_bet(b))

    if not bets:
        print("  Нет ставок в сегменте!")
        return

    profits = [b["profit"] for b in bets]
    n = len(profits)
    roi = sum(profits) / n
    lo, hi, means = bootstrap_ci(profits, n=N_BOOTSTRAP)

    # Percentiles
    means_s = sorted(means)
    p5  = means_s[int(0.05  * N_BOOTSTRAP)]
    p10 = means_s[int(0.10  * N_BOOTSTRAP)]
    p25 = means_s[int(0.25  * N_BOOTSTRAP)]
    p50 = means_s[int(0.50  * N_BOOTSTRAP)]
    p75 = means_s[int(0.75  * N_BOOTSTRAP)]
    p90 = means_s[int(0.90  * N_BOOTSTRAP)]
    p95 = means_s[int(0.95  * N_BOOTSTRAP)]
    prob_pos = sum(1 for m in means if m > 0) / N_BOOTSTRAP

    print(f"\n  n = {n}, observed ROI = {roi:+.4f}")
    print(f"  95% CI = [{lo:+.4f}, {hi:+.4f}]  {'✓ CI > 0' if lo > 0 else '✗ CI includes 0'}")
    print(f"\n  Bootstrap distribution ({N_BOOTSTRAP:,} resamples):")
    print(f"    P(ROI > 0)  = {prob_pos:.1%}")
    print(f"    p5  = {p5:+.4f}")
    print(f"    p10 = {p10:+.4f}")
    print(f"    p25 = {p25:+.4f}")
    print(f"    p50 = {p50:+.4f}  (median)")
    print(f"    p75 = {p75:+.4f}")
    print(f"    p90 = {p90:+.4f}")
    print(f"    p95 = {p95:+.4f}")

    # ASCII histogram
    bins = 20
    min_v = means_s[0]
    max_v = means_s[-1]
    width = (max_v - min_v) / bins if max_v > min_v else 1
    counts = [0] * bins
    for m in means:
        idx = min(int((m - min_v) / width), bins - 1)
        counts[idx] += 1
    max_c = max(counts) if counts else 1
    bar_w = 40
    print(f"\n  Гистограмма bootstrap ROI:")
    zero_line = int((0.0 - min_v) / width) if min_v < 0 < max_v else -1
    for i, c in enumerate(counts):
        lo_b = min_v + i * width
        hi_b = lo_b + width
        bar  = "█" * int(c / max_c * bar_w)
        mark = " ← 0" if i == zero_line else ""
        print(f"  {lo_b:+.3f} to {hi_b:+.3f} | {bar}{mark}")


# ── Analysis 3: Leave-one-month-out ──────────────────────────────────────────

def analysis_lomo(all_rows: list[dict]):
    print("\n" + "="*90)
    print("АНАЛИЗ 3: LEAVE-ONE-MONTH-OUT (locked rule: edge_adj>0 & elo_diff>=150 & odds<2.0)")
    print("="*90)
    print("  Логика: модель фиксирована, тестируем каждый месяц как holdout.\n")

    # Собираем сегментные ставки
    seg_bets = []
    for b in all_rows:
        if b["edge_adj"] > 0 and b["elo_diff"] >= 150 and b["bet_odds"] < 2.0:
            cb = compute_bet(b)
            cb["month"] = b["month"]
            seg_bets.append(cb)

    if not seg_bets:
        print("  Нет ставок!")
        return

    months = sorted(set(b["month"] for b in seg_bets))
    hdr = f"  {'Month':>8}  {'n_test':>7}  {'ROI_test':>9}  {'CI_test':>18}  {'CLV+':>6}  {'n_train':>8}  {'ROI_train':>10}"
    print(hdr)
    print("  " + "-"*85)

    for m in months:
        test  = [b for b in seg_bets if b["month"] == m]
        train = [b for b in seg_bets if b["month"] != m]
        if not test:
            continue

        # Test stats
        t_s = stats(test)
        if len(test) >= 5:
            tlo, thi, _ = bootstrap_ci([b["profit"] for b in test], n=2000)
            ci_str = f"[{tlo:+.3f},{thi:+.3f}]"
        else:
            ci_str = "  (n<5)   "

        # Train stats
        tr_s = stats(train) if train else None
        tr_roi = f"{tr_s['roi']:+.3f}" if tr_s else "  n/a"

        star = " ✓" if (len(test) >= 5 and tlo > 0) else "  "
        print(f"  {m:>8}  {t_s['n']:>7}  {t_s['roi']:>+9.3f}  {ci_str:>18}  "
              f"{t_s['clv_pos']:>6.3f}  {len(train):>8}  {tr_roi:>10}{star}")

    # Overall
    all_s = stats(seg_bets)
    print(f"\n  Overall:  n={all_s['n']}  ROI={all_s['roi']:+.4f}  CLV+={all_s['clv_pos']:.3f}")
    agg_lo, agg_hi, _ = bootstrap_ci(all_s["profits"])
    print(f"  95% CI = [{agg_lo:+.4f}, {agg_hi:+.4f}]  {'✓' if agg_lo > 0 else '✗'}")


# ── Analysis 4: League breakdown ──────────────────────────────────────────────

def analysis_league(all_rows: list[dict], min_n: int = 5):
    print("\n" + "="*90)
    print(f"АНАЛИЗ 4: LEAGUE BREAKDOWN (edge_adj>0 & elo_diff>=150 & odds<2.0, min_n={min_n})")
    print("="*90)

    by_league: dict[str, list] = defaultdict(list)
    for b in all_rows:
        if b["edge_adj"] > 0 and b["elo_diff"] >= 150 and b["bet_odds"] < 2.0:
            cb = compute_bet(b)
            # Shorten league name
            ln = b["league"].replace("DOTA2 - ", "").replace("DOTA2", "Unknown")
            by_league[ln].append(cb)

    rows_out = [(ln, stats(bets)) for ln, bets in by_league.items() if len(bets) >= min_n]
    rows_out.sort(key=lambda x: -x[1]["n"])

    hdr = f"  {'League':35}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CLV+':>6}  {'avgCLV':>8}  {'maxDD':>7}"
    print(hdr)
    print("  " + "-"*85)
    for ln, s in rows_out:
        print(f"  {ln:35}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"{s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  {s['max_dd']:>7.3f}")

    # "Other" bucket
    small_leagues = [bets for ln, bets in by_league.items() if len(bets) < min_n]
    if small_leagues:
        other_bets = [b for bets in small_leagues for b in bets]
        s = stats(other_bets)
        print(f"  {'Other (n<'+str(min_n)+')':35}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"{s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  {s['max_dd']:>7.3f}")

    # Signal concentration check
    total_n   = sum(s["n"] for _, s in rows_out)
    if rows_out:
        top_n = rows_out[0][1]["n"]
        print(f"\n  Signal concentration: top league = {rows_out[0][0]} "
              f"({top_n}/{total_n} = {top_n/total_n:.0%} ставок)")


# ── Analysis 5: Region breakdown ──────────────────────────────────────────────

def analysis_region(all_rows: list[dict]):
    print("\n" + "="*90)
    print("АНАЛИЗ 5: REGION BREAKDOWN (edge_adj>0 & elo_diff>=150 & odds<2.0)")
    print("="*90)

    by_region: dict[str, list] = defaultdict(list)
    for b in all_rows:
        if b["edge_adj"] > 0 and b["elo_diff"] >= 150 and b["bet_odds"] < 2.0:
            cb = compute_bet(b)
            by_region[b["region"]].append(cb)

    # Sort by n
    rows_out = [(r, stats(bets)) for r, bets in by_region.items() if bets]
    rows_out.sort(key=lambda x: -x[1]["n"])

    hdr = f"  {'Region':10}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  {'CLV+':>6}  {'avgCLV':>8}"
    print(hdr)
    print("  " + "-"*80)
    for region, s in rows_out:
        if len(s["profits"]) >= 5:
            lo, hi, _ = bootstrap_ci(s["profits"], n=2000)
            ci_str = f"[{lo:+.3f},{hi:+.3f}]"
            star = " ✓" if lo > 0 else "  "
        else:
            ci_str = "   (n<5)   "
            star = "  "
        print(f"  {region:10}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"{ci_str:>18}  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}{star}")

    # Concentration
    total_n = sum(s["n"] for _, s in rows_out)
    for region, s in rows_out[:2]:
        print(f"\n  {region}: {s['n']}/{total_n} = {s['n']/total_n:.0%} всех ставок")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["1","2","3","4","5"],
                        help="Запустить только один анализ")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*90}")
    print("STRESS TEST — dota_trader_v2")
    print(f"Стратегия: edge_adj > 0  AND  elo_diff >= 150  AND  bet_odds < 2.0")
    print(f"{'='*90}\n")

    all_rows = build_all_bets(conn)
    conn.close()

    # Locked segment summary
    seg = [b for b in all_rows
           if b["edge_adj"] > 0 and b["elo_diff"] >= 150 and b["bet_odds"] < 2.0]
    print(f"\n  Locked segment size: n = {len(seg)}")

    run_all = args.only is None
    if run_all or args.only == "1": analysis_threshold_sweep(all_rows)
    if run_all or args.only == "2": analysis_bootstrap(all_rows)
    if run_all or args.only == "3": analysis_lomo(all_rows)
    if run_all or args.only == "4": analysis_league(all_rows)
    if run_all or args.only == "5": analysis_region(all_rows)

    print(f"\n{'='*90}")
    print("Готово.")


if __name__ == "__main__":
    main()
