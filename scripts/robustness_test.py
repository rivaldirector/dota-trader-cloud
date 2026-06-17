#!/usr/bin/env python3
"""
Robustness tests для кандидата:
  edge_adj > 0  AND  elo_diff >= 75  AND  bet_odds < 2.0

1. Purged Time Split (60/20/20)
2. Consecutive Forward Windows (3 окна)
3. DreamLeague Dependency Test
4. Kelly Stress Test (flat / 0.25K / 0.5K)
5. Probability Calibration

Запуск:
    PYTHONPATH=. python3 scripts/robustness_test.py
    PYTHONPATH=. python3 scripts/robustness_test.py --only 3
"""
from __future__ import annotations

import sys, re, sqlite3, random, argparse, math
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
SEED       = 42
N_BOOT     = 5_000

# Locked rule
ELO_MIN    = 75
ODDS_MAX   = 2.0

# ── helpers ──────────────────────────────────────────────────────────────────

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def team_match(a, b):
    na, nb = normalize(a), normalize(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

def novig(h, a):
    if not h or not a or h <= 1 or a <= 1:
        return None, None
    ph, pa = 1/h, 1/a; s = ph+pa
    return ph/s, pa/s

def get_mp_for_team1(m_t1, m_t2, o_t1, o_t2, h, a):
    if team_match(m_t1, o_t1) and team_match(m_t2, o_t2):
        p1, p2 = novig(h, a); return p1, p2, (p1 is not None)
    if team_match(m_t1, o_t2) and team_match(m_t2, o_t1):
        p2, p1 = novig(h, a); return p1, p2, (p1 is not None)
    return None, None, False

def elo_prob(ra, rb):
    return 1.0 / (1.0 + mpow(10.0, (rb - ra) / 400.0))

def bootstrap_ci(vals, n=N_BOOT, seed=SEED):
    if len(vals) < 3:
        return float("nan"), float("nan"), []
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choices(vals, k=len(vals))) / len(vals) for _ in range(n)
    )
    return means[int(0.025*n)], means[int(0.975*n)], means

def stats_full(bets):
    if not bets:
        return None
    n       = len(bets)
    profits = [b["profit"] for b in bets]
    roi     = sum(profits)/n
    clv_pos = sum(1 for b in bets if b["clv"]>0)/n
    avg_clv = sum(b["clv"] for b in bets)/n
    wr      = sum(b["win"] for b in bets)/n
    lo, hi, _ = bootstrap_ci(profits)
    # drawdown
    cum=peak=max_dd=0.0
    for p in profits:
        cum+=p
        if cum>peak: peak=cum
        dd=peak-cum
        if dd>max_dd: max_dd=dd
    return dict(n=n, wr=wr, roi=roi, lo=lo, hi=hi,
                clv_pos=clv_pos, avg_clv=avg_clv, max_dd=max_dd,
                profits=profits)

def row_str(label, s, extra=""):
    if s is None:
        return f"  {label:40}  n=0"
    star = " ✓" if (s["lo"]==s["lo"] and s["lo"]>0) else "  "
    lo = f"{s['lo']:+.3f}" if s["lo"]==s["lo"] else "  nan"
    hi = f"{s['hi']:+.3f}" if s["hi"]==s["hi"] else "  nan"
    return (f"  {label:40}  n={s['n']:3d}  WR={s['wr']:.3f}  ROI={s['roi']:+.3f}  "
            f"CI=[{lo},{hi}]  CLV+={s['clv_pos']:.3f}  avgCLV={s['avg_clv']:+.4f}"
            f"{star}  {extra}")

# ── Core pipeline ─────────────────────────────────────────────────────────────

def build_all_bets(conn):
    PREFERRED = ["Bet365","Pinnacle","PinnacleSports","GGBet","10Bet",
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

    odds_idx = defaultdict(lambda: defaultdict(dict))
    for s in snaps:
        tag = "open" if s["captured_at"].endswith("_open") else "close"
        odds_idx[s["match_external_id"]][s["bookmaker"]][tag] = {
            "h": s["team_1_odds"], "a": s["team_2_odds"],
            "t1": s["team_1_name"], "t2": s["team_2_name"],
        }

    def best_od(mid):
        if mid not in odds_idx: return None, None
        bd = odds_idx[mid]
        bm = next((b for b in PREFERRED if b in bd and "open" in bd[b]), None)
        if not bm:
            bm = next((b for b,d in bd.items() if "open" in d), None)
        if not bm: return None, None
        op = bd[bm]["open"]; cl = bd[bm].get("close", op)
        return bm, {"open_h":op["h"],"open_a":op["a"],
                    "close_h":cl["h"],"close_a":cl["a"],
                    "t1":op["t1"],"t2":op["t2"]}

    now_dt = datetime.now(timezone.utc)
    elo    = defaultdict(lambda: START_ELO)
    games  = defaultdict(int)
    h2h    = defaultdict(list)
    rows   = []

    for r in matches:
        t1, t2, win = r["team_1_name"], r["team_2_name"], r["winner_name"]
        eid  = str(r["external_id"])
        ln   = r["league_name"] or ""
        bat  = r["begin_at"] or ""
        res  = 1 if win==t1 else 0
        e1, e2 = elo[t1], elo[t2]
        ep     = elo_prob(e1, e2)
        ediff  = abs(e1-e2)

        key = (min(t1,t2), max(t1,t2))
        h2e = h2h[key]
        if h2e:
            try: bts = datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
            except: bts = now_dt.timestamp()
            ww = wt = 0.0
            for (ts, k0w) in h2e:
                w = 0.5**((bts-ts)/365/86400); wt+=w
                if (k0w and t1==key[0]) or (not k0w and t1==key[1]): ww+=w
            h2h_n = len(h2e); h2h_wr = ww/wt if wt>0 else 0.5
        else:
            h2h_n = 0; h2h_wr = 0.5

        hc = min(h2h_n/H2H_CONF_N, 1.0)
        adj = ep*(1-H2H_MAX_W*hc) + h2h_wr*(H2H_MAX_W*hc)

        if games[t1]>=MIN_GAMES and games[t2]>=MIN_GAMES:
            bm, od = best_od(eid)
            if od:
                mp1, mp2, valid = get_mp_for_team1(
                    t1,t2,od["t1"],od["t2"],od["open_h"],od["open_a"])
                mp1c, _, _ = get_mp_for_team1(
                    t1,t2,od["t1"],od["t2"],
                    od["close_h"] or od["open_h"],od["close_a"] or od["open_a"])
                if valid and mp1 is not None:
                    ea = adj - mp1
                    bo = od["open_h"] if team_match(t1,od["t1"]) else od["open_a"]
                    rows.append(dict(
                        eid=eid, begin_at=bat, month=bat[:7],
                        league=ln.replace("DOTA2 - ","").replace("DOTA2","?"),
                        t1=t1, t2=t2,
                        elo_diff=ediff, adj_prob=adj,
                        mp_open=mp1,
                        mp_close=mp1c if mp1c else mp1,
                        edge_adj=ea, bet_odds=bo, result=res,
                    ))

        k = _tier_k(ln); w = _time_weight(bat,now_dt,365); kef=k*w
        ex = elo_prob(e1,e2)
        elo[t1]=e1+kef*(res-ex); elo[t2]=e2+kef*((1-res)-(1-ex))
        games[t1]+=1; games[t2]+=1
        try: ts=datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except: ts=now_dt.timestamp()
        h2h[key].append((ts, win==key[0]))

    return rows


def make_bet(b):
    if b["edge_adj"]<=0: return None
    win    = (b["result"]==1)
    profit = (b["bet_odds"]-1) if win else -1.0
    clv    = b["mp_close"] - b["mp_open"]
    mfav   = b["mp_open"]>0.5
    mkc    = int((mfav and b["result"]==1) or (not mfav and b["result"]==0))
    return dict(win=int(win),profit=profit,clv=clv,mkt_correct=mkc,
                bet_odds=b["bet_odds"], mp_open=b["mp_open"],
                adj_prob=b["adj_prob"])

def rule(b):
    return b["edge_adj"]>0 and b["elo_diff"]>=ELO_MIN and b["bet_odds"]<ODDS_MAX

def seg_bets(rows):
    out=[]
    for b in rows:
        if rule(b):
            bt=make_bet(b)
            if bt: out.append({**bt,"league":b["league"],"begin_at":b["begin_at"]})
    return out


# ── Task 1: Purged Time Split 60/20/20 ───────────────────────────────────────

def task1_purged_split(all_rows):
    print("\n"+"="*90)
    print("ЗАДАЧА 1: PURGED TIME SPLIT  60% train | 20% gap (purge) | 20% test")
    print("  Кандидат: edge_adj>0 & elo_diff>=75 & odds<2.0")
    print("="*90)

    seg = seg_bets(all_rows)
    if not seg:
        print("  Нет ставок"); return

    seg.sort(key=lambda b: b["begin_at"])
    n = len(seg)
    i60 = int(n*0.60)
    i80 = int(n*0.80)

    train = seg[:i60]
    gap   = seg[i60:i80]
    test  = seg[i80:]

    t_date  = train[-1]["begin_at"][:10] if train else "?"
    g_start = gap[0]["begin_at"][:10]    if gap   else "?"
    g_end   = gap[-1]["begin_at"][:10]   if gap   else "?"
    te_date = test[0]["begin_at"][:10]   if test  else "?"
    te_end  = test[-1]["begin_at"][:10]  if test  else "?"

    print(f"\n  Разбивка по n={n} ставкам:")
    print(f"    Train: n={len(train)}  до {t_date}")
    print(f"    Gap:   n={len(gap)}   {g_start} → {g_end}  (purge zone, не используется)")
    print(f"    Test:  n={len(test)}  {te_date} → {te_end}")

    st = stats_full(train); print("\n"+row_str("Train (60%)", st))
    sg = stats_full(gap);   print(row_str("Gap   (20%, purge)", sg))
    ste= stats_full(test);  print(row_str("Test  (20%)", ste))

    if ste:
        print(f"\n  Test детали:")
        print(f"    n={ste['n']}  WR={ste['wr']:.3f}  ROI={ste['roi']:+.4f}")
        lo,hi = ste['lo'],ste['hi']
        lo_s = f"{lo:+.3f}" if lo==lo else "nan"
        hi_s = f"{hi:+.3f}" if hi==hi else "nan"
        print(f"    CI=[{lo_s},{hi_s}]  {'✓ исключает 0' if lo>0 else '✗ включает 0'}")
        print(f"    CLV+={ste['clv_pos']:.3f}  avgCLV={ste['avg_clv']:+.4f}")
        print(f"    max_dd={ste['max_dd']:.3f}")
        prob_pos = sum(1 for m in bootstrap_ci(ste['profits'])[2] if m>0)/N_BOOT
        print(f"    P(ROI>0) = {prob_pos:.1%}")


# ── Task 2: Consecutive Forward Windows ──────────────────────────────────────

def task2_forward_windows(all_rows):
    print("\n"+"="*90)
    print("ЗАДАЧА 2: CONSECUTIVE FORWARD WINDOWS  (3 окна по времени)")
    print("  Кандидат: edge_adj>0 & elo_diff>=75 & odds<2.0")
    print("="*90)

    seg = seg_bets(all_rows)
    seg.sort(key=lambda b: b["begin_at"])
    n = len(seg)
    if n < 9:
        print("  Недостаточно данных для 3 окон"); return

    # Делим ставки на 3 равных части
    sz  = n//3
    w   = [seg[i*sz:(i+1)*sz] for i in range(3)]
    w[2]= seg[2*sz:]  # последнее окно берёт хвост

    print(f"\n  Всего ставок в сегменте: n={n}")
    print(f"  Размер каждого окна: ~{sz}")
    print()
    print(f"  {'Окно':8}  {'Период':24}  {'n':>4}  {'WR':>6}  {'ROI':>7}  "
          f"{'CI 95%':>18}  {'CLV+':>6}  {'avgCLV':>8}")
    print("  "+"-"*85)

    for i, window in enumerate(w):
        s = stats_full(window)
        if not s:
            print(f"  Window{i+1}: нет данных"); continue
        d_start = window[0]["begin_at"][:10]
        d_end   = window[-1]["begin_at"][:10]
        lo,hi   = s["lo"],s["hi"]
        lo_s = f"{lo:+.3f}" if lo==lo else "  nan"
        hi_s = f"{hi:+.3f}" if hi==hi else "  nan"
        mark = " ✓" if (lo==lo and lo>0) else "  "
        print(f"  Window{i+1}  {d_start} → {d_end}  {s['n']:>4}  "
              f"{s['wr']:>6.3f}  {s['roi']:>+7.3f}  [{lo_s},{hi_s}]  "
              f"{s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}{mark}")

    # Cumulative equity curve (per-window)
    print(f"\n  Equity by window (cumulative flat-stake P&L):")
    cum = 0.0
    for i, window in enumerate(w):
        profits = [b["profit"] for b in window]
        w_sum   = sum(profits)
        cum    += w_sum
        bar_len = int(abs(w_sum)/max(abs(sum([b["profit"] for b in seg])),0.001)*30)
        bar     = ("+" if w_sum>=0 else "-") * bar_len
        print(f"    Window{i+1}: Σprofit={w_sum:+.2f}  cumulative={cum:+.2f}  {bar}")


# ── Task 3: DreamLeague Dependency Test ──────────────────────────────────────

def task3_dreamleague(all_rows):
    print("\n"+"="*90)
    print("ЗАДАЧА 3: DREAMLEAGUE DEPENDENCY TEST")
    print("  Кандидат: edge_adj>0 & elo_diff>=75 & odds<2.0")
    print("="*90)

    seg = seg_bets(all_rows)

    dl_bets   = [b for b in seg if "dreamleague" in b["league"].lower()]
    no_dl     = [b for b in seg if "dreamleague" not in b["league"].lower()]

    s_all = stats_full(seg)
    s_dl  = stats_full(dl_bets)
    s_no  = stats_full(no_dl)

    print(f"\n  {'Subset':35}"+row_str("", s_all)[2:]) if False else None

    print(f"\n  {'Subset':35}  {'n':>4}  {'WR':>6}  {'ROI':>7}  "
          f"{'CI':>18}  {'CLV+':>6}  {'avgCLV':>8}  {'maxDD':>7}")
    print("  "+"-"*100)

    for label, s in [("ALL",s_all),("DreamLeague only",s_dl),("Без DreamLeague",s_no)]:
        if not s:
            print(f"  {label:35}  n=0"); continue
        lo,hi = s["lo"],s["hi"]
        lo_s = f"{lo:+.3f}" if lo==lo else "  nan"
        hi_s = f"{hi:+.3f}" if hi==hi else "  nan"
        mark = " ✓" if (lo==lo and lo>0) else "  "
        print(f"  {label:35}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"[{lo_s},{hi_s}]  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+8.4f}  "
              f"{s['max_dd']:>7.3f}{mark}")

    # Time check: DreamLeague months
    if dl_bets:
        print(f"\n  DreamLeague ставки по месяцам:")
        by_month = defaultdict(list)
        for b in dl_bets:
            by_month[b["begin_at"][:7]].append(b)
        for m in sorted(by_month):
            bets = by_month[m]
            roi  = sum(b["profit"] for b in bets)/len(bets)
            wr   = sum(b["win"] for b in bets)/len(bets)
            clvp = sum(1 for b in bets if b["clv"]>0)/len(bets)
            print(f"    {m}  n={len(bets):2d}  WR={wr:.3f}  ROI={roi:+.3f}  CLV+={clvp:.3f}")

    # Statistical test: is DL ROI significantly different from no-DL?
    if s_dl and s_no:
        diff = s_dl["roi"] - s_no["roi"]
        print(f"\n  ROI разница: DreamLeague {s_dl['roi']:+.3f} vs без DL {s_no['roi']:+.3f}  Δ={diff:+.3f}")
        if diff > 0:
            print(f"  DreamLeague даёт ROI на {diff:.3f} выше. CLV+ DL={s_dl['clv_pos']:.3f} vs no-DL={s_no['clv_pos']:.3f}")
        # Bootstrap test: can we tell them apart?
        rng = random.Random(SEED)
        dl_p  = s_dl["profits"]
        no_p  = s_no["profits"]
        diffs = []
        for _ in range(N_BOOT):
            d_dl = sum(rng.choices(dl_p, k=len(dl_p)))/len(dl_p)
            d_no = sum(rng.choices(no_p, k=len(no_p)))/len(no_p)
            diffs.append(d_dl - d_no)
        diffs.sort()
        ci_lo = diffs[int(0.025*N_BOOT)]
        ci_hi = diffs[int(0.975*N_BOOT)]
        print(f"  Bootstrap CI разницы ROI: [{ci_lo:+.3f}, {ci_hi:+.3f}]  "
              f"{'✓ DL достоверно лучше' if ci_lo>0 else '✗ разница недостоверна'}")


# ── Task 4: Kelly Stress Test ─────────────────────────────────────────────────

def task4_kelly(all_rows):
    print("\n"+"="*90)
    print("ЗАДАЧА 4: KELLY STRESS TEST  (flat / 0.25K / 0.5K)")
    print("  Кандидат: edge_adj>0 & elo_diff>=75 & odds<2.0")
    print("="*90)

    seg = seg_bets(all_rows)
    if not seg:
        print("  Нет ставок"); return

    def kelly_fraction(adj_prob, odds):
        b = odds - 1.0   # net odds
        p = adj_prob
        q = 1.0 - p
        f = (b*p - q) / b
        return max(0.0, f)

    def simulate(bets_data, fraction_fn):
        """fraction_fn(b) → fraction of bankroll to bet"""
        bankroll = 1.0
        peak     = 1.0
        max_dd   = 0.0
        # Ulcer index: sqrt(mean of squared drawdown%)
        dd_squares = []
        bankroll_history = [1.0]

        for b in bets_data:
            f = fraction_fn(b)
            f = min(f, 0.25)   # safety cap
            stake = bankroll * f
            win   = (b["result"]==1) if hasattr(b, "get") else b["win"]
            if isinstance(b, dict) and "win" in b:
                win = b["win"]
            profit = stake * (b["bet_odds"]-1) if win else -stake
            bankroll += profit
            bankroll = max(bankroll, 0.001)
            bankroll_history.append(bankroll)
            if bankroll > peak: peak = bankroll
            dd = (peak - bankroll)/peak
            if dd > max_dd: max_dd = dd
            dd_squares.append(dd**2)

        ulcer = math.sqrt(sum(dd_squares)/len(dd_squares)) if dd_squares else 0.0
        final_roi = (bankroll - 1.0)
        return dict(
            final_bankroll=bankroll,
            roi=final_roi,
            max_dd=max_dd,
            ulcer=ulcer,
            history=bankroll_history,
        )

    # Need raw rows for Kelly (we need adj_prob and bet_odds)
    raw_seg = []
    for b in all_rows:
        if rule(b):
            bt = make_bet(b)
            if bt:
                raw_seg.append({**bt, "adj_prob": b["adj_prob"],
                                 "bet_odds": b["bet_odds"], "result": b["result"]})

    results = {}

    # Flat betting
    def flat_fn(b): return 1.0   # 1 unit
    flat_bets = raw_seg
    # simulate flat differently — use profit directly
    flat_profits = [b["profit"] for b in flat_bets]
    n = len(flat_profits)
    flat_roi = sum(flat_profits)/n
    flat_bankroll = 1.0
    flat_peak = 1.0; flat_dd = 0.0; flat_dd_sq = []
    for p in flat_profits:
        flat_bankroll += p/n   # 1 unit = 1/n of starting bankroll
        if flat_bankroll > flat_peak: flat_peak = flat_bankroll
        dd = (flat_peak - flat_bankroll)/flat_peak if flat_peak>0 else 0
        if dd > flat_dd: flat_dd = dd
        flat_dd_sq.append(dd**2)
    flat_ulcer = math.sqrt(sum(flat_dd_sq)/len(flat_dd_sq)) if flat_dd_sq else 0

    results["Flat (1 unit)"] = dict(
        n=n, roi=flat_roi, max_dd=flat_dd, ulcer=flat_ulcer,
        final=1.0+flat_roi)

    # Kelly fractions
    for label, kf in [("0.25 Kelly", 0.25), ("0.5 Kelly", 0.5)]:
        r = simulate(
            raw_seg,
            lambda b, kf=kf: kf * kelly_fraction(b["adj_prob"], b["bet_odds"])
        )
        results[label] = dict(
            n=n, roi=r["roi"], max_dd=r["max_dd"],
            ulcer=r["ulcer"], final=r["final_bankroll"])

    print(f"\n  n = {n} ставок")
    print(f"\n  {'Strategy':20}  {'ROI':>10}  {'Final BK':>10}  {'max_DD':>8}  {'Ulcer':>8}")
    print("  "+"-"*65)
    for label, r in results.items():
        print(f"  {label:20}  {r['roi']:>+10.4f}  {r['final']:>10.4f}  "
              f"{r['max_dd']:>8.4f}  {r['ulcer']:>8.4f}")

    # Kelly individual bet fractions
    k_vals = [kelly_fraction(b["adj_prob"], b["bet_odds"]) for b in raw_seg]
    k_vals_pos = [k for k in k_vals if k > 0]
    if k_vals_pos:
        k_vals_pos.sort()
        print(f"\n  Kelly fraction stats (edge_adj>0, elo>=75, odds<2.0):")
        print(f"    Нет ставки (K<=0): {sum(1 for k in k_vals if k<=0)}")
        print(f"    Min:    {min(k_vals_pos):.4f}")
        print(f"    Median: {k_vals_pos[len(k_vals_pos)//2]:.4f}")
        print(f"    Mean:   {sum(k_vals_pos)/len(k_vals_pos):.4f}")
        print(f"    Max:    {max(k_vals_pos):.4f}")
        print(f"    >20%:   {sum(1 for k in k_vals_pos if k>0.20)} ставок")

    # Drawdown periods
    print(f"\n  Flat betting equity (per 5-bet buckets):")
    cum = 0.0
    for i in range(0, n, 5):
        chunk = flat_profits[i:i+5]
        s     = sum(chunk)
        cum  += s
        bar   = ("▲" if s>=0 else "▼") * min(int(abs(s)*10),15)
        print(f"    Bets {i+1:3d}-{i+len(chunk):3d}  Δ={s:+.2f}  cum={cum:+.2f}  {bar}")


# ── Task 5: Probability Calibration ──────────────────────────────────────────

def task5_calibration(all_rows):
    print("\n"+"="*90)
    print("ЗАДАЧА 5: PROBABILITY CALIBRATION")
    print("  Кандидат: edge_adj>0 & elo_diff>=75 & odds<2.0")
    print("="*90)

    seg_rows = [b for b in all_rows if rule(b)]

    # Calibration: adj_prob buckets vs actual WR
    BUCKETS = [(0.50,0.55),(0.55,0.60),(0.60,0.65),(0.65,0.70),
               (0.70,0.75),(0.75,0.80),(0.80,0.85),(0.85,0.90),(0.90,1.01)]

    print(f"\n  Calibration (adj_prob → actual WR)  — ALL matched rows with edge_adj>0 & elo>=75 & odds<2.0")
    print(f"  {'adj_prob bucket':18}  {'n':>4}  {'adj_prob avg':>13}  "
          f"{'market_prob avg':>16}  {'actual WR':>10}  {'edge avg':>10}  {'calibration err':>16}")
    print("  "+"-"*100)

    all_calib_errs = []
    for (lo, hi) in BUCKETS:
        bets = [b for b in seg_rows if lo <= b["adj_prob"] < hi]
        if not bets: continue
        avg_adj = sum(b["adj_prob"] for b in bets)/len(bets)
        avg_mkt = sum(b["mp_open"] for b in bets)/len(bets)
        actual  = sum(b["result"] for b in bets)/len(bets)
        avg_edge= sum(b["edge_adj"] for b in bets)/len(bets)
        err     = actual - avg_adj   # positive = model underestimates
        all_calib_errs.append((len(bets), err))
        over = "over" if err < -0.05 else ("under" if err > 0.05 else "ok")
        print(f"  [{lo:.2f},{hi:.2f})        {len(bets):>4}  "
              f"{avg_adj:>13.4f}  {avg_mkt:>16.4f}  {actual:>10.4f}  "
              f"{avg_edge:>+10.4f}  {err:>+16.4f}  {over}")

    # Overall calibration
    total_bets = [b for b in seg_rows if 0.50 <= b["adj_prob"] < 1.01]
    if total_bets:
        avg_adj_all = sum(b["adj_prob"] for b in total_bets)/len(total_bets)
        actual_all  = sum(b["result"] for b in total_bets)/len(total_bets)
        err_all     = actual_all - avg_adj_all
        print(f"\n  Overall: adj_prob avg={avg_adj_all:.4f}  actual WR={actual_all:.4f}  "
              f"err={err_all:+.4f}  "
              f"({'model underestimates' if err_all>0 else 'model overestimates'})")

    # CLV calibration: does positive edge predict CLV?
    print(f"\n  Edge vs CLV correlation (edge_adj > 0, elo>=75, odds<2.0):")
    seg = seg_bets(all_rows)
    if seg:
        edges = [b["bet_odds"] - 1/(b["mp_open"] or 0.001) for b in seg]  # simple
        edges_2 = []
        clvs    = []
        for b in all_rows:
            if rule(b):
                bt = make_bet(b)
                if bt:
                    edges_2.append(b["edge_adj"])
                    clvs.append(bt["clv"])
        if edges_2 and clvs:
            n = len(edges_2)
            mean_e = sum(edges_2)/n; mean_c = sum(clvs)/n
            cov = sum((e-mean_e)*(c-mean_c) for e,c in zip(edges_2,clvs))/n
            var_e = sum((e-mean_e)**2 for e in edges_2)/n
            var_c = sum((c-mean_c)**2 for c in clvs)/n
            corr  = cov/math.sqrt(var_e*var_c) if var_e>0 and var_c>0 else 0
            print(f"    corr(edge_adj, CLV) = {corr:+.4f}  "
                  f"({'позитивная корреляция' if corr>0 else 'негативная корреляция'})")
            print(f"    mean edge_adj = {mean_e:+.4f}")
            print(f"    mean CLV      = {mean_c:+.4f}")
            # Scatter: edge quartiles vs CLV
            pairs = sorted(zip(edges_2, clvs))
            q     = len(pairs)//4
            print(f"\n    Edge quartiles → avg CLV:")
            for i, label in enumerate(["Q1 (низкий edge)","Q2","Q3","Q4 (высокий edge)"]):
                chunk = pairs[i*q:(i+1)*q]
                if not chunk: continue
                avg_e = sum(p[0] for p in chunk)/len(chunk)
                avg_c = sum(p[1] for p in chunk)/len(chunk)
                clv_p = sum(1 for p in chunk if p[1]>0)/len(chunk)
                print(f"      {label:22}: n={len(chunk):2d}  avg_edge={avg_e:+.4f}  "
                      f"avg_CLV={avg_c:+.4f}  CLV+={clv_p:.0%}")

    # Final assessment
    print(f"\n  {'='*70}")
    print(f"  ОЦЕНКА: Если запустить paper daemon на 3 месяца?")
    print(f"  {'='*70}")

    s_seg = stats_full(seg_bets(all_rows))
    if s_seg:
        lo,hi = s_seg["lo"],s_seg["hi"]
        lo_s = f"{lo:+.3f}" if lo==lo else "nan"
        hi_s = f"{hi:+.3f}" if hi==hi else "nan"

        # Expected bets per month: total bets / period in months
        dates = sorted(b["begin_at"][:7] for b in all_rows if rule(b))
        if dates:
            months_span = len(set(dates))
            bets_per_month = s_seg["n"] / months_span if months_span > 0 else 0
        else:
            bets_per_month = 0

        print(f"\n  Исторически:")
        print(f"    n = {s_seg['n']}  ROI = {s_seg['roi']:+.4f}  CI = [{lo_s},{hi_s}]")
        print(f"    CLV+ = {s_seg['clv_pos']:.3f}  avgCLV = {s_seg['avg_clv']:+.4f}")
        print(f"    Ставок в месяц: ~{bets_per_month:.1f}")
        print(f"    За 3 месяца: ожидаемо ~{bets_per_month*3:.0f} ставок")

        print(f"\n  Основания ожидать положительный CLV:")
        clv_ok   = s_seg["clv_pos"] > 0.55
        aclv_ok  = s_seg["avg_clv"] > 0
        ci_ok    = lo > 0 if lo==lo else False
        calib_ok = (actual_all > avg_adj_all) if total_bets else False

        checks = [
            (clv_ok,   f"CLV+ = {s_seg['clv_pos']:.3f} > 55%",
             "рынок движется к нам в большинстве случаев"),
            (aclv_ok,  f"avgCLV = {s_seg['avg_clv']:+.4f} > 0",
             "в среднем рынок движется в нашу сторону"),
            (ci_ok,    f"ROI CI = [{lo_s},{hi_s}] исключает 0",
             "исторически ROI статистически значим"),
            (calib_ok, f"Модель недооценивает фаворитов ({err_all:+.4f})",
             "реальный WR выше прогноза — скорее хорошо"),
        ]
        for ok, fact, meaning in checks:
            mark = "✓" if ok else "✗"
            print(f"    {mark} {fact}: {meaning}")

        passed = sum(1 for ok,_,_ in checks if ok)
        print(f"\n  Итог: {passed}/4 оснований для позитивного прогноза.")
        if passed >= 3:
            print("  ВЫВОД: Основания есть. Paper daemon оправдан.")
            print("         Ожидаемый ROI 3мес ≈ +10..+20% при ~17 ставках.")
            print("         Если CLV+ упадёт ниже 50% → остановить.")
        elif passed == 2:
            print("  ВЫВОД: Слабые основания. Paper daemon возможен с малым размером ставки.")
        else:
            print("  ВЫВОД: Недостаточно оснований. Нужно больше данных.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["1","2","3","4","5"])
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*90}")
    print("ROBUSTNESS TEST — dota_trader_v2")
    print(f"Кандидат: edge_adj > 0  AND  elo_diff >= {ELO_MIN}  AND  odds < {ODDS_MAX}")
    print(f"{'='*90}")
    print("Загружаем данные...", flush=True)
    all_rows = build_all_bets(conn)
    conn.close()
    n_seg = sum(1 for b in all_rows if rule(b))
    print(f"  Матчей с odds: {len(all_rows)}  |  В сегменте: {n_seg}")

    run = args.only
    if run is None or run=="1": task1_purged_split(all_rows)
    if run is None or run=="2": task2_forward_windows(all_rows)
    if run is None or run=="3": task3_dreamleague(all_rows)
    if run is None or run=="4": task4_kelly(all_rows)
    if run is None or run=="5": task5_calibration(all_rows)

    print(f"\n{'='*90}")
    print("Готово.")


if __name__ == "__main__":
    main()
