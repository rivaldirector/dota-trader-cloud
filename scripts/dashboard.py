#!/usr/bin/env python3
"""
Генерирует HTML-дашборд из локальной SQLite БД.

Секции:
  1. Upcoming matches с live edge (если есть odds)
  2. Rolling ROI по paper bets
  3. CLV статистика по историческим odds
  4. Топ-20 команд по Elo

Запуск:
    PYTHONPATH=. python3 scripts/dashboard.py
    PYTHONPATH=. python3 scripts/dashboard.py --open   # открыть в браузере
    PYTHONPATH=. python3 scripts/dashboard.py --no-live  # без BetsAPI запросов
"""
from __future__ import annotations

import sys, argparse, sqlite3, json, subprocess
from collections import defaultdict
from datetime import datetime, timezone
from math import pow
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

DB_PATH   = PROJECT_ROOT / settings.database_path
OUT_PATH  = PROJECT_ROOT / "dashboard.html"
K         = 32
START_ELO = 1500.0


# ── Elo ───────────────────────────────────────────────────────────────────────

def build_elo(conn):
    rows = conn.execute("""
        SELECT team_1_name, team_2_name, winner_name, begin_at
        FROM matches WHERE status='finished'
          AND team_1_name IS NOT NULL AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()
    elo   = defaultdict(lambda: START_ELO)
    games = defaultdict(int)
    for r in rows:
        t1, t2, w = r[0], r[1], r[2]
        e1 = elo[t1]; e2 = elo[t2]
        ea = 1.0 / (1.0 + pow(10.0, (e2 - e1) / 400.0))
        s1 = 1 if w == t1 else 0
        elo[t1] = e1 + K * (s1 - ea)
        elo[t2] = e2 + K * ((1 - s1) - (1 - ea))
        games[t1] += 1; games[t2] += 1
    return dict(elo), dict(games)


# ── CLV stats ─────────────────────────────────────────────────────────────────

def clv_stats(conn):
    """
    Для матчей где есть и opening и closing odds:
    CLV = close_market_prob - open_market_prob (если > 0 рынок "догнал" нас)
    """
    rows = conn.execute("""
        SELECT o.match_external_id,
               o.team_1_odds, o.team_2_odds, o.captured_at,
               m.winner_name, m.team_1_name
        FROM odds_snapshots o
        JOIN matches m ON m.external_id = o.match_external_id
        WHERE o.source='betsapi'
          AND o.team_1_odds IS NOT NULL
          AND m.status='finished'
        ORDER BY o.match_external_id, o.bookmaker, o.captured_at
    """).fetchall()

    by_match: dict = defaultdict(lambda: {"open": None, "close": None, "winner": None, "t1": None})
    for r in rows:
        mid = r[0]
        cap = r[3]
        by_match[mid]["winner"] = r[4]
        by_match[mid]["t1"]     = r[5]
        oh, oa = r[1], r[2]
        ih = 1.0 / oh; ia = 1.0 / oa; tot = ih + ia
        p_h = ih / tot
        if cap.endswith("_open") and by_match[mid]["open"] is None:
            by_match[mid]["open"] = p_h
        elif cap.endswith("_close"):
            by_match[mid]["close"] = p_h

    results = []
    for mid, d in by_match.items():
        if d["open"] is None or d["close"] is None:
            continue
        clv = d["close"] - d["open"]  # рынок сдвинулся
        results.append({"mid": mid, "clv": clv, "open": d["open"], "close": d["close"]})

    if not results:
        return {"count": 0, "avg_clv": 0, "pct_positive": 0}

    avg = sum(r["clv"] for r in results) / len(results)
    pos = sum(1 for r in results if r["clv"] > 0)
    return {
        "count": len(results),
        "avg_clv": round(avg, 4),
        "pct_positive": round(pos / len(results) * 100, 1),
    }


# ── Paper bets ROI ────────────────────────────────────────────────────────────

def roi_stats(conn):
    rows = conn.execute("""
        SELECT odds, stake, profit, result
        FROM bets WHERE status='settled'
    """).fetchall()
    if not rows:
        return {"count": 0, "roi": 0, "total_stake": 0, "total_profit": 0, "winrate": 0}
    total_stake  = sum(r[1] for r in rows)
    total_profit = sum(r[2] for r in rows)
    wins = sum(1 for r in rows if r[3] == "win")
    return {
        "count":        len(rows),
        "roi":          round(total_profit / total_stake * 100, 2) if total_stake else 0,
        "total_stake":  round(total_stake, 2),
        "total_profit": round(total_profit, 2),
        "winrate":      round(wins / len(rows) * 100, 1),
    }


# ── DB summary ────────────────────────────────────────────────────────────────

def db_summary(conn):
    matches  = conn.execute("SELECT COUNT(*) FROM matches WHERE status='finished'").fetchone()[0]
    snapshots = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    leagues  = conn.execute(
        "SELECT COUNT(DISTINCT league_name) FROM odds_snapshots WHERE league_name IS NOT NULL"
    ).fetchone()[0]
    return {"matches": matches, "snapshots": snapshots, "leagues": leagues}


# ── Top teams ─────────────────────────────────────────────────────────────────

def top_teams_data(elo, games, n=20):
    ranked = sorted(elo.items(), key=lambda x: -x[1])[:n]
    return [{"team": t, "elo": round(e, 0), "games": games.get(t, 0)} for t, e in ranked]


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="300">
<title>Dota Trader Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'SF Mono', 'Consolas', monospace; background: #0d1117; color: #c9d1d9; font-size: 13px; }}
  .header {{ background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; display:flex; align-items:center; gap:16px; }}
  .header h1 {{ font-size: 18px; color: #58a6ff; }}
  .header .ts {{ color: #8b949e; font-size: 11px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
  .card h2 {{ font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }}
  .stat-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }}
  .stat {{ text-align: center; }}
  .stat .val {{ font-size: 28px; font-weight: bold; color: #58a6ff; }}
  .stat .lbl {{ font-size: 11px; color: #8b949e; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ color: #8b949e; font-size: 11px; text-transform: uppercase; text-align: left;
        padding: 4px 8px; border-bottom: 1px solid #30363d; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #21262d; }}
  tr:last-child td {{ border-bottom: none; }}
  .pos {{ color: #3fb950; }}
  .neg {{ color: #f85149; }}
  .neu {{ color: #8b949e; }}
  .star {{ color: #d29922; }}
  .wide {{ grid-column: 1 / -1; }}
  .tag {{ display:inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; }}
  .tag-s {{ background:#1f6feb22; color:#58a6ff; border:1px solid #1f6feb; }}
  .tag-a {{ background:#3fb95022; color:#3fb950; border:1px solid #3fb950; }}
  .tag-b {{ background:#8b949e22; color:#8b949e; border:1px solid #8b949e; }}
</style>
</head>
<body>
<div class="header">
  <h1>🎮 Dota Trader</h1>
  <span class="ts">Обновлено: {ts} · БД: {db_matches} матчей · {db_snapshots} odds snapshots · {db_leagues} лиг</span>
</div>
<div class="grid">

  <!-- ROI Card -->
  <div class="card">
    <h2>Paper Trading ROI</h2>
    <div class="stat-row">
      <div class="stat"><div class="val {roi_color}">{roi}%</div><div class="lbl">ROI</div></div>
      <div class="stat"><div class="val">{bet_count}</div><div class="lbl">Ставок</div></div>
      <div class="stat"><div class="val">{winrate}%</div><div class="lbl">Winrate</div></div>
      <div class="stat"><div class="val {profit_color}">${profit}</div><div class="lbl">Profit</div></div>
    </div>
    {roi_note}
  </div>

  <!-- CLV Card -->
  <div class="card">
    <h2>Closing Line Value</h2>
    <div class="stat-row">
      <div class="stat"><div class="val {clv_color}">{clv_avg:+.3f}</div><div class="lbl">Avg CLV</div></div>
      <div class="stat"><div class="val">{clv_count}</div><div class="lbl">Матчей</div></div>
      <div class="stat"><div class="val">{clv_pct}%</div><div class="lbl">CLV &gt; 0</div></div>
    </div>
    <p style="color:#8b949e;font-size:11px;margin-top:8px">
      CLV &gt; 0 означает что рынок "догонял" нас — модель видит сигнал раньше рынка.
    </p>
  </div>

  <!-- Upcoming with edge -->
  <div class="card wide">
    <h2>Предстоящие матчи с edge</h2>
    {upcoming_table}
  </div>

  <!-- Top teams -->
  <div class="card">
    <h2>Топ-20 команд по Elo</h2>
    <table>
      <tr><th>#</th><th>Команда</th><th>Elo</th><th>Матчей</th></tr>
      {top_teams_rows}
    </table>
  </div>

  <!-- League breakdown -->
  <div class="card">
    <h2>Топ лиг в odds_snapshots</h2>
    <table>
      <tr><th>Лига</th><th>Snapshots</th></tr>
      {league_rows}
    </table>
  </div>

</div>
</body>
</html>"""


def render_upcoming(conn, elo, games, use_live: bool) -> str:
    if not use_live:
        return "<p style='color:#8b949e'>Live запросы отключены (--no-live)</p>"

    try:
        from adapters.betsapi import BetsAPIClient, _extract_moneyline, _is_dota2
        import re

        def norm(s): return re.sub(r"[^a-z0-9]", "", s.lower())
        def find(name):
            n = norm(name)
            for k in elo:
                if norm(k) == n: return k
            for k in elo:
                nk = norm(k)
                if n in nk or nk in n: return k
            return None

        client = BetsAPIClient()
        events = client.get_upcoming_dota2()
    except Exception as e:
        return f"<p style='color:#f85149'>BetsAPI error: {e}</p>"

    if not events:
        return "<p style='color:#8b949e'>Нет предстоящих матчей</p>"

    rows_html = ""
    for event in events:
        home = event.get("home", {}).get("name", "")
        away = event.get("away", {}).get("name", "")
        ts   = event.get("time", "")
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            time_str = dt.strftime("%m-%d %H:%M")
        except Exception:
            time_str = "?"

        t1k = find(home); t2k = find(away)
        if t1k and t2k:
            e1 = elo.get(t1k, START_ELO); e2 = elo.get(t2k, START_ELO)
            prob = 1.0 / (1.0 + pow(10.0, (e2 - e1) / 400.0))
            g = min(games.get(t1k, 0), games.get(t2k, 0))
        else:
            prob = 0.5; g = 0

        try:
            summary = client.get_odds_summary(str(event.get("id", "")))
            bms = _extract_moneyline(summary)
        except Exception:
            bms = []

        if bms:
            chosen = next((b for b in bms if b["bookmaker"] == "Bet365"), bms[0])
            oh, oa = chosen["close_home"], chosen["close_away"]
            ih = 1/oh; ia = 1/oa; tot = ih + ia
            mkt = round(ih / tot, 3)
            edge = round(prob - mkt, 3)
            bm_name = chosen["bookmaker"]
            odds_str = f"{oh:.2f}"
        else:
            mkt = None; edge = None; bm_name = "-"; odds_str = "-"

        if edge is not None and edge >= 0.05:
            edge_td = f'<td class="pos star">★ +{edge:.3f}</td>'
        elif edge is not None and edge > 0:
            edge_td = f'<td class="pos">+{edge:.3f}</td>'
        elif edge is not None:
            edge_td = f'<td class="neg">{edge:.3f}</td>'
        else:
            edge_td = '<td class="neu">?</td>'

        mkt_str  = f"{mkt:.3f}" if mkt else "?"
        prob_str = f"{prob:.3f}" if g >= 5 else f"{prob:.3f} ⚠"

        rows_html += f"""<tr>
          <td>{time_str}</td>
          <td>{home}</td>
          <td>{away}</td>
          <td>{prob_str}</td>
          <td>{mkt_str}</td>
          {edge_td}
          <td>{odds_str}</td>
          <td style="color:#8b949e">{bm_name}</td>
        </tr>"""

    return f"""<table>
      <tr><th>Время</th><th>Home</th><th>Away</th><th>Model</th>
          <th>Market</th><th>Edge</th><th>Odds</th><th>BM</th></tr>
      {rows_html}
    </table>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open",    action="store_true", help="Открыть в браузере")
    parser.add_argument("--no-live", action="store_true", help="Без BetsAPI запросов")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("Строим Elo...", flush=True)
    elo, games = build_elo(conn)

    print("Собираем статистику...", flush=True)
    clv  = clv_stats(conn)
    roi  = roi_stats(conn)
    dbs  = db_summary(conn)

    top  = top_teams_data(elo, games, 20)
    top_rows = "".join(
        f"<tr><td style='color:#8b949e'>{i+1}</td>"
        f"<td>{t['team']}</td>"
        f"<td style='color:#58a6ff'>{t['elo']:.0f}</td>"
        f"<td style='color:#8b949e'>{t['games']}</td></tr>"
        for i, t in enumerate(top)
    )

    # League breakdown
    league_data = conn.execute("""
        SELECT league_name, COUNT(*) as cnt
        FROM odds_snapshots
        WHERE league_name IS NOT NULL
        GROUP BY league_name ORDER BY cnt DESC LIMIT 20
    """).fetchall()
    league_rows = "".join(
        f"<tr><td>{r[0]}</td><td style='color:#58a6ff'>{r[1]}</td></tr>"
        for r in league_data
    )

    print("Запрашиваем upcoming матчи...", flush=True)
    upcoming_html = render_upcoming(conn, elo, games, not args.no_live)
    conn.close()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ROI colour
    roi_v = roi["roi"]
    roi_color    = "pos" if roi_v > 0 else ("neg" if roi_v < 0 else "neu")
    profit_color = "pos" if roi["total_profit"] > 0 else "neg"
    roi_note = ("<p style='color:#8b949e;font-size:11px'>Нет завершённых paper ставок пока.</p>"
                if roi["count"] == 0 else "")

    # CLV colour
    clv_color = "pos" if clv["avg_clv"] > 0 else "neg"

    html = HTML_TEMPLATE.format(
        ts           = ts,
        db_matches   = dbs["matches"],
        db_snapshots = dbs["snapshots"],
        db_leagues   = dbs["leagues"],
        roi          = roi_v,
        roi_color    = roi_color,
        bet_count    = roi["count"],
        winrate      = roi["winrate"],
        profit       = roi["total_profit"],
        profit_color = profit_color,
        roi_note     = roi_note,
        clv_avg      = clv["avg_clv"],
        clv_color    = clv_color,
        clv_count    = clv["count"],
        clv_pct      = clv["pct_positive"],
        upcoming_table = upcoming_html,
        top_teams_rows = top_rows,
        league_rows    = league_rows,
    )

    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"✓ Dashboard: {OUT_PATH}")

    if args.open:
        subprocess.run(["open", str(OUT_PATH)])


if __name__ == "__main__":
    main()
