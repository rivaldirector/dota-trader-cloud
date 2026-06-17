#!/usr/bin/env python3
"""
Destruction Test — GPT Report #8
Цель: уничтожить Rule C или доказать существование сигнала.

Rule C: edge_adj > 0  AND  elo_diff >= 75  AND  odds < 2.0  AND  market_prob 60-70%

Задачи:
  1. Permutation Test       (10k shuffles, p-value)
  2. Random Rules Competition (1000 rules, ранг Rule C)
  3. Out-of-Sample Walk Forward (месячная динамика)
  4. Market Efficiency Test  (edge buckets внутри Rule C)
  5. H2H Causality Test      (контроль elo_diff)
  6. Paper Trading Expectation (30/60/90 дней)

Запуск:
    PYTHONPATH=. python3 scripts/destruction_test.py
    PYTHONPATH=. python3 scripts/destruction_test.py --only 1
"""
from __future__ import annotations

import sys, re, sqlite3, random, argparse
from collections import defaultdict
from datetime import datetime, timezone
from math import pow as mpow
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from models.team_rating import _tier_k, _time_weight

DB_PATH    = PROJECT_ROOT / settings.database_path
START_ELO  = 1500.0
MIN_GAMES  = 3
H2H_MAX_W  = 0.40
H2H_CONF_N = 5.0
N_PERM     = 10_000
N_RANDOM   = 1_000
N_BOOT     = 5_000
SEED       = 42

# ── Rule C definition ────────────────────────────────────────────────────────

def RULE_C(b):
    return (b["edge_adj"] > 0 and b["elo_diff"] >= 75
            and b["bet_odds"] < 2.0 and 0.60 <= b["mp_open"] < 0.70)

# ── helpers ──────────────────────────────────────────────────────────────────

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def team_match(a, b):
    na, nb = normalize(a), normalize(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

def novig(h, a):
    if not h or not a or h <= 1 or a <= 1: return None, None
    ph, pa = 1/h, 1/a; s = ph+pa; return ph/s, pa/s

def get_mp(m_t1, m_t2, o_t1, o_t2, h, a):
    if team_match(m_t1,o_t1) and team_match(m_t2,o_t2):
        p1,p2 = novig(h,a); return p1,p2,(p1 is not None)
    if team_match(m_t1,o_t2) and team_match(m_t2,o_t1):
        p2,p1 = novig(h,a); return p1,p2,(p1 is not None)
    return None,None,False

def elo_prob(ra, rb):
    return 1.0/(1.0+mpow(10.0,(rb-ra)/400.0))

def roi(bets):
    if not bets: return float("nan")
    profits = [profit_of(b) for b in bets]
    return sum(profits)/len(profits)

def profit_of(b):
    return (b["bet_odds"]-1) if b["result"]==1 else -1.0

def clv_pos_of(b):
    return b["mp_close"] - b["mp_open"]

def bootstrap_ci(vals, n=N_BOOT, seed=SEED):
    if len(vals) < 3: return float("nan"), float("nan"), []
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(vals,k=len(vals)))/len(vals) for _ in range(n))
    return means[int(0.025*n)], means[int(0.975*n)], means

def full_stats(bets):
    if not bets: return None
    n = len(bets)
    profits = [profit_of(b) for b in bets]
    clvs = [clv_pos_of(b) for b in bets]
    roi_v = sum(profits)/n
    clvpos = sum(1 for c in clvs if c>0)/n
    avg_clv = sum(clvs)/n
    lo,hi,_ = bootstrap_ci(profits)
    wr = sum(1 for b in bets if b["result"]==1)/n
    return dict(n=n,wr=wr,roi=roi_v,lo=lo,hi=hi,
                clvpos=clvpos,avg_clv=avg_clv,
                profits=profits,clvs=clvs)

# ── Core pipeline ─────────────────────────────────────────────────────────────

def build_all_bets(conn):
    PREF = ["Bet365","Pinnacle","PinnacleSports","GGBet","10Bet",
            "188Bet","FonBet","MelBet","CashPoint","888Sport"]

    matches = conn.execute("""
        SELECT external_id, name, league_name, begin_at,
               team_1_name, team_2_name, winner_name
        FROM matches
        WHERE status='finished'
          AND team_1_name IS NOT NULL AND team_2_name IS NOT NULL
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
    """).fetchall()

    oidx = defaultdict(lambda: defaultdict(dict))
    for s in snaps:
        tag = "open" if s["captured_at"].endswith("_open") else "close"
        oidx[s["match_external_id"]][s["bookmaker"]][tag] = {
            "h":s["team_1_odds"],"a":s["team_2_odds"],
            "t1":s["team_1_name"],"t2":s["team_2_name"]}

    def best(mid):
        if mid not in oidx: return None,None
        bd=oidx[mid]
        bm=next((b for b in PREF if b in bd and "open" in bd[b]),None)
        if not bm: bm=next((b for b,d in bd.items() if "open" in d),None)
        if not bm: return None,None
        op=bd[bm]["open"]; cl=bd[bm].get("close",op)
        return bm,{"oh":op["h"],"oa":op["a"],"ch":cl["h"],"ca":cl["a"],
                   "t1":op["t1"],"t2":op["t2"]}

    now_dt=datetime.now(timezone.utc)
    elo=defaultdict(lambda:START_ELO); games=defaultdict(int)
    h2h=defaultdict(list); rows=[]

    for r in matches:
        t1,t2,win=r["team_1_name"],r["team_2_name"],r["winner_name"]
        eid=str(r["external_id"]); ln=r["league_name"] or ""; bat=r["begin_at"] or ""
        result=1 if win==t1 else 0
        e1,e2=elo[t1],elo[t2]; ep=elo_prob(e1,e2); ediff=abs(e1-e2)

        key=(min(t1,t2),max(t1,t2)); h2e=h2h[key]
        if h2e:
            try: bts=datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
            except: bts=now_dt.timestamp()
            ww=wt=0.0
            for (ts,k0w) in h2e:
                w=0.5**((bts-ts)/365/86400); wt+=w
                if (k0w and t1==key[0]) or (not k0w and t1==key[1]): ww+=w
            h2h_n=len(h2e); h2h_wr=ww/wt if wt>0 else 0.5
        else:
            h2h_n=0; h2h_wr=0.5

        hc=min(h2h_n/H2H_CONF_N,1.0)
        adj=ep*(1-H2H_MAX_W*hc)+h2h_wr*(H2H_MAX_W*hc)
        h2h_delta=adj-ep

        if games[t1]>=MIN_GAMES and games[t2]>=MIN_GAMES:
            bm,od=best(eid)
            if od:
                mp1,mp2,valid=get_mp(t1,t2,od["t1"],od["t2"],od["oh"],od["oa"])
                mp1c,_,_=get_mp(t1,t2,od["t1"],od["t2"],
                                od["ch"] or od["oh"],od["ca"] or od["oa"])
                if valid and mp1 is not None:
                    ea=adj-mp1
                    bo=od["oh"] if team_match(t1,od["t1"]) else od["oa"]
                    rows.append(dict(
                        eid=eid, begin_at=bat, month=bat[:7],
                        league=ln.replace("DOTA2 - ","").replace("DOTA2","?"),
                        elo_diff=ediff, elo_prob=ep, adj_prob=adj,
                        h2h_n=h2h_n, h2h_wr=h2h_wr, h2h_delta=h2h_delta,
                        mp_open=mp1, mp_close=mp1c if mp1c else mp1,
                        edge_adj=ea, bet_odds=bo, result=result,
                    ))

        k=_tier_k(ln); w=_time_weight(bat,now_dt,365); kef=k*w
        ex=elo_prob(e1,e2)
        elo[t1]=e1+kef*(result-ex); elo[t2]=e2+kef*((1-result)-(1-ex))
        games[t1]+=1; games[t2]+=1
        try: ts=datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except: ts=now_dt.timestamp()
        h2h[key].append((ts,win==key[0]))

    return rows


# ── Task 1: Permutation Test ──────────────────────────────────────────────────

def task1_permutation(all_rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 1: PERMUTATION TEST  (n_perm=10 000)")
    print("  H0: ROI Rule C можно получить случайным образом при случайных результатах матчей.")
    print("="*100)

    rc_rows = [b for b in all_rows if RULE_C(b)]
    n_rc = len(rc_rows)
    if n_rc == 0:
        print("  Rule C: 0 ставок — невозможно запустить тест."); return

    # Observed metrics
    obs_profits = [profit_of(b) for b in rc_rows]
    obs_roi = sum(obs_profits) / n_rc
    obs_clvs = [clv_pos_of(b) for b in rc_rows]
    obs_clvpos = sum(1 for c in obs_clvs if c > 0) / n_rc
    obs_wr = sum(b["result"] for b in rc_rows) / n_rc

    print(f"\n  Наблюдаемые метрики Rule C (n={n_rc}):")
    print(f"    WR     = {obs_wr:.3f}")
    print(f"    ROI    = {obs_roi:+.4f}")
    print(f"    CLV+   = {obs_clvpos:.3f}")

    # Permutation: shuffle result column across ALL rows, keep features fixed
    # Rule C selection (based on features) stays the same — only outcomes change
    rc_indices = [i for i, b in enumerate(all_rows) if RULE_C(b)]
    all_results = [b["result"] for b in all_rows]
    rc_bet_odds = [all_rows[i]["bet_odds"] for i in rc_indices]
    n_all = len(all_results)

    rng = random.Random(SEED)
    null_rois = []
    null_wrs  = []
    for _ in range(N_PERM):
        shuffled = all_results[:]
        rng.shuffle(shuffled)
        r_results = [shuffled[i] for i in rc_indices]
        profits = [(rc_bet_odds[j]-1) if r_results[j]==1 else -1.0
                   for j in range(n_rc)]
        null_rois.append(sum(profits)/n_rc)
        null_wrs.append(sum(r_results)/n_rc)

    null_rois.sort()
    null_wrs.sort()
    p_roi = sum(1 for r in null_rois if r >= obs_roi) / N_PERM
    p_wr  = sum(1 for w in null_wrs  if w >= obs_wr)  / N_PERM

    # Null distribution stats
    null_mean = sum(null_rois)/N_PERM
    null_p95  = null_rois[int(0.95*N_PERM)]
    null_p99  = null_rois[int(0.99*N_PERM)]
    null_p999 = null_rois[int(0.999*N_PERM)]

    print(f"\n  Null distribution (ROI под H0):")
    print(f"    Mean    = {null_mean:+.4f}  (ожидаемо ≈ 0)")
    print(f"    P95     = {null_p95:+.4f}")
    print(f"    P99     = {null_p99:+.4f}")
    print(f"    P99.9   = {null_p999:+.4f}")
    print(f"    Observed= {obs_roi:+.4f}")

    print(f"\n  p-value (ROI ≥ наблюдаемого):  {p_roi:.4f}  ({p_roi*100:.2f}%)")
    print(f"  p-value (WR  ≥ наблюдаемого):  {p_wr:.4f}  ({p_wr*100:.2f}%)")

    if p_roi < 0.001:
        verdict = "СИГНАЛ РЕАЛЕН  — p < 0.1% ✓✓✓"
    elif p_roi < 0.01:
        verdict = "УБЕДИТЕЛЬНО    — p < 1%   ✓✓"
    elif p_roi < 0.05:
        verdict = "СЛАБЫЙ СИГНАЛ  — p < 5%   ✓"
    else:
        verdict = "ШУМОВОЙ ПАТТЕРН — p > 5%  ✗"

    print(f"\n  Вердикт: {verdict}")

    # WR under H0
    expected_wr = sum(b["mp_open"] for b in rc_rows) / n_rc
    print(f"\n  WR под H0 (если рынок прав):  {expected_wr:.3f}")
    print(f"  Реальный WR:                   {obs_wr:.3f}")
    print(f"  Превышение:                    {obs_wr - expected_wr:+.3f}")
    p_wr_mkt = sum(1 for w in null_wrs if w >= obs_wr) / N_PERM
    # More meaningful: p(WR >= obs_wr) under H0 where each bet wins with market prob
    # (binomial approximation)
    print(f"  p(WR ≥ {obs_wr:.3f} | H0 shuffle): {p_wr_mkt:.4f}")


# ── Task 2: Random Rules Competition ─────────────────────────────────────────

def task2_random_rules(all_rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 2: RANDOM RULES COMPETITION  (n_rules=1 000, min_n=10)")
    print("  H0: Rule C — случайный локальный максимум в пространстве правил.")
    print("="*100)

    ELO_OPTS  = [0, 25, 50, 75, 100, 125, 150, 175, 200, 225, 250]
    ODDS_OPTS = [1.50, 1.60, 1.70, 1.80, 1.90, 2.00, 2.20, 2.50, 3.00]
    MKT_LO    = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
    MKT_WIDTH = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50]   # hi = lo + width (capped 1.0)
    EDGE_OPTS = [0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10]
    H2H_OPTS  = [0, 1, 2, 3, 5]

    # Rule C reference
    rc = [b for b in all_rows if RULE_C(b)]
    s_rc = full_stats(rc)
    print(f"\n  Rule C reference: n={s_rc['n']}  ROI={s_rc['roi']:+.3f}  "
          f"CI=[{s_rc['lo']:+.3f},{s_rc['hi']:+.3f}]  CLV+={s_rc['clvpos']:.3f}")

    rng = random.Random(SEED + 1)
    results = []
    for _ in range(N_RANDOM):
        elo_min  = rng.choice(ELO_OPTS)
        odds_max = rng.choice(ODDS_OPTS)
        mkt_lo   = rng.choice(MKT_LO)
        width    = rng.choice(MKT_WIDTH)
        mkt_hi   = min(mkt_lo + width, 1.01)
        edge_min = rng.choice(EDGE_OPTS)
        h2h_min  = rng.choice(H2H_OPTS)

        bets = [b for b in all_rows
                if b["edge_adj"] >= edge_min
                and b["elo_diff"] >= elo_min
                and b["bet_odds"] < odds_max
                and mkt_lo <= b["mp_open"] < mkt_hi
                and b["h2h_n"] >= h2h_min]
        if len(bets) < 10: continue

        s = full_stats(bets)
        if not s: continue
        results.append({
            "rule": f"elo>={elo_min} odds<{odds_max} mkt[{mkt_lo:.2f},{mkt_hi:.2f}) "
                    f"edge>={edge_min:.2f} h2h>={h2h_min}",
            "n": s["n"], "roi": s["roi"], "lo": s["lo"], "hi": s["hi"],
            "clvpos": s["clvpos"]
        })

    n_valid = len(results)
    print(f"\n  Сгенерировано правил: {N_RANDOM}  |  Валидных (n>=10): {n_valid}")

    if n_valid == 0:
        print("  Нет валидных правил."); return

    # Rank Rule C
    better_roi   = sum(1 for r in results if r["roi"]   >= s_rc["roi"])
    better_clv   = sum(1 for r in results if r["clvpos"]>= s_rc["clvpos"])
    better_lo    = sum(1 for r in results if r["lo"]    >= s_rc["lo"])
    pct_roi  = better_roi  / n_valid
    pct_clv  = better_clv  / n_valid
    pct_lo   = better_lo   / n_valid

    print(f"\n  Сравнение Rule C vs {n_valid} случайных правил:")
    print(f"    Правил с ROI  ≥ {s_rc['roi']:+.3f}:  {better_roi}/{n_valid} = {pct_roi:.1%}")
    print(f"    Правил с CLV+ ≥ {s_rc['clvpos']:.3f}:  {better_clv}/{n_valid} = {pct_clv:.1%}")
    print(f"    Правил с CI_lo≥ {s_rc['lo']:+.3f}: {better_lo}/{n_valid} = {pct_lo:.1%}")

    # Top-10 by CI_lo (most reliable)
    top = sorted(results, key=lambda r: r["lo"], reverse=True)[:10]
    print(f"\n  Топ-10 случайных правил по CI_lo:")
    print(f"  {'Rule':60}  {'n':>4}  {'ROI':>7}  {'CI_lo':>7}  {'CLV+':>6}")
    print("  "+"-"*95)
    for r in top:
        mark = "  ← Rule C" if abs(r["lo"] - s_rc["lo"]) < 0.005 else ""
        print(f"  {r['rule']:60}  {r['n']:>4}  {r['roi']:>+7.3f}  "
              f"{r['lo']:>+7.3f}  {r['clvpos']:>6.3f}{mark}")

    # Print Rule C position
    sorted_lo = sorted(results, key=lambda r: r["lo"], reverse=True)
    rc_rank_approx = better_lo + 1
    print(f"\n  Rule C примерный ранг по CI_lo: #{rc_rank_approx} из {n_valid}")
    print(f"  Процентиль: {(n_valid-better_lo)/n_valid:.1%}  "
          f"({'✓ Top-5%' if pct_lo < 0.05 else ('✓ Top-10%' if pct_lo < 0.10 else '— не в топ-10%')})")

    # Distribution of ROI in null
    roi_sorted = sorted(r["roi"] for r in results)
    p50 = roi_sorted[n_valid//2]
    p75 = roi_sorted[int(n_valid*0.75)]
    p90 = roi_sorted[int(n_valid*0.90)]
    p95 = roi_sorted[int(n_valid*0.95)]
    print(f"\n  Распределение ROI среди {n_valid} случайных правил:")
    print(f"    Медиана: {p50:+.3f}  P75: {p75:+.3f}  P90: {p90:+.3f}  P95: {p95:+.3f}")
    print(f"    Rule C:  {s_rc['roi']:+.3f}")


# ── Task 3: Out-of-Sample Walk Forward ───────────────────────────────────────

def task3_walk_forward(all_rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 3: OUT-OF-SAMPLE WALK FORWARD  (месяц за месяцем)")
    print("  H0: Rule C деградирует как только применяется на новых данных.")
    print("="*100)

    # Month-by-month Rule C
    by_month = defaultdict(list)
    for b in all_rows:
        if RULE_C(b):
            by_month[b["month"]].append(b)

    months = sorted(by_month.keys())
    print(f"\n  Rule C по месяцам:")
    print(f"\n  {'Месяц':10}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CLV+':>6}  "
          f"{'avgCLV':>7}  {'CumROI':>8}")
    print("  "+"-"*65)
    cum_profits = []; cum_bets = 0
    for m in months:
        bets = by_month[m]
        profits = [profit_of(b) for b in bets]
        clvs    = [clv_pos_of(b) for b in bets]
        n = len(bets)
        wr = sum(b["result"] for b in bets)/n
        roi_m = sum(profits)/n
        clvp = sum(1 for c in clvs if c>0)/n
        avg_clv = sum(clvs)/n
        cum_profits += profits
        cum_bets += n
        cum_roi = sum(cum_profits)/cum_bets
        mark = " ✓" if roi_m > 0 else " ✗"
        print(f"  {m:10}  {n:>4}  {wr:>6.3f}  {roi_m:>+7.3f}  {clvp:>6.3f}  "
              f"{avg_clv:>+7.4f}  {cum_roi:>+8.3f}{mark}")

    # Consecutive 3-month windows — Rule C ROI per window
    all_rc = sorted([b for b in all_rows if RULE_C(b)], key=lambda b: b["begin_at"])
    n_rc = len(all_rc)
    print(f"\n  Последовательные окна (каждые ~8 ставок):")
    win_size = max(1, n_rc // 4)
    wins = [all_rc[i:i+win_size] for i in range(0, n_rc, win_size)]
    for i, w in enumerate(wins, 1):
        if not w: continue
        profits = [profit_of(b) for b in w]
        clvs = [clv_pos_of(b) for b in w]
        r = sum(profits)/len(profits)
        cp = sum(1 for c in clvs if c > 0)/len(clvs)
        lo,hi,_ = bootstrap_ci(profits)
        lo_s = f"{lo:+.3f}" if lo==lo else " nan"
        hi_s = f"{hi:+.3f}" if hi==hi else " nan"
        mark = " ✓" if (lo==lo and lo>0) else "  "
        print(f"  Окно {i} (n={len(w)}):  "
              f"ROI={r:+.3f}  CI=[{lo_s},{hi_s}]  CLV+={cp:.3f}{mark}")

    # Walk-forward: Train on first K months, test on K+1..K+2
    print(f"\n  Walk-forward (expanding train → test следующие 2 мес):")
    print(f"  {'Train до':12}  {'n_train':>7}  {'ROI_train':>10}  "
          f"{'n_test':>6}  {'ROI_test':>9}  {'CLV+_test':>10}")
    print("  "+"-"*70)
    for split in range(2, len(months)-1):
        train_months = set(months[:split])
        test_months  = set(months[split:split+2])
        train = [b for b in all_rows if RULE_C(b) and b["month"] in train_months]
        test  = [b for b in all_rows if RULE_C(b) and b["month"] in test_months]
        if len(train) < 3 or len(test) < 1: continue
        roi_tr = sum(profit_of(b) for b in train)/len(train)
        roi_te = sum(profit_of(b) for b in test)/len(test)
        clvp_te = sum(1 for b in test if clv_pos_of(b)>0)/len(test)
        mark = " ✓" if roi_te > 0 else " ✗"
        print(f"  {months[split-1]:12}  {len(train):>7}  {roi_tr:>+10.3f}  "
              f"{len(test):>6}  {roi_te:>+9.3f}  {clvp_te:>10.3f}{mark}")

    # Sign test: how many months positive?
    pos_months = sum(1 for m in months if by_month[m]
                     and sum(profit_of(b) for b in by_month[m])/len(by_month[m]) > 0)
    tot_months = len([m for m in months if by_month[m]])
    print(f"\n  Позитивных месяцев: {pos_months}/{tot_months}  "
          f"({'✓ >50%' if pos_months/tot_months > 0.5 else '✗ ≤50%'})")

    # Binomial p-value: P(X >= pos_months | p=0.5, n=tot_months)
    from math import comb
    p_binom = sum(comb(tot_months,k)*(0.5**tot_months)
                  for k in range(pos_months, tot_months+1))
    print(f"  Биномиальный тест (H0: p=0.5):  p-value = {p_binom:.4f}")


# ── Task 4: Market Efficiency Test ───────────────────────────────────────────

def task4_market_efficiency(all_rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 4: MARKET EFFICIENCY TEST  (edge_adj buckets внутри Rule C)")
    print("  H0: Величина edge не связана с ROI — edge является noise.")
    print("="*100)

    rc = [b for b in all_rows if RULE_C(b)]
    s_rc = full_stats(rc)
    print(f"\n  Rule C baseline: n={s_rc['n']}  ROI={s_rc['roi']:+.3f}  CLV+={s_rc['clvpos']:.3f}")

    BANDS = [(0.00,0.02),(0.02,0.04),(0.04,0.06),(0.06,0.08),(0.08,0.12),(0.12,0.30)]
    LABELS = ["0-2%","2-4%","4-6%","6-8%","8-12%","12%+"]

    print(f"\n  {'Edge bucket':12}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  "
          f"{'CLV+':>6}  {'avgCLV':>7}  {'mkt_prob':>8}")
    print("  "+"-"*85)

    rois = []
    for (lo,hi),label in zip(BANDS,LABELS):
        bets = [b for b in rc if lo <= b["edge_adj"] < hi]
        if not bets: print(f"  {label:12}  n=0"); continue
        profits = [profit_of(b) for b in bets]
        clvs    = [clv_pos_of(b) for b in bets]
        n = len(bets)
        wr = sum(b["result"] for b in bets)/n
        roi_v = sum(profits)/n
        clvp  = sum(1 for c in clvs if c>0)/n
        avg_clv = sum(clvs)/n
        avg_mkt = sum(b["mp_open"] for b in bets)/n
        bl,bh,_ = bootstrap_ci(profits)
        lo_s = f"{bl:+.3f}" if bl==bl else " nan"
        hi_s = f"{bh:+.3f}" if bh==bh else " nan"
        mark = " ✓" if (bl==bl and bl>0) else "  "
        print(f"  {label:12}  {n:>4}  {wr:>6.3f}  {roi_v:>+7.3f}  [{lo_s},{hi_s}]  "
              f"{clvp:>6.3f}  {avg_clv:>+7.4f}  {avg_mkt:>8.4f}{mark}")
        rois.append((roi_v, n))

    # Monotonicity check: is ROI monotonically increasing with edge?
    valid_rois = [(r,n) for r,n in rois]
    if len(valid_rois) >= 3:
        monotone = all(valid_rois[i][0] <= valid_rois[i+1][0]
                       for i in range(len(valid_rois)-1))
        print(f"\n  Монотонность ROI по edge: {'✓ ДА' if monotone else '✗ НЕТ'}")
        # Spearman rank correlation
        n_bands = len(valid_rois)
        x_ranks = list(range(1, n_bands+1))
        y_vals  = [r for r,_ in valid_rois]
        y_ranks = sorted(range(n_bands), key=lambda i: y_vals[i])
        y_rank_vals = [0]*n_bands
        for rank, idx in enumerate(y_ranks): y_rank_vals[idx] = rank+1
        n_sp = n_bands
        d_sq = sum((x_ranks[i]-y_rank_vals[i])**2 for i in range(n_sp))
        rho = 1 - 6*d_sq/(n_sp*(n_sp**2-1)) if n_sp > 2 else float("nan")
        print(f"  Spearman ρ (edge vs ROI):  {rho:.3f}  "
              f"({'✓ сильная монотонная связь' if rho > 0.7 else '~ слабая связь' if rho > 0.3 else '✗ нет связи'})")

    # Edge > CLV? Если edge настоящий, CLV должен коррелировать с edge
    print(f"\n  Связь edge_adj и CLV (avgCLV по buckets):")
    for (lo,hi),label in zip(BANDS,LABELS):
        bets = [b for b in rc if lo <= b["edge_adj"] < hi]
        if not bets: continue
        clvs = [clv_pos_of(b) for b in bets]
        avg_clv = sum(clvs)/len(clvs)
        clvp = sum(1 for c in clvs if c>0)/len(clvs)
        print(f"    {label:8}: avgCLV={avg_clv:+.4f}  CLV+={clvp:.3f}  n={len(bets)}")


# ── Task 5: H2H Causality Test ────────────────────────────────────────────────

def task5_h2h_causality(all_rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 5: H2H CAUSALITY TEST  (контроль elo_diff)")
    print("  H0: H2H не добавляет информации сверх Elo — H2H повышение объясняется")
    print("      тем что у сильных команд (высокий elo_diff) лучший H2H рекорд.")
    print("="*100)

    # Within same elo_diff band: H2H positive vs H2H neutral
    ELO_BANDS = [(75,125),(125,175),(175,250),(250,9999)]
    ELO_LABELS = ["elo 75-125","elo 125-175","elo 175-250","elo 250+"]

    print(f"\n  H2H positive (delta>+2%) vs H2H neutral (-2%..+2%) — "
          f"в каждом elo_diff диапазоне:")
    print(f"\n  {'Band':18}  {'H2H group':25}  {'n':>4}  {'WR':>6}  {'ROI':>7}  "
          f"{'CI':>18}  {'CLV+':>6}")
    print("  "+"-"*95)

    for (elo_lo,elo_hi), elo_label in zip(ELO_BANDS, ELO_LABELS):
        for h2h_label, h2h_filt in [
            ("H2H повышает >+2%", lambda b: b["h2h_delta"] > 0.02),
            ("H2H нейтрален",     lambda b: abs(b["h2h_delta"]) <= 0.02),
            ("H2H снижает <-2%",  lambda b: b["h2h_delta"] < -0.02),
        ]:
            bets = [b for b in all_rows
                    if RULE_C(b) and elo_lo <= b["elo_diff"] < elo_hi
                    and h2h_filt(b)]
            if not bets:
                print(f"  {elo_label:18}  {h2h_label:25}  n=0")
                continue
            s = full_stats(bets)
            if not s: continue
            lo_s = f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
            hi_s = f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
            mark = " ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
            print(f"  {elo_label:18}  {h2h_label:25}  {s['n']:>4}  {s['wr']:>6.3f}  "
                  f"{s['roi']:>+7.3f}  [{lo_s},{hi_s}]  {s['clvpos']:>6.3f}{mark}")
        print("  "+"-"*95)

    # Overall: does H2H delta add beyond elo_diff correlation?
    print(f"\n  Корреляция h2h_delta и elo_diff (проверяем конфаундер):")
    rc_all = [b for b in all_rows if RULE_C(b)]
    # Pearson r between h2h_delta and elo_diff
    n = len(rc_all)
    if n > 5:
        xm = sum(b["h2h_delta"] for b in rc_all)/n
        ym = sum(b["elo_diff"]  for b in rc_all)/n
        num = sum((b["h2h_delta"]-xm)*(b["elo_diff"]-ym) for b in rc_all)
        sx  = (sum((b["h2h_delta"]-xm)**2 for b in rc_all)/n)**0.5
        sy  = (sum((b["elo_diff"] -ym)**2 for b in rc_all)/n)**0.5
        r   = num/(n*sx*sy) if sx*sy > 0 else 0.0
        print(f"  corr(h2h_delta, elo_diff) = {r:+.3f}  "
              f"({'✗ конфаундер значим' if abs(r)>0.3 else '✓ конфаундер слабый'})")

    # H2H: match-level: does H2H predict CLV independently?
    print(f"\n  H2H delta → CLV: независимый тест (Rule C, все ставки):")
    for h2h_label, h2h_filt in [
        ("H2H повышает >+2%", lambda b: b["h2h_delta"] > 0.02),
        ("H2H нейтрален",     lambda b: abs(b["h2h_delta"]) <= 0.02),
    ]:
        bets = [b for b in all_rows if RULE_C(b) and h2h_filt(b)]
        if not bets: continue
        clvs = [clv_pos_of(b) for b in bets]
        avg_clv = sum(clvs)/len(clvs)
        clvp = sum(1 for c in clvs if c>0)/len(clvs)
        avg_elo = sum(b["elo_diff"] for b in bets)/len(bets)
        print(f"  {h2h_label:28}: n={len(bets):>3}  CLV+={clvp:.3f}  "
              f"avgCLV={avg_clv:+.4f}  avg_elo_diff={avg_elo:.0f}")


# ── Task 6: Paper Trading Expectation ────────────────────────────────────────

def task6_paper_expectation(all_rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 6: PAPER TRADING EXPECTATION  (30 / 60 / 90 дней)")
    print("  Вопрос: если запустить paper daemon завтра, какова вероятность")
    print("  что через 90 дней мы увидим CLV+ > 50% и ROI > 0?")
    print("="*100)

    rc = sorted([b for b in all_rows if RULE_C(b)], key=lambda b: b["begin_at"])
    n_rc = len(rc)
    s_rc = full_stats(rc)

    # Historical bet frequency
    if rc:
        first_dt = datetime.fromisoformat(rc[0]["begin_at"].replace("Z","+00:00"))
        last_dt  = datetime.fromisoformat(rc[-1]["begin_at"].replace("Z","+00:00"))
        span_days = (last_dt - first_dt).days or 1
        bets_per_day = n_rc / span_days
        bets_per_month = bets_per_day * 30
    else:
        bets_per_day = 0; bets_per_month = 0

    print(f"\n  Rule C историческая частота:")
    print(f"    n_total = {n_rc}  за {span_days} дней")
    print(f"    Ставок в день:    {bets_per_day:.3f}")
    print(f"    Ставок в месяц:   {bets_per_month:.1f}")
    print(f"    Ожидаемо за 30 д: {round(bets_per_day*30)}")
    print(f"    Ожидаемо за 60 д: {round(bets_per_day*60)}")
    print(f"    Ожидаемо за 90 д: {round(bets_per_day*90)}")

    # Monte Carlo simulation from historical distribution
    profits_hist = [profit_of(b) for b in rc]
    clvs_hist    = [clv_pos_of(b) for b in rc]  # CLV independent of result
    clv_labels   = [1 if c > 0 else 0 for c in clvs_hist]

    print(f"\n  Исторические метрики Rule C:")
    print(f"    ROI    = {s_rc['roi']:+.4f}")
    print(f"    CLV+   = {s_rc['clvpos']:.3f}")
    print(f"    avgCLV = {s_rc['avg_clv']:+.4f}")

    N_SIM = 10_000
    rng = random.Random(SEED + 2)

    print(f"\n  Monte Carlo симуляция (n_sim={N_SIM:,}, выборка из исторических прибылей):")
    print(f"\n  {'Горизонт':10}  {'n_exp':>5}  {'P(ROI>0)':>9}  {'P(CLV+>50%)':>12}  "
          f"{'E[ROI]':>7}  {'CI E[ROI]':>14}  {'P(ROI>+5%)':>11}")
    print("  "+"-"*80)

    for days, n_exp in [(30, max(1,round(bets_per_day*30))),
                        (60, max(1,round(bets_per_day*60))),
                        (90, max(1,round(bets_per_day*90)))]:
        if not profits_hist:
            print(f"  {days}д:   нет данных"); continue

        sim_rois = []
        sim_clvpos = []
        for _ in range(N_SIM):
            sample_p = rng.choices(profits_hist, k=n_exp)
            sample_c = rng.choices(clv_labels,  k=n_exp)
            sim_rois.append(sum(sample_p)/n_exp)
            sim_clvpos.append(sum(sample_c)/n_exp)

        p_roi_pos = sum(1 for r in sim_rois if r > 0) / N_SIM
        p_roi_5   = sum(1 for r in sim_rois if r > 0.05) / N_SIM
        p_clvpos  = sum(1 for c in sim_clvpos if c > 0.5) / N_SIM
        e_roi     = sum(sim_rois) / N_SIM
        sim_rois_s = sorted(sim_rois)
        ci_lo = sim_rois_s[int(0.025*N_SIM)]
        ci_hi = sim_rois_s[int(0.975*N_SIM)]

        mark_c = " ✓" if p_clvpos > 0.70 else ("~" if p_clvpos > 0.50 else " ✗")
        print(f"  {days}д ({n_exp} ст.)  {n_exp:>5}  {p_roi_pos:>9.1%}  "
              f"{p_clvpos:>12.1%}  {e_roi:>+7.3f}  "
              f"[{ci_lo:+.3f},{ci_hi:+.3f}]{mark_c}  {p_roi_5:>11.1%}")

    # Stop-loss analysis: when should we pull the plug?
    print(f"\n  Анализ стоп-сигнала: при каком результате стоп?")
    print(f"  (сколько убыточных ставок подряд ожидается при ROI=+0.26)")
    if profits_hist:
        # Simulate streaks
        rng2 = random.Random(SEED + 3)
        max_losing_streaks = []
        for _ in range(N_SIM):
            sample = rng2.choices(profits_hist, k=50)
            max_ls = cur_ls = 0
            for p in sample:
                if p < 0: cur_ls += 1; max_ls = max(max_ls, cur_ls)
                else: cur_ls = 0
            max_losing_streaks.append(max_ls)
        avg_mls = sum(max_losing_streaks)/N_SIM
        p_ls3   = sum(1 for s in max_losing_streaks if s >= 3) / N_SIM
        p_ls5   = sum(1 for s in max_losing_streaks if s >= 5) / N_SIM
        print(f"    В симуляции 50 ставок:")
        print(f"      Avg max losing streak:    {avg_mls:.1f}")
        print(f"      P(losing streak ≥ 3):     {p_ls3:.1%}")
        print(f"      P(losing streak ≥ 5):     {p_ls5:.1%}")
        print(f"    → Рекомендация: стоп после 5+ подряд убытков при 20+ ставках")

    # Monthly CLV+ expectation
    print(f"\n  CLV+ ожидание по месяцам  (помесячная variability):")
    by_month = defaultdict(list)
    for b in rc: by_month[b["month"]].append(clv_pos_of(b))
    for m in sorted(by_month.keys()):
        clvs = by_month[m]
        avg_c = sum(1 for c in clvs if c>0)/len(clvs)
        print(f"    {m}: CLV+={avg_c:.3f}  n={len(clvs)}")

    print(f"\n  ВЫВОД:")
    if bets_per_day > 0:
        n90 = round(bets_per_day*90)
        # Quick estimate
        p_clv_90 = None
        if profits_hist:
            rng3 = random.Random(SEED+4)
            sims = []
            for _ in range(N_SIM):
                sample_c = rng3.choices(clv_labels, k=n90)
                sims.append(sum(sample_c)/n90)
            p_clv_90 = sum(1 for c in sims if c > 0.5)/N_SIM
        print(f"    За 90 дней ожидается ~{n90} ставок.")
        if p_clv_90:
            if p_clv_90 > 0.80:
                print(f"    P(CLV+>50% за 90 дней) = {p_clv_90:.1%}  ✓ ВЫСОКИЙ — запускать daemon")
            elif p_clv_90 > 0.60:
                print(f"    P(CLV+>50% за 90 дней) = {p_clv_90:.1%}  ~ УМЕРЕННЫЙ — с осторожностью")
            else:
                print(f"    P(CLV+>50% за 90 дней) = {p_clv_90:.1%}  ✗ НИЗКИЙ — не запускать")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["1","2","3","4","5","6"])
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*100}")
    print("DESTRUCTION TEST — dota_trader_v2  (GPT Report #8)")
    print(f"{'='*100}")
    print("Загружаем данные...", flush=True)
    all_rows = build_all_bets(conn)
    conn.close()

    rc = [b for b in all_rows if RULE_C(b)]
    print(f"  Матчей: {len(all_rows)}  |  Rule C ставок: {len(rc)}\n")
    print(f"  Rule C: edge_adj>0  elo_diff>=75  odds<2.0  market_prob 60-70%")

    run = args.only
    if run is None or run=="1": task1_permutation(all_rows)
    if run is None or run=="2": task2_random_rules(all_rows)
    if run is None or run=="3": task3_walk_forward(all_rows)
    if run is None or run=="4": task4_market_efficiency(all_rows)
    if run is None or run=="5": task5_h2h_causality(all_rows)
    if run is None or run=="6": task6_paper_expectation(all_rows)

    print(f"\n{'='*100}")
    print("Готово.")

if __name__ == "__main__":
    main()
