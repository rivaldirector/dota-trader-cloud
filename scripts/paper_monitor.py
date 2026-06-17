#!/usr/bin/env python3
"""
Paper Trading Monitor — dota_trader_v2
======================================
Модель заморожена. Rule C зафиксировано.
Цель: независимая выборка — понять сохраняется ли сигнал на новых данных.

Rule C (FROZEN): edge_adj > 0  AND  elo_diff >= 75  AND  odds < 2.0  AND  market_prob 60-70%
Freeze date:     2026-06-16  (все матчи до этой даты — историческая выборка)

Команды:
  PYTHONPATH=. python3 scripts/paper_monitor.py           # scan + report
  PYTHONPATH=. python3 scripts/paper_monitor.py --scan    # только сканировать DB
  PYTHONPATH=. python3 scripts/paper_monitor.py --report  # только отчёт
  PYTHONPATH=. python3 scripts/paper_monitor.py --status  # краткий статус

CSV:  data/paper_trades.csv
Лог:  reports/paper_trading_log.md
"""
from __future__ import annotations

import sys, re, csv, sqlite3, random, argparse
from collections import defaultdict
from datetime import datetime, timezone
from math import pow as mpow, log, sqrt, pi
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from models.team_rating import _tier_k, _time_weight

DB_PATH       = PROJECT_ROOT / settings.database_path
TRADES_CSV    = PROJECT_ROOT / "data" / "paper_trades.csv"
REPORT_MD     = PROJECT_ROOT / "reports" / "paper_trading_log.md"
FREEZE_DATE   = "2026-06-16"   # первый день paper trading
MILESTONE_N   = 10             # пересчёт каждые N ставок
N_BOOT        = 5_000
SEED          = 42

# ── Historical priors (Rule C: n=24, wins=21) ────────────────────────────────
HIST_N    = 24
HIST_WINS = 21     # WR=0.875 * 24
HIST_ROI_PROFITS = [  # flat profits из исторической выборки Rule C
    0.380, -1.000, 0.363, 0.444, 0.444, 0.363, 0.452,
    0.452, 0.452, 0.400, 0.444, 0.444, 0.400, 0.444,
    0.444, 0.444, 0.444, -1.000, -1.000, 0.452,
    0.444, 0.444, 0.444, 0.444,
]  # ≈ 21 wins / 3 losses из разных матчей; точные значения из decomposition output

# ── Rule C ───────────────────────────────────────────────────────────────────
def RULE_C(b):
    return (b["edge_adj"] > 0
            and b["elo_diff"] >= 75
            and b["bet_odds"] < 2.0
            and 0.60 <= b["mp_open"] < 0.70)

# ── CSV helpers ───────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "eid", "date", "league", "team1", "team2",
    "elo_diff", "market_prob", "adj_prob", "edge_adj",
    "h2h_n", "h2h_delta", "odds", "result", "close_odds", "clv",
    "h2h_positive",
]

def load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with open(TRADES_CSV, newline="") as f:
        return list(csv.DictReader(f))

def save_trades(trades: list[dict]):
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(trades)

def trade_from_bet(b: dict) -> dict:
    clv = float(b["mp_close"]) - float(b["mp_open"])
    h2h_pos = 1 if float(b["h2h_delta"]) > 0.02 else 0
    return {
        "eid":         b["eid"],
        "date":        b["begin_at"][:10],
        "league":      b["league"],
        "team1":       b.get("team1", ""),
        "team2":       b.get("team2", ""),
        "elo_diff":    f"{b['elo_diff']:.1f}",
        "market_prob": f"{b['mp_open']:.4f}",
        "adj_prob":    f"{b['adj_prob']:.4f}",
        "edge_adj":    f"{b['edge_adj']:.4f}",
        "h2h_n":       str(b["h2h_n"]),
        "h2h_delta":   f"{b['h2h_delta']:.4f}",
        "odds":        f"{b['bet_odds']:.3f}",
        "result":      str(b["result"]),   # 1=win 0=loss -1=pending
        "close_odds":  f"{b.get('close_odds', b['bet_odds']):.3f}",
        "clv":         f"{clv:.4f}",
        "h2h_positive": str(h2h_pos),
    }

# ── Pipeline (frozen model) ───────────────────────────────────────────────────

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
    return 1.0 / (1.0 + mpow(10.0, (rb-ra)/400.0))

def build_all_bets_extended(conn):
    """Full pipeline (same as historical). Returns ALL rows including new ones."""
    START_ELO  = 1500.0
    MIN_GAMES  = 3
    H2H_MAX_W  = 0.40
    H2H_CONF_N = 5.0
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

    def best_odds(mid):
        if mid not in oidx: return None,None
        bd=oidx[mid]
        bm=next((b for b in PREF if b in bd and "open" in bd[b]),None)
        if not bm: bm=next((b for b,d in bd.items() if "open" in d),None)
        if not bm: return None,None
        op=bd[bm]["open"]; cl=bd[bm].get("close",op)
        return bm,{"oh":op["h"],"oa":op["a"],"ch":cl["h"],"ca":cl["a"],
                   "t1":op["t1"],"t2":op["t2"]}

    now_dt = datetime.now(timezone.utc)
    elo    = defaultdict(lambda: START_ELO)
    games  = defaultdict(int)
    h2h    = defaultdict(list)
    rows   = []

    for r in matches:
        t1,t2,win = r["team_1_name"],r["team_2_name"],r["winner_name"]
        eid = str(r["external_id"]); ln = r["league_name"] or ""; bat = r["begin_at"] or ""
        result = 1 if win==t1 else 0
        e1,e2  = elo[t1],elo[t2]; ep = elo_prob(e1,e2); ediff = abs(e1-e2)

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

        hc  = min(h2h_n/H2H_CONF_N, 1.0)
        adj = ep*(1-H2H_MAX_W*hc) + h2h_wr*(H2H_MAX_W*hc)
        h2h_delta = adj - ep

        if games[t1]>=MIN_GAMES and games[t2]>=MIN_GAMES:
            bm,od = best_odds(eid)
            if od:
                mp1,mp2,valid = get_mp(t1,t2,od["t1"],od["t2"],od["oh"],od["oa"])
                mp1c,_,_      = get_mp(t1,t2,od["t1"],od["t2"],
                                       od["ch"] or od["oh"],od["ca"] or od["oa"])
                if valid and mp1 is not None:
                    ea  = adj - mp1
                    bo  = od["oh"] if team_match(t1,od["t1"]) else od["oa"]
                    rows.append(dict(
                        eid=eid, begin_at=bat, month=bat[:7],
                        league=ln.replace("DOTA2 - ","").replace("DOTA2","?"),
                        team1=t1, team2=t2,
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

# ── Stats helpers ─────────────────────────────────────────────────────────────

def bootstrap_ci(vals, n=N_BOOT, seed=SEED):
    if len(vals) < 2: return float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(vals,k=len(vals)))/len(vals) for _ in range(n))
    return means[int(0.025*n)], means[int(0.975*n)]

def beta_stats(alpha, beta_param):
    """Mean and 95% CI of Beta(alpha, beta) using normal approximation."""
    mean = alpha / (alpha + beta_param)
    var  = alpha*beta_param / ((alpha+beta_param)**2 * (alpha+beta_param+1))
    std  = var**0.5
    return mean, max(0.0, mean-1.96*std), min(1.0, mean+1.96*std)

def p_edge_positive(profits, seed=SEED):
    """Bootstrap P(mean_ROI > 0)."""
    if not profits: return float("nan")
    lo, hi = bootstrap_ci(profits, seed=seed)
    if lo != lo: return float("nan")
    # Estimate from bootstrap distribution
    rng = random.Random(seed)
    means = [sum(rng.choices(profits,k=len(profits)))/len(profits) for _ in range(N_BOOT)]
    return sum(1 for m in means if m > 0) / N_BOOT

def compute_stats(trades: list[dict]):
    """Compute stats from settled trades (result != -1)."""
    settled = [t for t in trades if t["result"] != "-1"]
    if not settled: return None

    n    = len(settled)
    wins = sum(1 for t in settled if t["result"]=="1")
    wr   = wins/n
    profits = [(float(t["odds"])-1) if t["result"]=="1" else -1.0 for t in settled]
    roi  = sum(profits)/n
    clvs = [float(t["clv"]) for t in settled]
    clv_pos  = sum(1 for c in clvs if c>0)/n
    avg_clv  = sum(clvs)/n
    mkt_wins = sum(1 for t in settled if float(t["market_prob"])>0.5) # favorites
    mkt_wr   = mkt_wins/n

    lo, hi = bootstrap_ci(profits)
    p_pos  = p_edge_positive(profits)

    # Bayesian WR: Beta(prior_wins + new_wins, prior_losses + new_losses)
    prior_a = HIST_WINS + 1          # add 1 for prior strength
    prior_b = (HIST_N - HIST_WINS) + 1
    post_a  = prior_a + wins
    post_b  = prior_b + (n - wins)
    bay_mean, bay_lo, bay_hi = beta_stats(post_a, post_b)

    return dict(
        n=n, wins=wins, wr=wr, roi=roi, lo=lo, hi=hi,
        clv_pos=clv_pos, avg_clv=avg_clv, mkt_wr=mkt_wr,
        profits=profits, clvs=clvs, p_edge_pos=p_pos,
        bay_mean=bay_mean, bay_lo=bay_lo, bay_hi=bay_hi,
    )

# ── Scan ─────────────────────────────────────────────────────────────────────

def scan(verbose=True):
    """Scan DB for new Rule C bets since FREEZE_DATE. Update paper_trades.csv."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if verbose: print(f"Загружаем модель...", flush=True)
    all_rows = build_all_bets_extended(conn)
    conn.close()

    # Filter: only new matches after freeze date
    new_candidates = [b for b in all_rows
                      if b["begin_at"] >= FREEZE_DATE and RULE_C(b)]

    if verbose:
        print(f"  Всего матчей в модели: {len(all_rows)}")
        print(f"  Rule C кандидатов после {FREEZE_DATE}: {len(new_candidates)}")

    # Load existing trades
    existing = load_trades()
    existing_eids = {t["eid"] for t in existing}

    # Find truly new ones
    added = []
    for b in new_candidates:
        if b["eid"] not in existing_eids:
            t = trade_from_bet(b)
            existing.append(t)
            added.append(t)

    if added:
        save_trades(existing)
        if verbose:
            print(f"\n  ✓ Добавлено новых ставок: {len(added)}")
            for t in added:
                mark = "W" if t["result"]=="1" else ("L" if t["result"]=="0" else "?")
                print(f"    [{mark}] {t['date']}  {t['league']:25}  "
                      f"{t['team1'][:15]:15} vs {t['team2'][:15]:15}  "
                      f"odds={t['odds']}  edge={t['edge_adj']}  CLV={t['clv']}")
    else:
        if verbose: print(f"\n  Новых ставок не найдено.")

    return existing, added

# ── Report ────────────────────────────────────────────────────────────────────

def generate_report(trades: list[dict], print_to_console=True):
    settled = [t for t in trades if t["result"] != "-1"]
    pending = [t for t in trades if t["result"] == "-1"]
    n_all   = len(settled)

    lines = []
    w = lines.append

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    w(f"# Paper Trading Log — dota_trader_v2")
    w(f"**Обновлено:** {now_str}")
    w(f"**Freeze date:** {FREEZE_DATE}")
    w(f"**Rule C:** `edge_adj>0 AND elo_diff>=75 AND odds<2.0 AND market_prob 60-70%`")
    w(f"**Цель paper trading:** первые 30 новых ставок — независимая выборка.")
    w(f"")

    if not settled:
        w(f"*Ставок пока нет. Ожидаем первую ставку Rule C.*")
        if pending:
            w(f"\n**Ожидают результата:** {len(pending)}")
            for t in pending:
                w(f"- {t['date']}  {t['league']}  {t['team1']} vs {t['team2']}  "
                  f"odds={t['odds']}")
        report_text = "\n".join(lines)
        REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
        REPORT_MD.write_text(report_text)
        if print_to_console: print(report_text)
        return

    # Progress toward 30-bet milestone
    progress = f"{n_all}/30"
    bar_full = int(n_all * 20 / 30)
    bar = "█"*bar_full + "░"*(20-bar_full)
    w(f"**Прогресс к 30 ставкам:** [{bar}] {progress}")
    w(f"")

    # Overall stats
    s = compute_stats(settled)
    w(f"---")
    w(f"## Cumulative Stats (n={s['n']})")
    w(f"")
    lo_s = f"{s['lo']:+.3f}" if s['lo']==s['lo'] else "nan"
    hi_s = f"{s['hi']:+.3f}" if s['hi']==s['hi'] else "nan"
    ci_mark = "✓" if (s['lo']==s['lo'] and s['lo']>0) else "○"
    w(f"```")
    w(f"WR          = {s['wr']:.3f}  ({s['wins']}/{s['n']})")
    w(f"ROI         = {s['roi']:+.4f}   CI=[{lo_s},{hi_s}]  {ci_mark}")
    w(f"CLV+        = {s['clv_pos']:.3f}  ({'✓' if s['clv_pos']>0.5 else '✗'} >50%)")
    w(f"avgCLV      = {s['avg_clv']:+.4f}")
    w(f"market_WR   = {s['mkt_wr']:.3f}  (рынок угадывает)")
    w(f"P(edge>0)   = {s['p_edge_pos']:.1%}  (bootstrap)")
    w(f"")
    w(f"Bayesian WR = {s['bay_mean']:.3f}  CI=[{s['bay_lo']:.3f},{s['bay_hi']:.3f}]")
    w(f"  (prior: {HIST_N} исторических ставок Rule C)")
    w(f"```")
    w(f"")

    # H2H breakdown
    h2h_pos  = [t for t in settled if t["h2h_positive"]=="1"]
    h2h_neut = [t for t in settled if t["h2h_positive"]=="0"]
    w(f"### H2H Breakdown")
    w(f"```")
    for label, grp in [("H2H positive (delta>+2%)", h2h_pos),
                        ("H2H neutral  (delta≤+2%)", h2h_neut)]:
        if not grp:
            w(f"{label:30}  n=0"); continue
        sg = compute_stats(grp)
        if not sg: continue
        lo_g = f"{sg['lo']:+.3f}" if sg['lo']==sg['lo'] else " nan"
        hi_g = f"{sg['hi']:+.3f}" if sg['hi']==sg['hi'] else " nan"
        mark = " ✓" if (sg['lo']==sg['lo'] and sg['lo']>0) else "  "
        w(f"{label:30}  n={sg['n']:>3}  WR={sg['wr']:.3f}  "
          f"ROI={sg['roi']:>+7.3f}  CLV+={sg['clv_pos']:.3f}{mark}")
    w(f"```")
    w(f"")

    # Milestone blocks (every 10 bets)
    if n_all >= MILESTONE_N:
        w(f"---")
        w(f"## Milestone Checkpoints")
        for milestone in range(MILESTONE_N, n_all+1, MILESTONE_N):
            chunk = settled[:milestone]
            ms = compute_stats(chunk)
            if not ms: continue
            lo_m = f"{ms['lo']:+.3f}" if ms['lo']==ms['lo'] else " nan"
            hi_m = f"{ms['hi']:+.3f}" if ms['hi']==ms['hi'] else " nan"
            ci_m = "✓" if (ms['lo']==ms['lo'] and ms['lo']>0) else "○"
            signal = "🟢 GREEN" if (ms['lo']==ms['lo'] and ms['lo']>0 and ms['clv_pos']>0.55) else \
                     "🟡 YELLOW" if ms['clv_pos']>0.5 else "🔴 RED"
            w(f"")
            w(f"### После {milestone} ставок  —  {signal}")
            w(f"```")
            w(f"  WR          = {ms['wr']:.3f}")
            w(f"  ROI         = {ms['roi']:>+.4f}   CI=[{lo_m},{hi_m}]  {ci_m}")
            w(f"  CLV+        = {ms['clv_pos']:.3f}")
            w(f"  Bayesian WR = {ms['bay_mean']:.3f}  [{ms['bay_lo']:.3f},{ms['bay_hi']:.3f}]")
            w(f"  P(edge>0)   = {ms['p_edge_pos']:.1%}")
            w(f"```")
        w(f"")

    # Weekly summary
    w(f"---")
    w(f"## Weekly History")
    w(f"")
    by_week = defaultdict(list)
    for t in settled:
        dt = datetime.strptime(t["date"], "%Y-%m-%d")
        week = dt.strftime("%Y-W%W")
        by_week[week].append(t)

    if by_week:
        w(f"```")
        w(f"{'Неделя':12}  {'n':>3}  {'WR':>5}  {'ROI':>7}  {'CLV+':>6}  {'avgCLV':>7}")
        w(f"{'-'*60}")
        for week in sorted(by_week.keys()):
            wg = by_week[week]
            sg = compute_stats(wg)
            if not sg: continue
            mark = "✓" if sg['roi']>0 else "✗"
            w(f"  {week:10}  {sg['n']:>3}  {sg['wr']:>5.3f}  "
              f"{sg['roi']:>+7.3f}  {sg['clv_pos']:>6.3f}  {sg['avg_clv']:>+7.4f}  {mark}")
        w(f"```")
        w(f"")

    # Individual bets log
    w(f"---")
    w(f"## Bet Log")
    w(f"")
    w(f"```")
    w(f"{'#':>3}  {'Date':10}  {'League':20}  {'Teams':32}  "
      f"{'odds':>5}  {'mkt':>5}  {'edge':>6}  {'H2H':>4}  {'R':>2}  {'CLV':>7}")
    w(f"{'-'*105}")
    for i, t in enumerate(settled, 1):
        mark  = "W" if t["result"]=="1" else "L"
        h_tag = "+" if t["h2h_positive"]=="1" else "~"
        teams = f"{t['team1'][:14]:14} vs {t['team2'][:14]:14}"
        w(f"  {i:>3}  {t['date']:10}  {t['league'][:20]:20}  {teams}  "
          f"{float(t['odds']):>5.3f}  {float(t['market_prob']):>5.3f}  "
          f"{float(t['edge_adj']):>+6.4f}  {h_tag:>4}  {mark:>2}  {float(t['clv']):>+7.4f}")
    w(f"```")
    w(f"")

    # Pending bets
    if pending:
        w(f"---")
        w(f"## Ожидают результата ({len(pending)})")
        w(f"```")
        for t in pending:
            h_tag = "+" if t["h2h_positive"]=="1" else "~"
            teams = f"{t['team1'][:14]:14} vs {t['team2'][:14]:14}"
            w(f"  {t['date']:10}  {t['league'][:20]:20}  {teams}  "
              f"odds={t['odds']}  edge={t['edge_adj']}  H2H={h_tag}")
        w(f"```")
        w(f"")

    # Stop-signal check
    if n_all >= 20:
        recent = settled[-5:]
        recent_profits = [(float(t["odds"])-1) if t["result"]=="1" else -1.0 for t in recent]
        losing_streak = 0
        for p in reversed(recent_profits):
            if p < 0: losing_streak += 1
            else: break
        if losing_streak >= 5:
            w(f"")
            w(f"⚠️  **СТОП-СИГНАЛ:** {losing_streak} убытков подряд при n≥20. Пересмотреть модель.")
        w(f"")
        w(f"*Текущий losing streak: {losing_streak}*")

    # Footer
    w(f"")
    w(f"---")
    w(f"*Следующий этап исследований: после 30 новых ставок.*")
    w(f"*Bootstrap: n={N_BOOT}, seed={SEED}*")

    report_text = "\n".join(lines)
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(report_text)
    if print_to_console: print(report_text)
    return report_text

# ── Status (brief) ────────────────────────────────────────────────────────────

def print_status(trades: list[dict]):
    settled = [t for t in trades if t["result"] != "-1"]
    pending = [t for t in trades if t["result"] == "-1"]
    n = len(settled)
    print(f"\n  Paper Trading Status")
    print(f"  {'='*40}")
    print(f"  Settled: {n}  |  Pending: {len(pending)}  |  Goal: 30")
    if settled:
        s = compute_stats(settled)
        lo_s = f"{s['lo']:+.3f}" if s['lo']==s['lo'] else " nan"
        hi_s = f"{s['hi']:+.3f}" if s['hi']==s['hi'] else " nan"
        print(f"  WR={s['wr']:.3f}  ROI={s['roi']:+.4f}  CI=[{lo_s},{hi_s}]")
        print(f"  CLV+={s['clv_pos']:.3f}  P(edge>0)={s['p_edge_pos']:.1%}")
    else:
        print(f"  Нет ставок ещё.")
    print()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Paper Trading Monitor")
    parser.add_argument("--scan",   action="store_true", help="Только сканировать DB")
    parser.add_argument("--report", action="store_true", help="Только отчёт")
    parser.add_argument("--status", action="store_true", help="Краткий статус")
    args = parser.parse_args()

    do_scan   = args.scan   or (not args.report and not args.status)
    do_report = args.report or (not args.scan   and not args.status)

    if do_scan:
        trades, added = scan(verbose=True)
    else:
        trades = load_trades()

    if args.status:
        print_status(trades)
        return

    if do_report:
        generate_report(trades, print_to_console=True)
        print(f"\n  Отчёт сохранён: {REPORT_MD}")

if __name__ == "__main__":
    main()
