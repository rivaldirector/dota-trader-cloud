#!/usr/bin/env python3
"""
Paper Report — Задачи 3 + 4 (weekly report + milestone stats)
=============================================================
Запуск:
  PYTHONPATH=. python3 scripts/paper_report.py
  PYTHONPATH=. python3 scripts/paper_report.py --save   # сохранить в MD
"""
from __future__ import annotations

import sys, argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts._paper_core import (
    get_paper_conn, compute_stats, signal_status,
    PRIOR_WINS, PRIOR_LOSSES,
)

REPORT_PATH = ROOT / "reports" / "paper_trading_log.md"
MILESTONE_N = 10
GOAL_N      = 30


def fmt_ci(lo, hi):
    if lo != lo: return "[nan,nan]"
    return f"[{lo:+.3f},{hi:+.3f}]"


def generate_report(save=False) -> str:
    conn = get_paper_conn()
    all_trades = conn.execute(
        "SELECT * FROM paper_trades ORDER BY start_time ASC"
    ).fetchall()
    conn.close()

    settled = [t for t in all_trades if t["status"] in ("WON","LOST")]
    pending = [t for t in all_trades if t["status"] == "PENDING"]
    void    = [t for t in all_trades if t["status"] == "VOID"]
    n_all   = len(settled)

    lines = []
    w = lines.append

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    w(f"# Paper Trading Log — dota_trader_v2")
    w(f"**Обновлено:** {now_str}  |  **Freeze date:** 2026-06-16")
    w(f"**Rule C:** `edge_adj>0 AND elo_diff>=75 AND odds<2.0 AND market_prob 60-70%`")
    w(f"")

    # Progress bar
    filled = int(n_all * 30 / GOAL_N)
    bar = "█"*min(filled,30) + "░"*(30-min(filled,30))
    w(f"**Прогресс:** [{bar}] {n_all}/{GOAL_N}")
    w(f"Total: {len(all_trades)}  |  Settled: {n_all}  |  Pending: {len(pending)}  |  Void: {len(void)}")
    w(f"")

    # ── Cumulative stats ───────────────────────────────────────────────────
    w(f"---")
    w(f"## Cumulative Stats")
    w(f"")

    s = compute_stats(settled)
    if not s:
        w(f"*Нет settled ставок.*")
    else:
        status = signal_status(s)
        w(f"### {status}")
        w(f"```")
        w(f"n           = {s['n']}  (wins={s['wins']})")
        w(f"WR          = {s['wr']:.3f}")
        w(f"ROI         = {s['roi']:+.4f}   CI={fmt_ci(s['lo'],s['hi'])}")
        w(f"CLV+        = {s['clvpos']:.3f}  ({'✓ >50%' if s['clvpos']>0.5 else '✗ ≤50%'})")
        w(f"avg CLV     = {s['avg_clv']:+.4f}")
        w(f"max DD      = {s['max_dd']:.4f}")
        w(f"max LS      = {s['max_ls']}")
        w(f"P(edge>0)   = {s['p_edge_pos']:.1%}")
        w(f"Bayesian WR = {s['bay_mean']:.3f}  [{s['bay_lo']:.3f},{s['bay_hi']:.3f}]")
        w(f"  (prior: Beta({PRIOR_WINS},{PRIOR_LOSSES}) = {PRIOR_WINS-1} hist wins)")
        w(f"```")
        w(f"")

        # Stop-signal
        if s["max_ls"] >= 5 and s["n"] >= 20:
            w(f"⚠️  **СТОП-СИГНАЛ:** {s['max_ls']} убытков подряд при n≥20")
            w(f"")

    # ── H2H breakdown ─────────────────────────────────────────────────────
    w(f"---")
    w(f"## H2H Breakdown")
    w(f"```")
    w(f"{'Группа':30}  {'n':>4}  {'WR':>5}  {'ROI':>7}  {'CI':>18}  {'CLV+':>6}  {'avgCLV':>7}")
    w(f"{'-'*85}")

    h2h_pos  = [t for t in settled if (t["h2h_delta"] or 0) > 0.02]
    h2h_neut = [t for t in settled if (t["h2h_delta"] or 0) <= 0.02]
    for label, grp in [("H2H positive (delta>+2%)", h2h_pos),
                        ("H2H neutral  (delta≤+2%)", h2h_neut)]:
        sg = compute_stats(grp)
        if not sg: w(f"  {label:30}  n=0"); continue
        mark = " ✓" if (sg["lo"]==sg["lo"] and sg["lo"]>0) else "  "
        w(f"  {label:30}  {sg['n']:>4}  {sg['wr']:>5.3f}  {sg['roi']:>+7.3f}  "
          f"{fmt_ci(sg['lo'],sg['hi']):>18}  {sg['clvpos']:>6.3f}  {sg['avg_clv']:>+7.4f}{mark}")
    w(f"```")
    w(f"")

    # ── League breakdown ───────────────────────────────────────────────────
    w(f"---")
    w(f"## League Breakdown")
    w(f"```")
    w(f"{'League':30}  {'n':>4}  {'WR':>5}  {'ROI':>7}  {'CLV+':>6}  {'avgCLV':>7}")
    w(f"{'-'*70}")

    by_league = defaultdict(list)
    for t in settled: by_league[t["league"] or "?"].append(t)
    # DreamLeague first
    dl_key = next((k for k in by_league if "dream" in k.lower()), None)
    keys_sorted = ([dl_key] if dl_key else []) + \
                  sorted(k for k in by_league if k != dl_key)
    for lg in keys_sorted:
        grp = by_league[lg]
        sg = compute_stats(grp)
        if not sg: continue
        tag = " ← DreamLeague" if dl_key and lg == dl_key else ""
        w(f"  {lg[:30]:30}  {sg['n']:>4}  {sg['wr']:>5.3f}  {sg['roi']:>+7.3f}  "
          f"{sg['clvpos']:>6.3f}  {sg['avg_clv']:>+7.4f}{tag}")

    # DreamLeague vs non
    if dl_key:
        dl_grp   = by_league[dl_key]
        nodl_grp = [t for t in settled if (t["league"] or "?") != dl_key]
        for label, grp in [("DreamLeague (total)", dl_grp),
                            ("non-DreamLeague",    nodl_grp)]:
            sg = compute_stats(grp)
            if not sg: continue
            w(f"  {'─'*68}")
            w(f"  {label:30}  {sg['n']:>4}  {sg['wr']:>5.3f}  {sg['roi']:>+7.3f}  "
              f"{sg['clvpos']:>6.3f}  {sg['avg_clv']:>+7.4f}")
    w(f"```")
    w(f"")

    # ── Bookmaker breakdown ────────────────────────────────────────────────
    w(f"---")
    w(f"## Bookmaker Breakdown")
    w(f"```")
    w(f"{'Bookmaker':20}  {'n':>4}  {'WR':>5}  {'ROI':>7}  {'CLV+':>6}  {'avgCLV':>7}")
    w(f"{'-'*60}")
    by_bm = defaultdict(list)
    for t in settled: by_bm[t["bookmaker"] or "?"].append(t)
    for bm in sorted(by_bm, key=lambda b: -len(by_bm[b])):
        grp = by_bm[bm]
        sg = compute_stats(grp)
        if not sg: continue
        w(f"  {bm[:20]:20}  {sg['n']:>4}  {sg['wr']:>5.3f}  {sg['roi']:>+7.3f}  "
          f"{sg['clvpos']:>6.3f}  {sg['avg_clv']:>+7.4f}")
    w(f"```")
    w(f"")

    # ── Weekly history ─────────────────────────────────────────────────────
    w(f"---")
    w(f"## Weekly History")
    w(f"```")
    w(f"{'Неделя':12}  {'n':>3}  {'WR':>5}  {'ROI':>7}  {'CLV+':>6}  {'avgCLV':>7}")
    w(f"{'-'*55}")
    by_week = defaultdict(list)
    for t in settled:
        dt   = t["start_time"][:10] if t["start_time"] else "?"
        try: week = datetime.strptime(dt, "%Y-%m-%d").strftime("%Y-W%W")
        except: week = "?"
        by_week[week].append(t)
    for wk in sorted(by_week):
        sg = compute_stats(by_week[wk])
        if not sg: continue
        mark = " ✓" if sg["roi"]>0 else " ✗"
        w(f"  {wk:12}  {sg['n']:>3}  {sg['wr']:>5.3f}  {sg['roi']:>+7.3f}  "
          f"{sg['clvpos']:>6.3f}  {sg['avg_clv']:>+7.4f}{mark}")
    w(f"```")
    w(f"")

    # ── Milestone reports ──────────────────────────────────────────────────
    if n_all >= MILESTONE_N:
        w(f"---")
        w(f"## Milestone Checkpoints")
        for ms in range(MILESTONE_N, n_all+1, MILESTONE_N):
            chunk = settled[:ms]
            ms_s  = compute_stats(chunk)
            if not ms_s: continue
            stat  = signal_status(ms_s)
            lo_s  = f"{ms_s['lo']:+.3f}" if ms_s["lo"]==ms_s["lo"] else " nan"
            hi_s  = f"{ms_s['hi']:+.3f}" if ms_s["hi"]==ms_s["hi"] else " nan"
            w(f"")
            w(f"### После {ms} ставок — {stat}")
            w(f"```")
            w(f"  WR          = {ms_s['wr']:.3f}")
            w(f"  ROI         = {ms_s['roi']:>+.4f}   CI=[{lo_s},{hi_s}]")
            w(f"  CLV+        = {ms_s['clvpos']:.3f}")
            w(f"  avg CLV     = {ms_s['avg_clv']:+.4f}")
            w(f"  Bayesian WR = {ms_s['bay_mean']:.3f}  [{ms_s['bay_lo']:.3f},{ms_s['bay_hi']:.3f}]")
            w(f"  P(edge>0)   = {ms_s['p_edge_pos']:.1%}")
            w(f"  max DD      = {ms_s['max_dd']:.4f}")
            w(f"  max LS      = {ms_s['max_ls']}")
            w(f"```")
        w(f"")

    # ── Bet log ────────────────────────────────────────────────────────────
    w(f"---")
    w(f"## Bet Log")
    w(f"```")
    w(f"{'#':>3}  {'Date':10}  {'League':22}  {'Bet team':18}  "
      f"{'odds':>5}  {'mkt':>5}  {'edge':>6}  {'H2H':>3}  {'R':>4}  {'CLV':>7}  {'P&L':>6}")
    w(f"{'-'*108}")
    for i, t in enumerate(settled, 1):
        mark   = "W" if t["status"]=="WON" else "L"
        h_tag  = "+" if (t["h2h_delta"] or 0) > 0.02 else "~"
        clv_s  = f"{t['clv']:+.4f}" if t["clv"] is not None else "  n/a"
        pnl_s  = f"{t['profit_flat']:+.3f}" if t["profit_flat"] is not None else "  n/a"
        dt     = (t["start_time"] or "")[:10]
        w(f"  {i:>3}  {dt:10}  {(t['league'] or '')[:22]:22}  "
          f"{(t['bet_team'] or '')[:18]:18}  "
          f"{t['open_odds']:>5.3f}  {t['market_prob']:>5.3f}  "
          f"{t['edge_adj']:>+6.4f}  {h_tag:>3}  {mark:>4}  {clv_s:>7}  {pnl_s:>6}")
    w(f"```")
    w(f"")

    # ── Pending ────────────────────────────────────────────────────────────
    if pending:
        w(f"---")
        w(f"## Ожидают результата ({len(pending)})")
        w(f"```")
        for t in pending:
            h_tag = "+" if (t["h2h_delta"] or 0) > 0.02 else "~"
            dt    = (t["start_time"] or "")[:16]
            w(f"  {dt}  {(t['league'] or '')[:22]:22}  "
              f"{(t['bet_team'] or '')[:18]:18}  "
              f"odds={t['open_odds']}  edge={t['edge_adj']:+.4f}  H2H={h_tag}")
        w(f"```")
        w(f"")

    # ── Footer ─────────────────────────────────────────────────────────────
    w(f"---")
    w(f"*Следующий этап исследований: после {GOAL_N} новых settled сигналов.*")
    if n_all >= GOAL_N:
        w(f"**✓ GOAL REACHED. Начинать следующий этап.**")

    text = "\n".join(lines)

    if save:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(text)
        print(f"Отчёт сохранён: {REPORT_PATH}")

    return text


def main():
    parser = argparse.ArgumentParser(description="Paper Report")
    parser.add_argument("--save", action="store_true",
                        help="Сохранить в reports/paper_trading_log.md")
    args = parser.parse_args()
    print(generate_report(save=args.save))


if __name__ == "__main__":
    main()
