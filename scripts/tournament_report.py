#!/usr/bin/env python3
"""
tournament_report.py
Шаг 4: Генерируем Markdown-отчёт с лидербордом и выводами.

Запуск:
    PYTHONPATH=. python3 scripts/tournament_report.py
    PYTHONPATH=. python3 scripts/tournament_report.py --output reports/tournament_report.md
"""
from __future__ import annotations
import sqlite3, os, argparse, datetime

TOURN_DB    = os.path.join(os.path.dirname(__file__), "../data/model_tournament.db")
DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "../reports/tournament_report.md")

FREEZE_DATE = "2026-06-16"


def fmt_pct(v): return f"{v:.1f}%"
def fmt_f2(v):  return f"{v:.2f}"
def fmt_f4(v):  return f"{v:.4f}"


def leaderboard_table(rows, cols, title):
    lines = [f"### {title}\n"]
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines.append(header)
    lines.append(sep)
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def generate_report(out_path: str):
    tcon = sqlite3.connect(TOURN_DB)
    tcur = tcon.cursor()

    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Метаданные
    meta = dict(tcur.execute("SELECT key, value FROM tournament_meta").fetchall())
    total_matches = meta.get("total_matches_inserted", "?")
    build_ts = meta.get("build_completed_at", "?")

    lines = []
    lines.append(f"# Dota Trader v2 — Внутренний Турнир Моделей\n")
    lines.append(f"**Сформировано:** {now_str}  ")
    lines.append(f"**FREEZE_DATE:** {FREEZE_DATE}  ")
    lines.append(f"**Матчей в базе:** {total_matches}  ")
    lines.append(f"**Build features:** {build_ts}\n")

    lines.append("---\n")

    # ── Обзор по Division ────────────────────────────────────────────────────
    lines.append("## 1. Покрытие данных\n")
    div_rows = tcur.execute("""
        SELECT division, split, COUNT(*) as n
        FROM tournament_blind_features
        GROUP BY division, split
        ORDER BY division, split
    """).fetchall()

    lines.append("| Division | Split | Матчей |")
    lines.append("|---|---|---:|")
    for div, spl, n in div_rows:
        lines.append(f"| {div} | {spl} | {n:,} |")
    lines.append("")

    lines.append("> **Division A** — любой букмекер | **B** — только Pinnacle | **C** — Pinnacle + ≥5 pre-match точек\n")

    # ── Сводная таблица по TEST (основная) ───────────────────────────────────
    lines.append("## 2. Лидерборд TEST (основной результат)\n")
    lines.append("> *Все стратегии кроме оракула M16 и post-hoc M06 можно использовать в production.*\n")

    test_rows = tcur.execute("""
        SELECT strategy_name, division, total_bets, win_rate,
               roi_pct, gross_pnl, avg_odds, avg_edge, avg_clv,
               bank_final, max_drawdown, sharpe
        FROM tournament_metrics
        WHERE split = 'TEST'
        ORDER BY division, roi_pct DESC
    """).fetchall()

    for div in ('A', 'B', 'C'):
        div_data = [r for r in test_rows if r[1] == div]
        if not div_data:
            continue
        cols = ["Стратегия", "Ставок", "Win%", "ROI%", "P&L $", "Avg Odds", "Avg Edge", "Sharpe", "MDD%"]
        table_rows = []
        for r in div_data:
            strat, _, bets, wr, roi, pnl, odds, edge, clv, bank, mdd, sharpe = r
            is_oracle   = strat == 'M16'
            is_posthoc  = strat == 'M06'
            flag = " ⚠️oracle" if is_oracle else (" ⚠️posthoc" if is_posthoc else "")
            table_rows.append([
                f"**{strat}**{flag}",
                bets,
                fmt_pct(wr * 100),
                fmt_pct(roi),
                fmt_f2(pnl),
                fmt_f4(odds),
                fmt_f4(edge),
                fmt_f4(sharpe),
                fmt_pct(mdd),
            ])
        lines.append(leaderboard_table(table_rows, cols, f"Division {div} — TEST"))
        lines.append("")

    # ── M05 Rule C Frozen — детали ───────────────────────────────────────────
    lines.append("## 3. M05 Rule C FROZEN — Полная Сводка\n")
    lines.append("> Rule C: edge_adj > 0 AND elo_diff >= 75 AND odds < 2.0 AND market_prob 60–70%\n")

    m05_rows = tcur.execute("""
        SELECT split, division, total_bets, win_rate, roi_pct, gross_pnl, sharpe, max_drawdown
        FROM tournament_metrics
        WHERE strategy_name = 'M05'
        ORDER BY split, division
    """).fetchall()

    lines.append("| Split | Division | Ставок | Win% | ROI% | P&L $ | Sharpe | MDD% |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for spl, div, bets, wr, roi, pnl, sh, mdd in m05_rows:
        lines.append(f"| {spl} | {div} | {bets} | {fmt_pct(wr*100)} | {fmt_pct(roi)} | {fmt_f2(pnl)} | {fmt_f4(sh)} | {fmt_pct(mdd)} |")
    lines.append("")

    # ── TOP-5 стратегий TEST по ROI (исключая Oracle и post-hoc) ────────────
    lines.append("## 4. Топ-5 Стратегий TEST (честные)\n")
    top5 = tcur.execute("""
        SELECT m.strategy_name, m.division, m.total_bets, m.win_rate,
               m.roi_pct, m.gross_pnl, m.sharpe, m.max_drawdown,
               r.is_oracle, r.is_posthoc, r.description
        FROM tournament_metrics m
        LEFT JOIN tournament_strategy_registry r ON m.strategy_name = r.strategy_name
        WHERE m.split = 'TEST'
        AND (r.is_oracle = 0 OR r.is_oracle IS NULL)
        AND (r.is_posthoc = 0 OR r.is_posthoc IS NULL)
        AND m.total_bets >= 5
        ORDER BY m.roi_pct DESC
        LIMIT 5
    """).fetchall()

    lines.append("| # | Стратегия | Div | Ставок | Win% | ROI% | P&L $ | Sharpe | Описание |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---|")
    for i, row in enumerate(top5, 1):
        strat, div, bets, wr, roi, pnl, sh, mdd, _, _, desc = row
        desc = (desc or '')[:50]
        lines.append(f"| {i} | **{strat}** | {div} | {bets} | {fmt_pct(wr*100)} | {fmt_pct(roi)} | {fmt_f2(pnl)} | {fmt_f4(sh)} | {desc} |")
    lines.append("")

    # ── Oracle benchmark ─────────────────────────────────────────────────────
    lines.append("## 5. Oracle Benchmark (M16)\n")
    oracle_rows = tcur.execute("""
        SELECT split, division, total_bets, win_rate, roi_pct, gross_pnl
        FROM tournament_metrics
        WHERE strategy_name = 'M16'
        ORDER BY split, division
    """).fetchall()
    if oracle_rows:
        lines.append("| Split | Division | Ставок | Win% | ROI% | P&L $ |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for spl, div, bets, wr, roi, pnl in oracle_rows:
            lines.append(f"| {spl} | {div} | {bets} | {fmt_pct(wr*100)} | {fmt_pct(roi)} | {fmt_f2(pnl)} |")
        lines.append("")
        lines.append("> Оракул использует close odds (утечка). Только верхняя граница ROI возможного.\n")

    # ── VAL vs TEST сравнение (overfitting check) ────────────────────────────
    lines.append("## 6. Overfitting Check: VAL → TEST\n")
    lines.append("| Стратегия | Division | ROI VAL% | ROI TEST% | Δ |")
    lines.append("|---|---|---:|---:|---:|")
    val_test = tcur.execute("""
        SELECT v.strategy_name, v.division,
               v.roi_pct as roi_val, t.roi_pct as roi_test
        FROM tournament_metrics v
        JOIN tournament_metrics t
          ON v.strategy_name = t.strategy_name AND v.division = t.division
        WHERE v.split = 'VAL' AND t.split = 'TEST'
        ORDER BY v.division, v.strategy_name
    """).fetchall()
    for strat, div, roi_v, roi_t in val_test:
        delta = roi_t - roi_v
        flag = " ✅" if roi_t > 0 else (" ⚠️" if delta < -10 else "")
        lines.append(f"| {strat} | {div} | {fmt_pct(roi_v)} | {fmt_pct(roi_t)} | {delta:+.1f}%{flag} |")
    lines.append("")

    # ── Выводы ───────────────────────────────────────────────────────────────
    lines.append("## 7. Выводы и Рекомендации\n")

    # Auto-generate conclusions
    profitable_test = [r for r in test_rows
                       if r[4] > 0  # roi_pct > 0
                       and r[2] >= 5]  # min 5 bets
    lines.append(f"**Прибыльных стратегий в TEST:** {len(profitable_test)} из {len(test_rows)}\n")

    if profitable_test:
        lines.append("**Кандидаты к production (TEST ROI > 0, ≥5 ставок):**\n")
        for r in sorted(profitable_test, key=lambda x: -x[4]):
            strat, div, bets, wr, roi, pnl, odds, edge, clv, bank, mdd, sharpe = r
            lines.append(f"- **{strat}** (Div {div}): {bets} ставок, ROI={fmt_pct(roi)}, Sharpe={fmt_f4(sharpe)}")
        lines.append("")

    lines.append("**Ограничения анализа:**\n")
    lines.append("- Flat stake $20, фиксированный банк $1,000 — не Kelly")
    lines.append("- odds_history coverage ~58% (только PinnacleSports pre-match)")
    lines.append("- TI Quals SA: нет котировок в BetsAPI odds/summary")
    lines.append("- M06/M16 исключены из production — post-hoc/oracle утечка")
    lines.append("- Следующий шаг: минимум 30 settled Rule C сигналов перед изменением параметров\n")

    lines.append("---")
    lines.append(f"*Сгенерировано: {now_str} | FREEZE_DATE: {FREEZE_DATE}*")

    # Сохраняем
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")

    print(f"Отчёт сохранён: {out_path}")
    tcon.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=DEFAULT_OUT)
    args = ap.parse_args()
    generate_report(args.output)
