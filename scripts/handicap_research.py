#!/usr/bin/env python3
"""
Handicap Research Phase 1 — Dota 2 Map Handicap ±1.5
Исследования 1-5: калибровка рынка, Elo vs Market, CLV тест, вердикт.

Usage:
    cd dota_trader_v2
    python3 scripts/handicap_research.py
"""
from __future__ import annotations
import json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from math import exp, log

ROOT = Path(__file__).parent.parent
DB   = ROOT / "storage" / "betsapi_harvest.db"

# ── Elo constants ─────────────────────────────────────────────────────────────
BASE_ELO  = 1500.0
BASE_K    = 32.0
TIER_K = {
    "the international": 1.5, " ti ": 1.5, "major": 1.5, "dreamleague": 1.5,
    "esl one": 1.5, "blast": 1.5,
    "dpc": 1.2, "esl": 1.2, "regional league": 1.2, "division": 1.2,
    "qualifier": 0.7, "cup": 0.7, "series": 0.7,
}

def tier_k(league: str) -> float:
    name = (league or "").lower()
    for kw, mult in TIER_K.items():
        if kw in name:
            return BASE_K * mult
    return BASE_K

def elo_expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

def elo_update(ra: float, rb: float, score_a: float, k: float):
    ea = elo_expected(ra, rb)
    return ra + k * (score_a - ea), rb + k * ((1-score_a) - (1-ea))

def logistic(x: float) -> float:
    return 1.0 / (1.0 + exp(-x))

def safe_log(p: float) -> float:
    p = max(1e-7, min(1-1e-7, p))
    return log(p)

def novig2(o1: float, o2: float):
    """Remove vig from 2-way market."""
    if o1 <= 1 or o2 <= 1:
        return None, None
    i1, i2 = 1/o1, 1/o2
    total = i1 + i2
    return i1/total, i2/total


# ── Step 1: Build Elo from historical Dota2 results ──────────────────────────
def build_elo(conn) -> dict:
    """
    Process all Dota2 ended BO3 matches chronologically.
    Returns: dict[event_id] -> {'elo_home': float, 'elo_away': float, 'elo_diff': float}
    """
    rows = conn.execute("""
        SELECT event_id, home_team, away_team, start_time,
               json_extract(raw_json,'$.ss') as ss,
               json_extract(raw_json,'$.league.name') as league
        FROM raw_events
        WHERE sport_tag='dota2' AND status='ended'
          AND home_team IS NOT NULL AND away_team IS NOT NULL
          AND json_extract(raw_json,'$.ss') IN ('2-0','0-2','2-1','1-2')
        ORDER BY start_time ASC
    """).fetchall()

    ratings: dict[str, float] = defaultdict(lambda: BASE_ELO)
    elo_snapshot: dict[str, dict] = {}  # event_id -> pre-match elos

    for row in rows:
        eid   = row['event_id']
        home  = row['home_team']
        away  = row['away_team']
        ss    = row['ss']
        lg    = row['league'] or ""
        k     = tier_k(lg)

        ra = ratings[home]
        rb = ratings[away]
        # Store PRE-match snapshot
        elo_snapshot[eid] = {
            'elo_home': ra,
            'elo_away': rb,
            'elo_diff': abs(ra - rb),
        }
        # Determine winner
        if ss in ('2-0',):
            score_a = 1.0   # home wins
        elif ss in ('0-2',):
            score_a = 0.0   # away wins
        elif ss == '2-1':
            score_a = 0.75  # home wins but contest
        else:  # 1-2
            score_a = 0.25
        # Update ratings
        new_ra, new_rb = elo_update(ra, rb, score_a, k)
        ratings[home] = new_ra
        ratings[away] = new_rb

    print(f"  Elo built: {len(elo_snapshot)} events, {len(ratings)} unique teams")
    return elo_snapshot


# ── Step 2: Build Handicap Dataset ───────────────────────────────────────────
def build_handicap_dataset(conn, elo_snapshot: dict) -> list[dict]:
    BO3 = {'2-0', '0-2', '2-1', '1-2'}
    ss_lookup = {}
    for row in conn.execute("""
        SELECT event_id, json_extract(raw_json,'$.ss') as ss
        FROM raw_events WHERE sport_tag='dota2' AND status='ended'
    """):
        if row['ss'] in BO3:
            ss_lookup[row['event_id']] = row['ss']

    records = []
    seen = set()
    for row in conn.execute("SELECT event_id, raw_json FROM odds_summary WHERE bookmaker='PinnacleSports'"):
        eid = row['event_id']
        if eid in seen: continue
        ss = ss_lookup.get(eid)
        if ss is None: continue
        elo = elo_snapshot.get(eid)
        try:
            raw = json.loads(row['raw_json'])
            for bm_name, bm_data in raw.get('results', {}).items():
                od   = bm_data.get('odds') or {}
                st   = od.get('start') or {}
                en   = od.get('end') or {}
                s2   = st.get('151_2')
                e2   = en.get('151_2')
                if not isinstance(s2, dict) or not isinstance(e2, dict): continue
                hcp  = s2.get('handicap')
                if str(hcp) not in ('-1.5', '+1.5', '1.5'): continue
                if e2.get('ss') is not None: continue  # live close, skip
                # Also grab MW for comparison
                s1   = st.get('151_1')
                e1   = en.get('151_1')

                def safe_f(v):
                    try: return float(v) if v else 0
                    except: return 0

                # Home side odds
                oh = safe_f(s2.get('home_od')); ch = safe_f(e2.get('home_od'))
                oa = safe_f(s2.get('away_od')); ca = safe_f(e2.get('away_od'))
                mw_oh = safe_f((s1 or {}).get('home_od'))
                mw_ah = safe_f((s1 or {}).get('away_od'))
                if oh <= 1 or ch <= 1 or oa <= 1: continue

                # Remove vig: get true probability
                imp_open_h, imp_open_a = novig2(oh, oa)
                imp_close_h, imp_close_a = novig2(ch, ca)
                mw_imp_h, mw_imp_a = novig2(mw_oh, mw_ah) if mw_oh > 1 else (None, None)

                # Who is -1.5?
                if str(hcp) == '-1.5':
                    # home is -1.5 (must sweep)
                    covers         = (ss == '2-0')
                    open_fav_od    = oh
                    close_fav_od   = ch
                    imp_open_fav   = imp_open_h
                    imp_close_fav  = imp_close_h
                    elo_fav        = (elo['elo_home'] if elo else None)
                    elo_dog        = (elo['elo_away'] if elo else None)
                    mw_imp_fav     = mw_imp_h
                else:  # +1.5 home means AWAY is -1.5
                    covers         = (ss == '0-2')
                    open_fav_od    = oa
                    close_fav_od   = ca
                    imp_open_fav   = imp_open_a
                    imp_close_fav  = imp_close_a
                    elo_fav        = (elo['elo_away'] if elo else None)
                    elo_dog        = (elo['elo_home'] if elo else None)
                    mw_imp_fav     = mw_imp_a

                if imp_open_fav is None or imp_close_fav is None: continue

                elo_diff = abs(elo_fav - elo_dog) if (elo_fav and elo_dog) else None
                elo_prob = elo_expected(elo_fav, elo_dog) if (elo_fav and elo_dog) else None

                records.append({
                    'event_id':        eid,
                    'ss':              ss,
                    'hcp':             str(hcp),
                    'covers':          covers,
                    'open_fav_od':     open_fav_od,
                    'close_fav_od':    close_fav_od,
                    'imp_open_fav':    imp_open_fav,
                    'imp_close_fav':   imp_close_fav,
                    'line_move':       imp_close_fav - imp_open_fav,  # + = market moved toward fav
                    'elo_diff':        elo_diff,
                    'elo_prob':        elo_prob,
                    'mw_imp_open_fav': mw_imp_fav,
                })
                seen.add(eid)
                break
        except: pass

    return records


# ── Helpers ───────────────────────────────────────────────────────────────────
def bucket_stats(records, key_fn, label_fn, buckets):
    """Generic bucket analysis."""
    bucketed = defaultdict(list)
    for r in records:
        v = key_fn(r)
        if v is None: continue
        for lo, hi in buckets:
            if lo <= v < hi:
                bucketed[(lo, hi)].append(r)
                break
    rows = []
    for (lo, hi) in buckets:
        recs = bucketed[(lo, hi)]
        if not recs:
            rows.append({'label': label_fn(lo,hi), 'n':0,'sweep_rate':None,'avg_key':None})
            continue
        sweep_rate = sum(1 for r in recs if r['covers']) / len(recs)
        avg_key    = sum(key_fn(r) for r in recs if key_fn(r)) / len(recs)
        rows.append({'label': label_fn(lo,hi), 'n': len(recs),
                     'sweep_rate': sweep_rate, 'avg_key': avg_key})
    return rows

def brier(records, prob_fn):
    vals = [(prob_fn(r), int(r['covers'])) for r in records if prob_fn(r) is not None]
    if not vals: return None
    return sum((p-a)**2 for p,a in vals) / len(vals)

def logloss(records, prob_fn):
    vals = [(prob_fn(r), int(r['covers'])) for r in records if prob_fn(r) is not None]
    if not vals: return None
    return -sum(a*safe_log(p) + (1-a)*safe_log(1-p) for p,a in vals) / len(vals)

def accuracy(records, prob_fn):
    vals = [(prob_fn(r), int(r['covers'])) for r in records if prob_fn(r) is not None]
    if not vals: return None
    return sum(1 for p,a in vals if (p>=0.5)==bool(a)) / len(vals)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*70)
    print("HANDICAP RESEARCH PHASE 1 — Dota 2 Map Handicap ±1.5")
    print("="*70)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("\n[1/2] Building Elo ratings from 12,201 matches...")
    elo_snapshot = build_elo(conn)

    print("[2/2] Building handicap dataset...")
    ds = build_handicap_dataset(conn, elo_snapshot)
    conn.close()

    n_total = len(ds)
    n_covers = sum(1 for r in ds if r['covers'])
    n_elo    = sum(1 for r in ds if r['elo_diff'] is not None)

    print(f"\n  Dataset: {n_total} events (Pinnacle, pre-match, BO3)")
    print(f"  Covers (sweep): {n_covers} ({n_covers/n_total*100:.1f}%)")
    print(f"  With Elo:       {n_elo}")
    print(f"  Avg open implied prob fav -1.5: {sum(r['imp_open_fav'] for r in ds)/n_total:.3f}")
    print(f"  Avg close implied prob fav -1.5: {sum(r['imp_close_fav'] for r in ds)/n_total:.3f}")

    sep = "─" * 70

    # ════════════════════════════════════════════════════════════════════════
    # RESEARCH 1: Market Calibration
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("RESEARCH 1 — Market Calibration: открытая линия Pinnacle vs реальный sweep")
    print(sep)
    print("Вопрос: Рынок завышает или занижает вероятность свипов?\n")

    prob_buckets = [(0.40,0.45),(0.45,0.50),(0.50,0.55),(0.55,0.60),
                    (0.60,0.65),(0.65,0.70),(0.70,0.75),(0.75,0.80),
                    (0.80,0.85),(0.85,0.92)]
    key_fn = lambda r: r['imp_open_fav']
    label_fn = lambda lo,hi: f"{lo*100:.0f}-{hi*100:.0f}%"

    print(f"  {'Bucket':<12} {'n':>6} {'mkt_prob':>10} {'sweep_rate':>11} {'cal_error':>11} {'direction'}")
    print(f"  {'─'*12} {'─'*6} {'─'*10} {'─'*11} {'─'*11} {'─'*10}")

    total_cal_err = []
    for lo, hi in prob_buckets:
        recs = [r for r in ds if lo <= r['imp_open_fav'] < hi]
        if len(recs) < 5:
            print(f"  {label_fn(lo,hi):<12} {'<5':>6}")
            continue
        sweep = sum(1 for r in recs if r['covers']) / len(recs)
        mkt_p = sum(r['imp_open_fav'] for r in recs) / len(recs)
        err   = sweep - mkt_p
        total_cal_err.append(abs(err))
        direction = "OVER" if err > 0.03 else ("UNDER" if err < -0.03 else "≈ fair")
        print(f"  {label_fn(lo,hi):<12} {len(recs):>6} {mkt_p:>10.3f} {sweep:>11.3f} {err:>+11.3f} {direction}")

    overall_sweep = n_covers / n_total
    overall_mkt   = sum(r['imp_open_fav'] for r in ds) / n_total
    print(f"\n  Overall: mkt_avg={overall_mkt:.3f}  actual_sweep={overall_sweep:.3f}  bias={overall_sweep-overall_mkt:+.3f}")
    if overall_sweep > overall_mkt + 0.02:
        print("  → РЫНОК СИСТЕМАТИЧЕСКИ ЗАНИЖАЕТ ВЕРОЯТНОСТЬ СВИПА (underestimates favorites)")
    elif overall_sweep < overall_mkt - 0.02:
        print("  → РЫНОК СИСТЕМАТИЧЕСКИ ЗАВЫШАЕТ ВЕРОЯТНОСТЬ СВИПА (overestimates favorites)")
    else:
        print("  → Рынок в целом хорошо откалиброван")

    # ════════════════════════════════════════════════════════════════════════
    # RESEARCH 2: Elo diff vs sweep rate
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("RESEARCH 2 — Elo diff vs Sweep Rate")
    print(sep)
    print("Вопрос: Есть ли зоны где Elo знает о свипах больше рынка?\n")

    elo_buckets = [(0,50),(50,100),(100,150),(150,200),(200,300),(300,700)]
    label_elo   = lambda lo,hi: f"{lo}-{hi}"
    ds_elo = [r for r in ds if r['elo_diff'] is not None]

    print(f"  {'Elo_diff':>10} {'n':>6} {'mkt_prob':>10} {'elo_prob':>10} {'sweep_rate':>11} {'mkt_err':>9} {'elo_err':>9}")
    print(f"  {'─'*10} {'─'*6} {'─'*10} {'─'*10} {'─'*11} {'─'*9} {'─'*9}")

    for lo, hi in elo_buckets:
        recs = [r for r in ds_elo if lo <= r['elo_diff'] < hi]
        if len(recs) < 10: continue
        sweep  = sum(1 for r in recs if r['covers']) / len(recs)
        mkt_p  = sum(r['imp_open_fav'] for r in recs) / len(recs)
        elo_p  = sum(r['elo_prob'] for r in recs) / len(recs)
        mkt_e  = sweep - mkt_p
        elo_e  = sweep - elo_p
        print(f"  {label_elo(lo,hi):>10} {len(recs):>6} {mkt_p:>10.3f} {elo_p:>10.3f} {sweep:>11.3f} {mkt_e:>+9.3f} {elo_e:>+9.3f}")

    # ════════════════════════════════════════════════════════════════════════
    # RESEARCH 3: Elo vs Market prediction
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("RESEARCH 3 — Elo vs Market: кто точнее предсказывает свип?")
    print(sep)

    ds_m = [r for r in ds if r['imp_open_fav'] is not None]
    ds_e = [r for r in ds if r['elo_prob'] is not None]
    ds_b = [r for r in ds if r['imp_open_fav'] is not None and r['elo_prob'] is not None]

    # Simple logistic blend
    # Find best alpha: pred = alpha*mkt + (1-alpha)*elo
    best_alpha = 0.5
    best_ll    = float('inf')
    for alpha in [i/10 for i in range(11)]:
        ll = -sum(
            int(r['covers'])*safe_log(alpha*r['imp_open_fav'] + (1-alpha)*r['elo_prob']) +
            (1-int(r['covers']))*safe_log(1 - (alpha*r['imp_open_fav'] + (1-alpha)*r['elo_prob']))
            for r in ds_b
        ) / len(ds_b)
        if ll < best_ll:
            best_ll, best_alpha = ll, alpha

    blend_prob = lambda r: best_alpha*r['imp_open_fav'] + (1-best_alpha)*r['elo_prob']

    print(f"\n  {'Model':<22} {'n':>6} {'Accuracy':>10} {'Brier':>9} {'LogLoss':>10}")
    print(f"  {'─'*22} {'─'*6} {'─'*10} {'─'*9} {'─'*10}")

    print(f"  {'Market (open Pinnacle)':<22} {len(ds_m):>6} "
          f"{accuracy(ds_m, lambda r: r['imp_open_fav'])*100:>9.1f}% "
          f"{brier(ds_m, lambda r: r['imp_open_fav']):>9.4f} "
          f"{logloss(ds_m, lambda r: r['imp_open_fav']):>10.4f}")

    print(f"  {'Elo only':<22} {len(ds_e):>6} "
          f"{accuracy(ds_e, lambda r: r['elo_prob'])*100:>9.1f}% "
          f"{brier(ds_e, lambda r: r['elo_prob']):>9.4f} "
          f"{logloss(ds_e, lambda r: r['elo_prob']):>10.4f}")

    print(f"  {'Market + Elo blend':<22} {len(ds_b):>6} "
          f"{accuracy(ds_b, blend_prob)*100:>9.1f}% "
          f"{brier(ds_b, blend_prob):>9.4f} "
          f"{logloss(ds_b, blend_prob):>10.4f}")

    # Naive baseline: always predict overall sweep rate
    naive_p = n_covers / n_total
    print(f"  {'Baseline (naive avg)':<22} {n_total:>6} "
          f"{accuracy(ds, lambda r: naive_p)*100:>9.1f}% "
          f"{brier(ds, lambda r: naive_p):>9.4f} "
          f"{logloss(ds, lambda r: naive_p):>10.4f}")

    print(f"\n  Best blend: alpha={best_alpha:.1f}*mkt + {1-best_alpha:.1f}*elo")

    # ════════════════════════════════════════════════════════════════════════
    # RESEARCH 4: Line Movement → CLV signal
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("RESEARCH 4 — Line Movement: несёт ли движение информацию о свипе?")
    print(sep)
    print("Вопрос: Когда рынок движется к/от фаворита -1.5 — прав ли он?\n")

    THRESH = 0.02  # 2% implied prob movement = meaningful move
    moved_toward  = [r for r in ds if r['line_move'] >  THRESH]  # mkt more confident
    moved_against = [r for r in ds if r['line_move'] < -THRESH]  # mkt less confident
    no_move       = [r for r in ds if abs(r['line_move']) <= THRESH]

    def group_stats(recs, label):
        if not recs:
            print(f"  {label}: 0 records")
            return
        n = len(recs)
        sw = sum(1 for r in recs if r['covers']) / n
        avg_open  = sum(r['imp_open_fav']  for r in recs) / n
        avg_close = sum(r['imp_close_fav'] for r in recs) / n
        avg_move  = sum(r['line_move'] for r in recs) / n
        clv_positive = sum(1 for r in recs if r['imp_open_fav'] > r['imp_close_fav']) / n
        print(f"  {label}")
        print(f"    n={n}  sweep_rate={sw:.3f}  avg_open={avg_open:.3f}  avg_close={avg_close:.3f}  avg_move={avg_move:+.3f}")
        print(f"    % where open > close (CLV positive): {clv_positive*100:.1f}%")
        print(f"    → Market was {'RIGHT' if (avg_move>0) == (sw>avg_open) else 'WRONG'} to move this way")

    group_stats(moved_toward,  "Line moved TOWARD fav   (mkt more sure of sweep)")
    print()
    group_stats(moved_against, "Line moved AGAINST fav  (mkt less sure of sweep)")
    print()
    group_stats(no_move,       "Line did NOT move significantly")

    # CLV-style test: does open > close predict actual outcome better?
    ds_clv = [r for r in ds if abs(r['line_move']) > 0.001]
    if ds_clv:
        clv_correct = sum(
            1 for r in ds_clv
            if (r['imp_open_fav'] > r['imp_close_fav']) == (not r['covers'])
        ) / len(ds_clv)
        print(f"\n  CLV test: when you had BETTER than closing line odds,")
        print(f"  how often did the market move away (signalling you were wrong)?")
        print(f"  Rate of 'open > close AND doesn't cover': {clv_correct*100:.1f}%")

    # ════════════════════════════════════════════════════════════════════════
    # RESEARCH 5: Verdict
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{sep}")
    print("RESEARCH 5 — ВЕРДИКТ: Handicap vs Match Winner эффективность")
    print(sep)

    # Key signals:
    overall_cal_bias = overall_sweep - overall_mkt
    mkt_brier  = brier(ds_m, lambda r: r['imp_open_fav'])
    elo_brier  = brier(ds_e, lambda r: r['elo_prob'])
    blend_gain = mkt_brier - brier(ds_b, blend_prob) if ds_b else 0

    print(f"\n  СВОДКА СИГНАЛОВ:")
    print(f"    Систематическое смещение рынка:  {overall_cal_bias:+.3f}")
    print(f"    Brier market:                    {mkt_brier:.4f}")
    print(f"    Brier Elo:                       {elo_brier:.4f}")
    print(f"    Blend gain over market:          {blend_gain:+.4f}")
    print(f"    Best blend alpha (mkt weight):   {best_alpha:.1f}")

    # Determine verdict
    conditions = {
        "market bias > 3%": abs(overall_cal_bias) > 0.03,
        "elo improves over market": elo_brier < mkt_brier,
        "blend improves over market": blend_gain > 0.001,
    }

    passed = sum(conditions.values())

    print(f"\n  КРИТЕРИИ:")
    for cond, val in conditions.items():
        print(f"    {'✓' if val else '✗'} {cond}")

    print(f"\n  ВЕРДИКТ: ", end="")
    if passed >= 3:
        verdict = "YES"
        print(f"YES — Handicap рынок МЕНЕЕ эффективен чем Match Winner")
        print(f"""
  Обоснование:
    • Рынок систематически {'занижает' if overall_cal_bias > 0 else 'завышает'} вероятность свипов на {abs(overall_cal_bias)*100:.1f}%
    • Elo даёт улучшение над рынком: Brier {elo_brier:.4f} vs {mkt_brier:.4f}
    • Комбинация Elo + рынок лучше чем рынок один
    • Вывод: рынок не полностью учитывает информацию о доминировании команд
    • Это и есть наш потенциальный edge""")
    elif passed == 2:
        verdict = "MAYBE"
        print(f"MAYBE — Слабые признаки неэффективности")
        print(f"""
  Обоснование:
    • Некоторые зоны выглядят неэффективными, но не все
    • Смещение рынка: {overall_cal_bias*100:+.1f}% {'(значимо)' if abs(overall_cal_bias)>0.02 else '(слабо)'}
    • Elo {'улучшает' if elo_brier < mkt_brier else 'не улучшает'} предсказание
    • Нужно больше данных или уточнение фильтров для однозначного вывода""")
    else:
        verdict = "NO"
        print(f"NO — Handicap рынок эффективен, схожий уровень с Match Winner")
        print(f"""
  Обоснование:
    • Смещение рынка: {overall_cal_bias*100:+.1f}% (незначимо)
    • Elo не даёт существенного улучшения
    • Рынок уже учитывает информацию о доминировании
    • Edge в этом рынке маловероятен без дополнительных признаков""")

    print(f"\n{'='*70}")
    print(f"ИТОГ: {verdict}")
    print(f"  Dataset: {n_total} матчей | Покрытие: Pinnacle pre-match BO3")
    print(f"{'='*70}\n")


if __name__ == '__main__':
    main()
