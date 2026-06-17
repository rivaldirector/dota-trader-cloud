#!/usr/bin/env python3
"""
Decomposition analysis: что создаёт edge?
  1. Elo Strength Analysis  (elo_diff buckets)
  2. H2H Contribution       (h2h_n buckets)
  3. Edge Size Analysis      (edge quartiles)
  4. Market Probability      (market_prob buckets)
  5. Forward Simulation      (3 правила × purged test)

Запуск:
    PYTHONPATH=. python3 scripts/decomposition_analysis.py
    PYTHONPATH=. python3 scripts/decomposition_analysis.py --only 2
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
N_BOOT     = 5_000
SEED       = 42

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

def bootstrap_ci(vals, n=N_BOOT, seed=SEED):
    if len(vals) < 3: return float("nan"), float("nan"), []
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(vals,k=len(vals)))/len(vals) for _ in range(n))
    return means[int(0.025*n)], means[int(0.975*n)], means

def stats(bets):
    if not bets: return None
    n = len(bets)
    profits = [b["profit"] for b in bets]
    roi = sum(profits)/n
    clv_pos = sum(1 for b in bets if b["clv"]>0)/n
    avg_clv = sum(b["clv"] for b in bets)/n
    wr = sum(b["win"] for b in bets)/n
    acc_mkt = sum(b["mkt_correct"] for b in bets)/n
    lo,hi,_ = bootstrap_ci(profits)
    # max drawdown
    cum=peak=mdd=0.0
    for p in profits:
        cum+=p
        if cum>peak: peak=cum
        dd=peak-cum
        if dd>mdd: mdd=dd
    return dict(n=n,wr=wr,roi=roi,lo=lo,hi=hi,
                clv_pos=clv_pos,avg_clv=avg_clv,
                acc_mkt=acc_mkt,mdd=mdd,profits=profits)

def prow(label, s, extra=""):
    if not s: return f"  {label:38}  n=0"
    lo = f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
    hi = f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
    mark = " ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
    return (f"  {label:38}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
            f"[{lo},{hi}]  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+7.4f}"
            f"  {s['acc_mkt']:>7.3f}{mark}  {extra}")

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
        res=1 if win==t1 else 0
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
        h2h_delta=adj-ep   # how much H2H moved the probability

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
                        mp_open=mp1,
                        mp_close=mp1c if mp1c else mp1,
                        edge_elo=ep-mp1,      # pure Elo edge
                        edge_adj=ea,           # H2H-adjusted edge
                        bet_odds=bo, result=res,
                    ))

        k=_tier_k(ln); w=_time_weight(bat,now_dt,365); kef=k*w
        ex=elo_prob(e1,e2)
        elo[t1]=e1+kef*(res-ex); elo[t2]=e2+kef*((1-res)-(1-ex))
        games[t1]+=1; games[t2]+=1
        try: ts=datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except: ts=now_dt.timestamp()
        h2h[key].append((ts,win==key[0]))

    return rows

def make_bet(b, require_edge_pos=True):
    if require_edge_pos and b["edge_adj"]<=0: return None
    result=b["result"]; win=(result==1)
    profit=(b["bet_odds"]-1) if win else -1.0
    clv=b["mp_close"]-b["mp_open"]
    mfav=b["mp_open"]>0.5
    mkc=int((mfav and result==1) or (not mfav and result==0))
    return dict(win=int(win),profit=profit,clv=clv,mkt_correct=mkc,
                bet_odds=b["bet_odds"],mp_open=b["mp_open"],
                adj_prob=b["adj_prob"],elo_prob=b["elo_prob"])


# ── Task 1: Elo Strength Analysis ────────────────────────────────────────────

def task1_elo(rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 1: ELO STRENGTH ANALYSIS  (edge_adj > 0, odds < 2.0)")
    print("  Вопрос: растёт ли edge монотонно вместе с elo_diff?")
    print("="*100)

    BANDS = [(75,125),(125,175),(175,250),(250,9999),(0,75)]
    LABELS = ["elo_diff 75-125","elo_diff 125-175","elo_diff 175-250",
              "elo_diff 250+","elo_diff 0-75 (control)"]

    print(f"\n  {'Label':28}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI 95%':>18}  "
          f"{'CLV+':>6}  {'avgCLV':>7}  {'acc_mkt':>8}  {'acc_elo':>8}")
    print("  "+"-"*105)

    for (lo,hi), label in zip(BANDS, LABELS):
        bets=[]
        for b in rows:
            if b["edge_adj"]>0 and lo<=b["elo_diff"]<hi and b["bet_odds"]<2.0:
                bt=make_bet(b)
                if bt:
                    bt["elo_correct"]=int((b["elo_prob"]>0.5 and b["result"]==1) or
                                          (b["elo_prob"]<=0.5 and b["result"]==0))
                    bets.append(bt)
        s=stats(bets)
        if not s:
            print(f"  {label:28}  n=0"); continue
        acc_elo=sum(b["elo_correct"] for b in bets)/len(bets)
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        sep = "  │" if label=="elo_diff 0-75 (control)" else "   "
        print(f"{sep} {label:28}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"[{lo_c},{hi_c}]  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+7.4f}  "
              f"{s['acc_mkt']:>8.3f}  {acc_elo:>8.3f}{mark}")

    # Pure Elo edge vs H2H-adj edge comparison
    print(f"\n  ── Pure Elo edge vs H2H-adjusted edge (elo_diff>=75, odds<2.0) ──")
    print(f"  {'Source':25}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  {'CLV+':>6}")
    print("  "+"-"*75)
    for label, edge_key in [("Pure Elo edge (no H2H)","edge_elo"),
                              ("H2H-adjusted edge","edge_adj")]:
        bets=[]
        for b in rows:
            if b[edge_key]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0:
                bt=make_bet(b, require_edge_pos=False)
                if bt: bets.append(bt)
        s=stats(bets)
        if not s: print(f"  {label:25}  n=0"); continue
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        print(f"  {label:25}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"[{lo_c},{hi_c}]  {s['clv_pos']:>6.3f}{mark}")


# ── Task 2: H2H Contribution ──────────────────────────────────────────────────

def task2_h2h(rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 2: H2H CONTRIBUTION  (edge_adj > 0, elo_diff >= 75, odds < 2.0)")
    print("  Вопрос: действительно ли H2H добавляет информацию?")
    print("="*100)

    BANDS = [(0,1),(1,3),(3,5),(5,999)]
    LABELS = ["H2H = 0","H2H = 1-2","H2H = 3-4","H2H >= 5"]

    print(f"\n  {'Label':15}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  "
          f"{'CLV+':>6}  {'avgCLV':>7}  {'acc_adj':>8}  {'acc_mkt':>8}")
    print("  "+"-"*90)

    for (lo,hi),label in zip(BANDS,LABELS):
        bets=[]
        adj_correct_list=[]
        for b in rows:
            if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0 \
               and lo<=b["h2h_n"]<hi:
                bt=make_bet(b)
                if bt:
                    adj_c=int((b["adj_prob"]>0.5 and b["result"]==1) or
                               (b["adj_prob"]<=0.5 and b["result"]==0))
                    adj_correct_list.append(adj_c)
                    bets.append(bt)
        s=stats(bets)
        if not s: print(f"  {label:15}  n=0"); continue
        acc_adj=sum(adj_correct_list)/len(adj_correct_list) if adj_correct_list else 0
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        print(f"  {label:15}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"[{lo_c},{hi_c}]  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+7.4f}  "
              f"{acc_adj:>8.3f}  {s['acc_mkt']:>8.3f}{mark}")

    # H2H delta analysis: does H2H adjustment direction matter?
    print(f"\n  ── H2H delta: как H2H сдвигает вероятность? ──")
    print(f"  (h2h_delta = adj_prob - elo_prob)")
    print(f"\n  {'Direction':25}  {'n':>4}  {'WR':>6}  {'ROI':>7}  {'CI':>18}  {'CLV+':>6}")
    print("  "+"-"*75)

    for label, filt in [
        ("H2H повышает (>+2%)",  lambda b: b["h2h_delta"]>0.02),
        ("H2H нейтрален (-2%..+2%)", lambda b: abs(b["h2h_delta"])<=0.02),
        ("H2H снижает (<-2%)",   lambda b: b["h2h_delta"]<-0.02),
    ]:
        bets=[]
        for b in rows:
            if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0 and filt(b):
                bt=make_bet(b)
                if bt: bets.append(bt)
        s=stats(bets)
        if not s: print(f"  {label:25}  n=0"); continue
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        print(f"  {label:25}  {s['n']:>4}  {s['wr']:>6.3f}  {s['roi']:>+7.3f}  "
              f"[{lo_c},{hi_c}]  {s['clv_pos']:>6.3f}{mark}")


# ── Task 3: Edge Size Analysis ────────────────────────────────────────────────

def task3_edge_size(rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 3: EDGE SIZE ANALYSIS  (edge_adj > 0, elo_diff >= 75, odds < 2.0)")
    print("  Вопрос: Q4 лучше Q1? Если нет — величина edge бесполезна.")
    print("="*100)

    seg=[(b["edge_adj"],b) for b in rows
         if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0]
    seg.sort(key=lambda x:x[0])
    n=len(seg); q=n//4

    print(f"\n  Всего ставок: n={n}")
    print(f"\n  {'Квартиль':22}  {'edge range':20}  {'n':>4}  {'WR':>6}  {'ROI':>7}  "
          f"{'CI':>18}  {'CLV+':>6}  {'avgCLV':>7}")
    print("  "+"-"*100)

    quartiles=[]
    for i,label in enumerate(["Q1 (edge 0..25%)","Q2 (25..50%)","Q3 (50..75%)","Q4 (75..100%)"]):
        chunk=seg[i*q:(i+1)*q] if i<3 else seg[3*q:]
        if not chunk: continue
        edges=[e for e,_ in chunk]
        bets=[make_bet(b) for _,b in chunk]; bets=[b for b in bets if b]
        s=stats(bets)
        if not s: continue
        e_lo,e_hi=min(edges),max(edges)
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        print(f"  {label:22}  {e_lo:+.4f}..{e_hi:+.4f}  {s['n']:>4}  "
              f"{s['wr']:>6.3f}  {s['roi']:>+7.3f}  [{lo_c},{hi_c}]  "
              f"{s['clv_pos']:>6.3f}  {s['avg_clv']:>+7.4f}{mark}")
        quartiles.append((label,s))

    # Summary
    if len(quartiles)>=2:
        q1_roi = quartiles[0][1]["roi"]
        q4_roi = quartiles[-1][1]["roi"]
        q1_clv = quartiles[0][1]["clv_pos"]
        q4_clv = quartiles[-1][1]["clv_pos"]
        print(f"\n  Q4 ROI = {q4_roi:+.3f}  vs  Q1 ROI = {q1_roi:+.3f}  "
              f"→  {'Q4 ЛУЧШЕ ✓' if q4_roi>q1_roi else 'Q4 НЕ ЛУЧШЕ ✗'}")
        print(f"  Q4 CLV+ = {q4_clv:.3f}  vs  Q1 CLV+ = {q1_clv:.3f}  "
              f"→  {'Q4 лучше по CLV+' if q4_clv>q1_clv else 'Q4 не лучше по CLV+'}")

    # Also show: edge_elo quartiles (pure Elo)
    print(f"\n  ── Pure Elo edge quartiles (edge_elo, без H2H) ──")
    seg2=[(b["edge_elo"],b) for b in rows
          if b["edge_elo"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0]
    seg2.sort(key=lambda x:x[0])
    n2=len(seg2); q2=n2//4
    print(f"  n={n2}")
    for i,label in enumerate(["Q1","Q2","Q3","Q4"]):
        chunk=seg2[i*q2:(i+1)*q2] if i<3 else seg2[3*q2:]
        if not chunk: continue
        edges=[e for e,_ in chunk]
        bets=[make_bet(b,require_edge_pos=False) for _,b in chunk]
        bets=[b for b in bets if b]
        s=stats(bets)
        if not s: continue
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        print(f"  {label}  edge_elo {min(edges):+.4f}..{max(edges):+.4f}  "
              f"n={s['n']}  ROI={s['roi']:+.3f}  CLV+={s['clv_pos']:.3f}  "
              f"avgCLV={s['avg_clv']:+.4f}{mark}")


# ── Task 4: Market Probability Analysis ──────────────────────────────────────

def task4_market_prob(rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 4: MARKET PROBABILITY ANALYSIS  (edge_adj > 0, elo_diff >= 75, odds < 2.0)")
    print("  Вопрос: сигнал живёт в конкретной зоне или универсален?")
    print("="*100)

    BANDS = [(0.50,0.60),(0.60,0.65),(0.65,0.70),(0.70,0.80),(0.80,1.01)]
    LABELS = ["50-60%","60-65%","65-70%","70-80%","80%+"]

    print(f"\n  market_prob    n    WR     ROI      CI 95%              CLV+   avgCLV   "
          f"adj_prob   elo_prob   mkt_prob")
    print("  "+"-"*105)

    for (lo,hi),label in zip(BANDS,LABELS):
        bets=[]
        adj_list,elo_list,mkt_list=[],[],[]
        for b in rows:
            if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0 \
               and lo<=b["mp_open"]<hi:
                bt=make_bet(b)
                if bt:
                    bets.append(bt)
                    adj_list.append(b["adj_prob"])
                    elo_list.append(b["elo_prob"])
                    mkt_list.append(b["mp_open"])
        s=stats(bets)
        if not s: print(f"  {label:14} n=0"); continue
        lo_c=f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_c=f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        mark=" ✓" if (s['lo']==s['lo'] and s['lo']>0) else "  "
        avg_adj=sum(adj_list)/len(adj_list)
        avg_elo=sum(elo_list)/len(elo_list)
        avg_mkt=sum(mkt_list)/len(mkt_list)
        print(f"  {label:14} {s['n']:>4}  {s['wr']:>5.3f}  {s['roi']:>+7.3f}  "
              f"[{lo_c},{hi_c}]  {s['clv_pos']:>6.3f}  {s['avg_clv']:>+6.4f}  "
              f"{avg_adj:>9.4f}  {avg_elo:>9.4f}  {avg_mkt:>9.4f}{mark}")

    # Market mispricing: where does market error (WR - market_prob) concentrate?
    print(f"\n  ── Market mispricing: actual WR - market_prob ──")
    print(f"  (positive = market underpriced the team we bet on)")
    print(f"\n  market_prob    n    WR     mkt_prob   WR - mkt_prob   adj_prob   WR - adj_prob")
    print("  "+"-"*90)
    for (lo,hi),label in zip(BANDS,LABELS):
        bets_raw=[(b["result"],b["mp_open"],b["adj_prob"]) for b in rows
                  if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0
                  and lo<=b["mp_open"]<hi]
        if not bets_raw: continue
        n=len(bets_raw)
        wr=sum(r for r,_,_ in bets_raw)/n
        avg_mkt=sum(m for _,m,_ in bets_raw)/n
        avg_adj=sum(a for _,_,a in bets_raw)/n
        mkt_err=wr-avg_mkt
        adj_err=wr-avg_adj
        print(f"  {label:14} {n:>4}  {wr:.3f}  {avg_mkt:.4f}      {mkt_err:>+14.4f}  "
              f"{avg_adj:.4f}      {adj_err:>+13.4f}")


# ── Task 5: Forward Simulation ────────────────────────────────────────────────

def task5_forward_sim(rows):
    print("\n"+"="*100)
    print("ЗАДАЧА 5: FORWARD SIMULATION — 3 правила × purged test")
    print("  Вопрос: какое правило максимизирует P(CLV+>50%) на следующих 20 ставках?")
    print("="*100)

    RULES = [
        ("Rule A: elo>=75 & odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0),
        ("Rule B: elo>=125 & odds<2.0",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=125 and b["bet_odds"]<2.0),
        ("Rule C: elo>=75 & odds<2.0 & mkt 60-70%",
         lambda b: b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0
                   and 0.60<=b["mp_open"]<0.70),
    ]

    for rule_label, filt in RULES:
        seg=[]
        for b in rows:
            if filt(b):
                bt=make_bet(b)
                if bt: bt["begin_at"]=b["begin_at"]; seg.append(bt)
        seg.sort(key=lambda b:b["begin_at"])
        n=len(seg)
        if n<5:
            print(f"\n  {rule_label}: n={n} (недостаточно данных)"); continue

        # Full history
        s_all=stats(seg)

        # Purged time split: 60/20/20
        i60=int(n*0.60); i80=int(n*0.80)
        train=seg[:i60]; gap=seg[i60:i80]; test=seg[i80:]
        s_tr=stats(train); s_te=stats(test)

        # Bootstrap P(ROI>0) for test
        if s_te and len(s_te["profits"])>=3:
            _,_,test_means=bootstrap_ci(s_te["profits"],n=N_BOOT)
            p_pos_test=sum(1 for m in test_means if m>0)/N_BOOT
            # P(CLV+>50%) — approximate via bootstrap of CLV+ labels
            clv_labels=[1 if b["clv"]>0 else 0 for b in test]
            rng=random.Random(SEED)
            clv_means=sorted(sum(rng.choices(clv_labels,k=len(clv_labels)))/len(clv_labels)
                             for _ in range(N_BOOT))
            p_clvpos=sum(1 for m in clv_means if m>0.5)/N_BOOT
        else:
            p_pos_test=float("nan"); p_clvpos=float("nan")

        lo_a=f"{s_all['lo']:+.3f}" if s_all['lo']==s_all['lo'] else " nan"
        hi_a=f"{s_all['hi']:+.3f}" if s_all['hi']==s_all['hi'] else " nan"

        print(f"\n  ── {rule_label} ──")
        print(f"  Full:   n={s_all['n']}  ROI={s_all['roi']:+.4f}  CI=[{lo_a},{hi_a}]  "
              f"CLV+={s_all['clv_pos']:.3f}  avgCLV={s_all['avg_clv']:+.4f}")

        if s_tr:
            lo_t=f"{s_tr['lo']:+.3f}" if s_tr['lo']==s_tr['lo'] else " nan"
            hi_t=f"{s_tr['hi']:+.3f}" if s_tr['hi']==s_tr['hi'] else " nan"
            print(f"  Train:  n={s_tr['n']}  ROI={s_tr['roi']:+.4f}  CI=[{lo_t},{hi_t}]  "
                  f"CLV+={s_tr['clv_pos']:.3f}")
        if s_te:
            lo_te=f"{s_te['lo']:+.3f}" if s_te['lo']==s_te['lo'] else " nan"
            hi_te=f"{s_te['hi']:+.3f}" if s_te['hi']==s_te['hi'] else " nan"
            mark=" ✓" if (s_te['lo']==s_te['lo'] and s_te['lo']>0) else "  "
            print(f"  Test:   n={s_te['n']}  ROI={s_te['roi']:+.4f}  CI=[{lo_te},{hi_te}]  "
                  f"CLV+={s_te['clv_pos']:.3f}{mark}")
            p_str=f"{p_pos_test:.1%}" if p_pos_test==p_pos_test else "nan"
            c_str=f"{p_clvpos:.1%}" if p_clvpos==p_clvpos else "nan"
            print(f"  P(test ROI>0)={p_str}  P(test CLV+>50%)={c_str}")

    # Comparative summary
    print(f"\n  {'='*70}")
    print(f"  СРАВНЕНИЕ: что лучше для следующих 20 ставок?")
    print(f"  {'='*70}")
    print(f"\n  {'Rule':40}  {'n_full':>7}  {'CLV+_full':>10}  {'Test ROI':>9}  {'Test CLV+':>10}  {'P(CLV+>50%)':>12}")
    print("  "+"-"*100)

    for rule_label, filt in RULES:
        seg=[]; bets_full=[]
        for b in rows:
            if filt(b):
                bt=make_bet(b)
                if bt: bt["begin_at"]=b["begin_at"]; seg.append(bt)
        seg.sort(key=lambda b:b["begin_at"])
        n=len(seg)
        if n<5: print(f"  {rule_label:40}  n<5"); continue
        s_all=stats(seg)
        i80=int(n*0.80)
        test=seg[i80:]
        s_te=stats(test)

        if s_te and len(s_te["profits"])>=3:
            clv_labels=[1 if b["clv"]>0 else 0 for b in test]
            rng=random.Random(SEED)
            clv_means=sorted(sum(rng.choices(clv_labels,k=len(clv_labels)))/len(clv_labels)
                             for _ in range(N_BOOT))
            p_clvpos=sum(1 for m in clv_means if m>0.5)/N_BOOT
        else:
            p_clvpos=float("nan")

        te_roi=f"{s_te['roi']:+.3f}" if s_te else "  n/a"
        te_clv=f"{s_te['clv_pos']:.3f}" if s_te else "  n/a"
        p_str=f"{p_clvpos:.1%}" if p_clvpos==p_clvpos else "  nan"
        mark=" ✓" if (s_te and s_te['lo']==s_te['lo'] and s_te['lo']>0) else "  "
        print(f"  {rule_label:40}  {n:>7}  {s_all['clv_pos']:>10.3f}  "
              f"{te_roi:>9}  {te_clv:>10}  {p_str:>12}{mark}")

    print(f"\n  Рекомендация:")
    print(f"  Правило с наибольшим P(CLV+>50%) в test — кандидат для paper daemon.")
    print(f"  Учитывать также n: маленький n = широкий CI = больше variance на 20 ставках.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["1","2","3","4","5"])
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*100}")
    print("DECOMPOSITION ANALYSIS — dota_trader_v2")
    print(f"{'='*100}")
    print("Загружаем данные...", flush=True)
    all_rows = build_all_bets(conn)
    conn.close()
    n_seg = sum(1 for b in all_rows if b["edge_adj"]>0 and b["elo_diff"]>=75 and b["bet_odds"]<2.0)
    print(f"  Матчей: {len(all_rows)}  |  Базовый сегмент: {n_seg}\n")

    run = args.only
    if run is None or run=="1": task1_elo(all_rows)
    if run is None or run=="2": task2_h2h(all_rows)
    if run is None or run=="3": task3_edge_size(all_rows)
    if run is None or run=="4": task4_market_prob(all_rows)
    if run is None or run=="5": task5_forward_sim(all_rows)

    print(f"\n{'='*100}")
    print("Готово.")

if __name__ == "__main__":
    main()
