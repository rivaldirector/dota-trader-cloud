#!/usr/bin/env python3
"""
Deep analysis — 7 задач для поиска устойчивого сегмента.

Запуск:
    PYTHONPATH=. python3 scripts/deep_analysis.py
    PYTHONPATH=. python3 scripts/deep_analysis.py --only 1   (только heatmap)
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
MIN_GAMES    = 3
H2H_MAX_W    = 0.40
H2H_CONF_N   = 5.0
SEED         = 42
N_BOOTSTRAP  = 5_000

# ── helpers ──────────────────────────────────────────────────────────────────

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def team_match(a, b):
    na, nb = normalize(a), normalize(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

def novig(h, a):
    if not h or not a or h <= 1 or a <= 1:
        return None, None
    ph, pa = 1/h, 1/a
    s = ph + pa
    return ph/s, pa/s

def get_mp_for_team1(m_t1, m_t2, o_t1, o_t2, odds1, odds2):
    if team_match(m_t1, o_t1) and team_match(m_t2, o_t2):
        p1, p2 = novig(odds1, odds2)
        return p1, p2, (p1 is not None)
    if team_match(m_t1, o_t2) and team_match(m_t2, o_t1):
        p2, p1 = novig(odds1, odds2)
        return p1, p2, (p1 is not None)
    return None, None, False

def elo_prob(ra, rb):
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))

def bootstrap_ci(values, n=N_BOOTSTRAP, alpha=0.05, seed=SEED):
    if len(values) < 3:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choices(values, k=len(values))) / len(values)
        for _ in range(n)
    )
    return means[int(alpha/2 * n)], means[int((1-alpha/2) * n)]

def stats(bets):
    if not bets:
        return None
    n       = len(bets)
    wins    = sum(b["win"] for b in bets)
    profits = [b["profit"] for b in bets]
    roi     = sum(profits) / n
    clv_pos = sum(1 for b in bets if b["clv"] > 0) / n
    avg_clv = sum(b["clv"] for b in bets) / n
    acc_our = wins / n
    acc_mkt = sum(b["mkt_correct"] for b in bets) / n
    return dict(n=n, wr=wins/n, roi=roi, clv_pos=clv_pos,
                avg_clv=avg_clv, acc_our=acc_our, acc_mkt=acc_mkt,
                profits=profits)

def ci_str(profits, n=N_BOOTSTRAP):
    lo, hi = bootstrap_ci(profits, n=n)
    if lo != lo:  # nan check
        return "(n<3)      ", False
    star = lo > 0
    return f"[{lo:+.3f},{hi:+.3f}]", star

def sz_tag(n):
    if n < 5:  return f"[n={n:2d}]*"
    if n < 10: return f"[n={n:2d}]~"
    return      f"[n={n:2d}] "

# ── Core pipeline ─────────────────────────────────────────────────────────────

def build_all_bets(conn):
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

    snaps = conn.execute("""
        SELECT match_external_id, bookmaker, captured_at,
               team_1_name, team_2_name, team_1_odds, team_2_odds
        FROM odds_snapshots
        WHERE league_name LIKE 'DOTA2%'
          AND team_1_odds IS NOT NULL AND team_2_odds IS NOT NULL
          AND team_1_odds > 1 AND team_2_odds > 1
        ORDER BY match_external_id, bookmaker
    """).fetchall()

    PREFERRED = ["Bet365","Pinnacle","PinnacleSports","GGBet","10Bet","188Bet",
                 "FonBet","MelBet","CashPoint","888Sport"]
    odds_idx = defaultdict(lambda: defaultdict(dict))
    for s in snaps:
        mid = s["match_external_id"]
        bm  = s["bookmaker"]
        tag = "open" if s["captured_at"].endswith("_open") else "close"
        odds_idx[mid][bm][tag] = {
            "h": s["team_1_odds"], "a": s["team_2_odds"],
            "t1": s["team_1_name"], "t2": s["team_2_name"],
        }

    # Also build full bookmaker-level odds for each match (for bm breakdown)
    bm_all_idx = defaultdict(dict)  # mid → {bm: {open_h,open_a,close_h,close_a,t1,t2}}
    for mid, bm_dict in odds_idx.items():
        for bm, d in bm_dict.items():
            if "open" in d:
                bm_all_idx[mid][bm] = {
                    "open_h": d["open"]["h"], "open_a": d["open"]["a"],
                    "close_h": d.get("close", d["open"])["h"],
                    "close_a": d.get("close", d["open"])["a"],
                    "t1": d["open"]["t1"], "t2": d["open"]["t2"],
                }

    def best_odds(mid, pref_order=PREFERRED):
        if mid not in odds_idx:
            return None, None
        bm_data = odds_idx[mid]
        chosen = None
        for bm in pref_order:
            if bm in bm_data and "open" in bm_data[bm]:
                chosen = bm; break
        if not chosen:
            for bm, d in bm_data.items():
                if "open" in d:
                    chosen = bm; break
        if not chosen:
            return None, None
        d = bm_data[chosen]
        od = d["open"]
        cd = d.get("close", od)
        return chosen, {
            "bm": chosen,
            "open_h": od["h"], "open_a": od["a"],
            "close_h": cd["h"], "close_a": cd["a"],
            "t1": od["t1"], "t2": od["t2"],
        }

    now_dt = datetime.now(timezone.utc)
    elo  = defaultdict(lambda: START_ELO)
    games = defaultdict(int)
    h2h  = defaultdict(list)  # (min_t,max_t) → [(ts, key0_won)]

    results = []

    for r in matches:
        t1  = r["team_1_name"]
        t2  = r["team_2_name"]
        win = r["winner_name"]
        eid = str(r["external_id"])
        ln  = r["league_name"] or ""
        bat = r["begin_at"] or ""
        result = 1 if win == t1 else 0

        e1, e2   = elo[t1], elo[t2]
        ep       = elo_prob(e1, e2)
        elo_diff = abs(e1 - e2)
        g1, g2   = games[t1], games[t2]

        key = (min(t1,t2), max(t1,t2))
        h2h_entries = h2h[key]
        if h2h_entries:
            try:
                bat_ts = datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
            except Exception:
                bat_ts = now_dt.timestamp()
            w_wins = w_tot = 0.0
            for (ts, k0_won) in h2h_entries:
                w = 0.5 ** ((bat_ts - ts) / 365 / 86400)
                w_tot  += w
                if (k0_won and t1 == key[0]) or (not k0_won and t1 == key[1]):
                    w_wins += w
            h2h_n    = len(h2h_entries)
            h2h_wr_d = w_wins / w_tot if w_tot > 0 else 0.5
        else:
            h2h_n = 0; h2h_wr_d = 0.5

        h2h_conf = min(h2h_n / H2H_CONF_N, 1.0)
        adj_prob = ep * (1 - H2H_MAX_W * h2h_conf) + h2h_wr_d * (H2H_MAX_W * h2h_conf)

        if g1 >= MIN_GAMES and g2 >= MIN_GAMES:
            bm_chosen, od = best_odds(eid)
            if od:
                mp1_open, mp2_open, valid = get_mp_for_team1(
                    t1, t2, od["t1"], od["t2"], od["open_h"], od["open_a"])
                mp1_close, _, _ = get_mp_for_team1(
                    t1, t2, od["t1"], od["t2"],
                    od["close_h"] or od["open_h"], od["close_a"] or od["open_a"])
                if valid and mp1_open is not None:
                    edge_adj  = adj_prob - mp1_open
                    # odds for t1 (the team we bet on when edge_adj>0)
                    if team_match(t1, od["t1"]):
                        bet_odds_t1 = od["open_h"]
                    else:
                        bet_odds_t1 = od["open_a"]

                    # All bookmakers data for this match (for bm breakdown)
                    bm_details = {}
                    for bm, bmod in bm_all_idx.get(eid, {}).items():
                        mp1, mp2, v = get_mp_for_team1(
                            t1, t2, bmod["t1"], bmod["t2"],
                            bmod["open_h"], bmod["open_a"])
                        if v and mp1 is not None:
                            edge_bm = adj_prob - mp1
                            # t1 odds from this bm
                            if team_match(t1, bmod["t1"]):
                                bo = bmod["open_h"]
                            else:
                                bo = bmod["open_a"]
                            mp1c, _, _ = get_mp_for_team1(
                                t1, t2, bmod["t1"], bmod["t2"],
                                bmod["close_h"] or bmod["open_h"],
                                bmod["close_a"] or bmod["open_a"])
                            bm_details[bm] = dict(
                                edge_adj=edge_bm, mp_open=mp1,
                                mp_close=mp1c if mp1c else mp1,
                                bet_odds=bo)

                    results.append(dict(
                        eid=eid, begin_at=bat, month=bat[:7],
                        league=ln, team1=t1, team2=t2,
                        elo_diff=round(elo_diff,1),
                        adj_prob=round(adj_prob,4),
                        mp_open=round(mp1_open,4),
                        mp_close=round(mp1_close,4) if mp1_close else round(mp1_open,4),
                        edge_adj=round(edge_adj,4),
                        bet_odds=round(bet_odds_t1,3),
                        result=result,
                        bookmaker=bm_chosen,
                        bm_details=bm_details,
                    ))

        # Update state
        k   = _tier_k(ln)
        w   = _time_weight(bat, now_dt, 365)
        kef = k * w
        exp1 = elo_prob(e1, e2)
        elo[t1] = e1 + kef * (result   - exp1)
        elo[t2] = e2 + kef * (1-result - (1-exp1))
        games[t1] += 1; games[t2] += 1
        try:
            ts = datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except Exception:
            ts = now_dt.timestamp()
        h2h[key].append((ts, win == key[0]))

    return results


def make_bet(b, bm=None):
    """
    Compute profit/clv/win from a row.
    bm: if set, use that bookmaker's edge_adj/bet_odds instead of primary.
    """
    if bm and bm in b.get("bm_details", {}):
        d = b["bm_details"][bm]
        edge_adj = d["edge_adj"]
        bet_odds = d["bet_odds"]
        mp_open  = d["mp_open"]
        mp_close = d["mp_close"]
    else:
        edge_adj = b["edge_adj"]
        bet_odds = b["bet_odds"]
        mp_open  = b["mp_open"]
        mp_close = b["mp_close"]

    if edge_adj <= 0:
        return None  # don't bet

    result = b["result"]
    win    = (result == 1)
    profit = (bet_odds - 1) if win else -1.0
    clv    = mp_close - mp_open  # positive = market moved toward t1
    mkt_fav_t1 = mp_open > 0.5
    mkt_correct = int((mkt_fav_t1 and result==1) or (not mkt_fav_t1 and result==0))
    return dict(win=int(win), profit=profit, clv=clv,
                mkt_correct=mkt_correct, bet_odds=bet_odds,
                mp_open=mp_open, league=b["league"],
                bookmaker=b.get("bookmaker","?"))


# ── Task 1: 2D Heatmap ───────────────────────────────────────────────────────

def task_heatmap(all_rows):
    ELO_THRESHOLDS  = [0, 25, 50, 75, 100, 125, 150, 175, 200, 225, 250]
    ODDS_THRESHOLDS = [1.30, 1.50, 1.70, 2.00]

    print("\n" + "="*100)
    print("ЗАДАЧА 1: 2D HEATMAP  edge_adj > 0  |  elo_diff >= X  |  odds < Y")
    print("="*100)

    for odds_thr in ODDS_THRESHOLDS:
        print(f"\n  ── odds < {odds_thr:.2f} ──────────────────────────────────────────────────────")
        print(f"  {'elo>=':>6}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI 95%':>18}  "
              f"{'CLV+':>6}  {'avgCLV':>8}  {'acc_our':>8}  {'acc_mkt':>8}")
        print("  " + "-"*90)
        for elo_thr in ELO_THRESHOLDS:
            bets = []
            for b in all_rows:
                if b["edge_adj"] > 0 and b["elo_diff"] >= elo_thr and b["bet_odds"] < odds_thr:
                    bt = make_bet(b)
                    if bt: bets.append(bt)
            if not bets:
                print(f"  {elo_thr:>6}  {'0':>4}")
                continue
            s = stats(bets)
            cis, star = ci_str(s["profits"])
            mark = " ✓" if star else "  "
            print(f"  {elo_thr:>6}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
                  f"{cis:>18}  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  "
                  f"{s['acc_our']:>8.3f}  {s['acc_mkt']:>8.3f}{mark}")

    # Best cells (CI > 0)
    print("\n  ── Только ячейки с CI > 0 ──")
    print(f"  {'elo>=':>6}  {'odds<':>6}  {'n':>4}  {'ROI':>7}  {'CI_lo':>7}  {'CLV+':>6}  {'avgCLV':>8}")
    print("  " + "-"*65)
    for odds_thr in ODDS_THRESHOLDS:
        for elo_thr in ELO_THRESHOLDS:
            bets = [make_bet(b) for b in all_rows
                    if b["edge_adj"]>0 and b["elo_diff"]>=elo_thr
                    and b["bet_odds"]<odds_thr and make_bet(b) is not None]
            bets = [b for b in bets if b]
            if len(bets) < 5: continue
            s = stats(bets)
            lo, hi = bootstrap_ci(s["profits"])
            if lo > 0:
                print(f"  {elo_thr:>6}  {odds_thr:>6.2f}  {s['n']:>4}  "
                      f"{s['roi']:>+7.3f}  {lo:>+7.3f}  {s['clv_pos']:>6.3f}  "
                      f"{s['avg_clv']:>+8.4f}  ✓")


# ── Task 2: League Breakdown (all leagues) ────────────────────────────────────

def task_league(all_rows):
    # Use best candidate from heatmap: edge_adj>0, elo_diff>=0 (all), odds<2.0
    # But also show for elo>=75
    for elo_thr, label in [(0, "edge_adj>0 & odds<2.0"), (75, "edge_adj>0 & elo_diff>=75 & odds<2.0")]:
        print(f"\n{'='*100}")
        print(f"ЗАДАЧА 2: LEAGUE BREAKDOWN  [{label}]")
        print("="*100)
        by_league = defaultdict(list)
        for b in all_rows:
            if b["edge_adj"] > 0 and b["elo_diff"] >= elo_thr and b["bet_odds"] < 2.0:
                bt = make_bet(b)
                if bt:
                    ln = b["league"].replace("DOTA2 - ","").replace("DOTA2","Unknown")
                    by_league[ln].append(bt)

        rows_out = [(ln, stats(bets)) for ln, bets in by_league.items()]
        rows_out.sort(key=lambda x: -x[1]["n"])

        total_n = sum(s["n"] for _, s in rows_out)
        print(f"\n  Total n = {total_n}")
        print(f"  {'League':38}  {'Tag':9}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  "
              f"{'CLV+':>6}  {'avgCLV':>8}  {'acc_our':>8}  {'acc_mkt':>8}")
        print("  " + "-"*110)
        for ln, s in rows_out:
            tag = sz_tag(s["n"])
            cis, star = ci_str(s["profits"]) if s["n"] >= 3 else ("(n<3)     ", False)
            mark = " ✓" if star else "  "
            pct  = f"{s['n']/total_n:.0%}"
            print(f"  {ln:38}  {tag}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
                  f"{cis:>18}  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  "
                  f"{s['acc_our']:>8.3f}  {s['acc_mkt']:>8.3f}  {pct:>4}{mark}")

        # Concentration
        top3_n = sum(s["n"] for _, s in rows_out[:3])
        print(f"\n  Концентрация: топ-3 лиги = {top3_n}/{total_n} = {top3_n/total_n:.0%}")


# ── Task 3: Bookmaker Breakdown ───────────────────────────────────────────────

def task_bookmaker(all_rows):
    # First: for overall edge_adj > 0, all elo_diff, all odds — what does each bm show?
    # Use per-bm edge_adj from bm_details
    for elo_thr, label in [(0, "edge_adj>0 (все)"), (75, "edge_adj>0 & elo_diff>=75 & odds<2.0")]:
        print(f"\n{'='*100}")
        print(f"ЗАДАЧА 3: BOOKMAKER BREAKDOWN  [{label}]")
        print("="*100)

        by_bm = defaultdict(list)
        for b in all_rows:
            odds_ok = (b["bet_odds"] < 2.0) if elo_thr > 0 else True
            elo_ok  = (b["elo_diff"] >= elo_thr)
            if not (elo_ok and odds_ok): continue

            # For each bookmaker's own edge
            for bm, d in b.get("bm_details", {}).items():
                if d["edge_adj"] > 0:
                    result = b["result"]
                    win    = (result == 1)
                    profit = (d["bet_odds"] - 1) if win else -1.0
                    clv    = d["mp_close"] - d["mp_open"]
                    mkt_fav_t1  = d["mp_open"] > 0.5
                    mkt_correct = int((mkt_fav_t1 and result==1) or (not mkt_fav_t1 and result==0))
                    if d["bet_odds"] < 2.0 or elo_thr == 0:
                        by_bm[bm].append(dict(win=int(win), profit=profit,
                                              clv=clv, mkt_correct=mkt_correct,
                                              bet_odds=d["bet_odds"]))

        rows_out = [(bm, stats(bets)) for bm, bets in by_bm.items() if bets]
        rows_out.sort(key=lambda x: -x[1]["n"])
        total_n = sum(s["n"] for _, s in rows_out)

        print(f"\n  Total bet-opportunities: {total_n} (across all bm)")
        print(f"  {'Bookmaker':15}  {'Tag':9}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  "
              f"{'CLV+':>6}  {'avgCLV':>8}  {'acc_our':>8}  {'acc_mkt':>8}")
        print("  " + "-"*100)
        for bm, s in rows_out:
            tag = sz_tag(s["n"])
            cis, star = ci_str(s["profits"]) if s["n"] >= 3 else ("(n<3)     ", False)
            mark = " ✓" if star else "  "
            print(f"  {bm:15}  {tag}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
                  f"{cis:>18}  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  "
                  f"{s['acc_our']:>8.3f}  {s['acc_mkt']:>8.3f}{mark}")


# ── Task 4: CLV Distribution ─────────────────────────────────────────────────

def task_clv_distribution(all_rows):
    # Test several candidate rules
    candidates = [
        ("edge_adj>0 & elo>=75 & odds<2.0",  lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75  and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=125 & odds<2.0", lambda b: b["edge_adj"]>0 and b["elo_diff"]>=125 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=150 & odds<2.0", lambda b: b["edge_adj"]>0 and b["elo_diff"]>=150 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=75 & odds<1.70", lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75  and b["bet_odds"]<1.70),
        ("edge_adj>0 & elo>=75 & odds<1.50", lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75  and b["bet_odds"]<1.50),
    ]
    print(f"\n{'='*100}")
    print("ЗАДАЧА 4: CLV DISTRIBUTION — для кандидатных правил")
    print("="*100)

    BUCKETS = [(-999,-0.10), (-0.10,-0.05), (-0.05,0.0), (0.0,0.05), (0.05,0.10), (0.10,999)]
    BLABELS = ["<-10%", "-10..-5%", "-5..0%", "0..5%", "5..10%", ">10%"]

    for label, filt in candidates:
        bets = []
        for b in all_rows:
            if filt(b):
                bt = make_bet(b)
                if bt: bets.append(bt)
        if not bets:
            print(f"\n  {label}: нет ставок")
            continue
        clvs = [bt["clv"] for bt in bets]
        n = len(clvs)
        sorted_c = sorted(clvs)
        mean_c = sum(clvs)/n
        med_c  = sorted_c[n//2]
        p25    = sorted_c[n//4]
        p75    = sorted_c[3*n//4]
        clv_pos = sum(1 for c in clvs if c > 0) / n
        print(f"\n  ── {label}  (n={n}) ──")
        print(f"     mean={mean_c:+.4f}  median={med_c:+.4f}  "
              f"p25={p25:+.4f}  p75={p75:+.4f}  "
              f"min={sorted_c[0]:+.4f}  max={sorted_c[-1]:+.4f}")
        print(f"     CLV+ = {clv_pos:.1%}")
        print(f"     Buckets:")
        for (lo,hi), bl in zip(BUCKETS, BLABELS):
            cnt  = sum(1 for c in clvs if lo < c <= hi)
            pct  = cnt/n
            bar  = "█" * int(pct * 30)
            print(f"       {bl:12} | {bar:<30} {cnt:3d}/{n}  ({pct:.0%})")


# ── Task 5: Remove One League ─────────────────────────────────────────────────

def task_remove_league(all_rows):
    # Use best candidate
    RULE_LABEL = "edge_adj>0 & elo_diff>=75 & odds<2.0"
    def apply_rule(rows):
        return [b for b in rows if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0]

    print(f"\n{'='*100}")
    print(f"ЗАДАЧА 5: REMOVE-ONE-LEAGUE TEST  [{RULE_LABEL}]")
    print("="*100)

    seg = apply_rule(all_rows)
    bets_all = [bt for b in seg for bt in [make_bet(b)] if bt]
    s_all = stats(bets_all)
    lo_all, hi_all = bootstrap_ci(s_all["profits"])
    print(f"\n  Full segment: n={s_all['n']}  ROI={s_all['roi']:+.4f}  "
          f"CI=[{lo_all:+.3f},{hi_all:+.3f}]  CLV+={s_all['clv_pos']:.3f}")
    print(f"\n  Removing each league:")
    print(f"  {'League removed':38}  {'n_rem':>6}  {'n_left':>6}  {'ROI':>7}  "
          f"{'CI':>18}  {'CLV+':>6}  {'avgCLV':>8}")
    print("  " + "-"*95)

    leagues = sorted(set(b["league"].replace("DOTA2 - ","").replace("DOTA2","Unknown")
                         for b in seg))
    for lg in leagues:
        remaining = [b for b in seg
                     if b["league"].replace("DOTA2 - ","").replace("DOTA2","Unknown") != lg]
        bets = [bt for b in remaining for bt in [make_bet(b)] if bt]
        n_removed = s_all["n"] - len(bets)
        if not bets:
            print(f"  {lg:38}  {n_removed:>6}  {'0':>6}")
            continue
        s = stats(bets)
        cis, star = ci_str(s["profits"])
        mark = " ✓" if star else "  "
        print(f"  {lg:38}  {n_removed:>6}  {s['n']:>6}  {s['roi']:>+7.3f}  "
              f"{cis:>18}  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}{mark}")


# ── Task 6: Favorite Strength Buckets ────────────────────────────────────────

def task_fav_buckets(all_rows):
    print(f"\n{'='*100}")
    print("ЗАДАЧА 6: FAVORITE STRENGTH BUCKETS  [edge_adj>0 & odds<2.0]")
    print("="*100)

    BUCKETS = [(0.50,0.60), (0.60,0.70), (0.70,0.80), (0.80,0.90), (0.90,1.01)]
    LABELS  = ["50-60%","60-70%","70-80%","80-90%","90%+"]

    for elo_thr, label in [(0, "все elo_diff"), (75, "elo_diff >= 75")]:
        print(f"\n  ── {label} ──")
        print(f"  {'market_prob':12}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  "
              f"{'CLV+':>6}  {'avgCLV':>8}  {'m_prob avg':>11}  {'adj_prob avg':>13}")
        print("  " + "-"*100)
        for (lo,hi), lbl in zip(BUCKETS, LABELS):
            bets = []
            mp_list, adj_list = [], []
            for b in all_rows:
                if not (b["edge_adj"]>0 and b["elo_diff"]>=elo_thr and b["bet_odds"]<2.0):
                    continue
                if not (lo <= b["mp_open"] < hi):
                    continue
                bt = make_bet(b)
                if bt:
                    bets.append(bt)
                    mp_list.append(b["mp_open"])
                    adj_list.append(b["adj_prob"])
            if not bets:
                print(f"  {lbl:12}  {'0':>4}")
                continue
            s = stats(bets)
            cis, star = ci_str(s["profits"]) if len(bets) >= 3 else ("(n<3)     ", False)
            mark = " ✓" if star else "  "
            avg_mp  = sum(mp_list)/len(mp_list)
            avg_adj = sum(adj_list)/len(adj_list)
            print(f"  {lbl:12}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
                  f"{cis:>18}  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  "
                  f"{avg_mp:>11.4f}  {avg_adj:>13.4f}{mark}")


# ── Task 7: Final Candidate Rule ──────────────────────────────────────────────

def task_final_rule(all_rows):
    print(f"\n{'='*100}")
    print("ЗАДАЧА 7: FINAL CANDIDATE RULE")
    print("Requirements: n>=40, CI ideally>0, CLV+>=55%, avgCLV>0, not 1-league, not 1-bm, logical")
    print("="*100)

    # Test candidate rules
    candidates = [
        ("edge_adj>0 & elo>=0  & odds<2.0",
         lambda b: b["edge_adj"]>0 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=25 & odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=25 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=50 & odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=50 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=75 & odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=100& odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=100 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=125& odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=125 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=150& odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=150 and b["bet_odds"]<2.0),
        ("edge_adj>0 & elo>=75 & odds<1.70",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<1.70),
        ("edge_adj>0 & elo>=75 & odds<1.50",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<1.50),
        ("edge_adj>0 & elo>=50 & odds<1.70",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=50 and b["bet_odds"]<1.70),
    ]

    print(f"\n  {'Rule':40}  {'n':>4}  {'ROI':>7}  {'CI_lo':>7}  {'CI_hi':>7}  "
          f"{'CLV+':>6}  {'avgCLV':>8}  {'PASS':>5}")
    print("  " + "-"*105)

    best = None
    for label, filt in candidates:
        bets = [bt for b in all_rows for bt in [make_bet(b)] if filt(b) and bt]
        if not bets:
            print(f"  {label:40}  {'0':>4}")
            continue
        s = stats(bets)
        lo, hi = bootstrap_ci(s["profits"]) if len(bets) >= 5 else (float("nan"), float("nan"))

        # Check all requirements
        n_ok    = s["n"] >= 40
        ci_ok   = lo > 0 if lo == lo else False
        clvp_ok = s["clv_pos"] >= 0.55
        aclv_ok = s["avg_clv"] > 0

        # League concentration check
        by_lg = defaultdict(int)
        for b in all_rows:
            if filt(b) and make_bet(b):
                ln = b["league"].replace("DOTA2 - ","").replace("DOTA2","Unknown")
                by_lg[ln] += 1
        top_lg_pct = max(by_lg.values()) / sum(by_lg.values()) if by_lg else 1.0
        lg_ok = top_lg_pct < 0.6

        # Bookmaker concentration
        by_bm = defaultdict(int)
        for b in all_rows:
            if filt(b) and make_bet(b):
                by_bm[b["bookmaker"]] += 1
        top_bm_pct = max(by_bm.values()) / sum(by_bm.values()) if by_bm else 1.0
        bm_ok = top_bm_pct < 0.7

        checks = [n_ok, ci_ok, clvp_ok, aclv_ok, lg_ok, bm_ok]
        n_pass  = sum(checks)
        pass_str = f"{n_pass}/6"
        n_str    = "n✓" if n_ok else "n✗"
        ci_str_r = "CI✓" if ci_ok else "CI✗"
        clv_str  = "CLV+✓" if clvp_ok else "CLV+✗"
        aclv_str = "aCLV✓" if aclv_ok else "aCLV✗"
        lg_str   = "Lg✓" if lg_ok else "Lg✗"
        bm_str   = "BM✓" if bm_ok else "BM✗"

        lo_s = f"{lo:+.3f}" if lo == lo else "  nan"
        hi_s = f"{hi:+.3f}" if hi == hi else "  nan"
        mark = " ← BEST" if n_pass == max(sum([n_ok,ci_ok,clvp_ok,aclv_ok,lg_ok,bm_ok]) for _,_ in [(0,0)]) else ""

        print(f"  {label:40}  {s['n']:>4}  {s['roi']:>+7.3f}  {lo_s:>7}  {hi_s:>7}  "
              f"{s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  "
              f"{n_str} {ci_str_r} {clv_str} {aclv_str} {lg_str} {bm_str}")

        if best is None or n_pass > best[0]:
            best = (n_pass, label, s, lo, hi, by_lg, by_bm)

    # Final verdict
    print(f"\n{'='*100}")
    if best and best[0] >= 4:
        n_pass, label, s, lo, hi, by_lg, by_bm = best
        print(f"  КАНДИДАТ: {label}")
        print(f"  n={s['n']}  ROI={s['roi']:+.4f}  CI=[{lo:+.3f},{hi:+.3f}]  "
              f"CLV+={s['clv_pos']:.3f}  avgCLV={s['avg_clv']:+.4f}")
        lo_lg = sorted(by_lg.items(), key=lambda x:-x[1])[:3]
        print(f"  Топ лиги: " + "  ".join(f"{lg}({n})" for lg,n in lo_lg))
        lo_bm = sorted(by_bm.items(), key=lambda x:-x[1])[:3]
        print(f"  Топ BM:   " + "  ".join(f"{bm}({n})" for bm,n in lo_bm))
        print(f"\n  Проходит {n_pass}/6 критериев.")
        if n_pass < 5:
            print("  ВЕРДИКТ: Готового правила для paper daemon пока нет.")
            print("           Лучший кандидат не проходит все критерии.")
            print("           Нужно накопить n >= 100 прежде чем фиксировать правило.")
        else:
            print("  ВЕРДИКТ: КАНДИДАТ ГОТОВ для paper daemon (при условии проспективной валидации).")
    else:
        print("  ВЕРДИКТ: Готового правила для paper daemon пока нет.")
        print("           Ни одно правило не проходит 4+ из 6 критериев.")
        print("           Нужно накопить n >= 100 прежде чем фиксировать правило.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["1","2","3","4","5","6","7"])
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*100}")
    print("DEEP ANALYSIS — dota_trader_v2")
    print(f"{'='*100}\n")
    print("Загружаем данные...", flush=True)
    all_rows = build_all_bets(conn)
    conn.close()
    print(f"  Матчей с odds: {len(all_rows)}\n", flush=True)

    run = args.only
    if run is None or run == "1": task_heatmap(all_rows)
    if run is None or run == "2": task_league(all_rows)
    if run is None or run == "3": task_bookmaker(all_rows)
    if run is None or run == "4": task_clv_distribution(all_rows)
    if run is None or run == "5": task_remove_league(all_rows)
    if run is None or run == "6": task_fav_buckets(all_rows)
    if run is None or run == "7": task_final_rule(all_rows)

    print(f"\n{'='*100}")
    print("Готово.")


if __name__ == "__main__":
    main()
