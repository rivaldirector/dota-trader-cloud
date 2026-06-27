#!/usr/bin/env python3
"""
generate_dashboard.py — генерирует статичный HTML-дэшборд с предиктами на
сегодня/ближайшие 48ч + прогрессом по гейту Rule C (M05) + общим P&L.

Пишет файл в корень проекта: dashboard.html
Открывается просто двойным кликом / как закладка в браузере — никаких
скриптов запускать не нужно. Перегенерируется автоматически каждые 6ч
через daily_paper_cycle.sh (launchd).
"""
from __future__ import annotations
import sqlite3, os, datetime, html, json, re

_DIR       = os.path.dirname(os.path.abspath(__file__))
HARVEST_DB = os.path.join(_DIR, '../storage/betsapi_harvest.db')
PAPER_DB   = os.path.join(_DIR, '../data/paper_trading.db')
OUT_HTML   = os.path.join(_DIR, '../dashboard.html')
PS_CACHE_PATH = os.path.join(_DIR, '../storage/pandascore_schedule_cache.json')
PS_HISTORY_DB = os.path.join(_DIR, '../data/pandascore_history.db')
BACKTEST_JSON_PATH = os.path.join(_DIR, '../data/elo_pure_60d_backtest.json')

HORIZON_H    = 72          # показываем предикты на ближайшие N часов (зафиксировано: всегда 72ч вперёд)
GATE_TARGET  = 30
K_FACTOR     = 32.0
START_ELO    = 1000.0
STARTING_BANK = 1000.0     # виртуальный банк живого (не demo) paper-trading
LIVE_SINCE   = 1781654400  # TEST_END+1 — граница между demo-backtest и реальными live-сигналами
STAKE_FLAT   = 20.0        # flat-стейк на 1 сигнал (см. paper_trading.py)

def fmt_pct(x):
    return f"{x*100:+.1f}%" if x is not None else "—"

def fmt_dt(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M UTC')

def parse_score(s):
    if not s: return None, None
    for sep in ('-', ':'):
        if sep in str(s):
            p = str(s).split(sep)
            try: return int(p[0]), int(p[1])
            except Exception: pass
    return None, None

def novig(oh, oa):
    if not oh or not oa or oh <= 1 or oa <= 1:
        return None, None
    rh, ra = 1/oh, 1/oa
    t = rh + ra
    return rh/t, ra/t

def elo_exp(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

def normalize_team(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())

def load_pandascore_cache():
    """Расписание из PandaScore (storage/pandascore_schedule_cache.json) —
    закрывает дыру в покрытии BetsAPI (см. fetch_pandascore_schedule.py).
    Если кэш не существует/устарел — просто не используется, не ошибка."""
    if not os.path.exists(PS_CACHE_PATH):
        return [], None
    try:
        with open(PS_CACHE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return data.get('matches', []), data.get('fetched_at')
    except Exception:
        return [], None


def build_elo_schedule_forecast():
    """Полностью локальный Elo-прогноз по ВСЕМ матчам расписания —
    не требует живого API, только то, что уже в betsapi_harvest.db.
    Возвращает список матчей с elo_prob_home/away и, если есть кэш
    одсов, дополнительно market_prob + edge."""
    if not os.path.exists(HARVEST_DB):
        return []

    hcon = sqlite3.connect(HARVEST_DB, timeout=10)

    # ── История для Elo: BetsAPI (betsapi_harvest.db) + PandaScore (отдельная
    # data/pandascore_history.db, см. fetch_pandascore_history.py) — мёрджим
    # и дедуплицируем, т.к. BetsAPI оказался сильно недообсчитан по части лиг
    # (TI Quals, EPL и т.п. — см. check_pandascore_coverage.py). PandaScore НЕ
    # трогает betsapi_harvest.db, это чисто read-only merge in-memory.
    history = []  # (start_time, home, away, act_h)  act_h: 1.0/0.0/0.5
    seen_hist_keys = set()

    for home, away, score, st in hcon.execute("""
        SELECT home_team, away_team, score, CAST(start_time AS INTEGER)
        FROM raw_events
        WHERE league LIKE 'DOTA2%' AND status='ended'
          AND score IS NOT NULL AND score != ''
    """).fetchall():
        sh, sa = parse_score(score)
        if sh is None:
            continue
        act_h = 1.0 if sh > sa else (0.0 if sa > sh else 0.5)
        key = (normalize_team(home), normalize_team(away), st // 3600)
        if key in seen_hist_keys:
            continue
        seen_hist_keys.add(key)
        history.append((st, home, away, act_h))

    ps_hist_added = 0
    if os.path.exists(PS_HISTORY_DB):
        pscon = sqlite3.connect(PS_HISTORY_DB, timeout=10)
        for home, away, st, winner in pscon.execute("""
            SELECT home_team, away_team, start_time, winner FROM ps_matches
            WHERE winner IS NOT NULL
        """).fetchall():
            key = (normalize_team(home), normalize_team(away), st // 3600)
            if key in seen_hist_keys:
                continue  # уже есть из BetsAPI — не дублируем
            nw = normalize_team(winner)
            if nw == normalize_team(home):
                act_h = 1.0
            elif nw == normalize_team(away):
                act_h = 0.0
            else:
                continue  # не смогли определить сторону — пропускаем
            seen_hist_keys.add(key)
            history.append((st, home, away, act_h))
            ps_hist_added += 1
        pscon.close()

    history.sort(key=lambda r: r[0])  # хронологически — обязательно для Elo

    # Rolling Elo по объединённой истории (chronological, no leakage)
    elo = {}
    for st, home, away, act_h in history:
        eh, ea = elo.get(home, START_ELO), elo.get(away, START_ELO)
        exp_h = elo_exp(eh, ea)
        elo[home] = eh + K_FACTOR * (act_h - exp_h)
        elo[away] = ea + K_FACTOR * ((1 - act_h) - (1 - exp_h))

    # Все матчи расписания (upcoming) — независимо от наличия одсов
    matches = hcon.execute("""
        SELECT event_id, home_team, away_team, league,
               CAST(start_time AS INTEGER)
        FROM raw_events
        WHERE league LIKE 'DOTA2%' AND status='upcoming'
        ORDER BY CAST(start_time AS INTEGER) ASC
    """).fetchall()

    seen_keys = set()
    for eid, home, away, league, st in matches:
        seen_keys.add((normalize_team(home), normalize_team(away), st // 3600))

    # ── Добор расписания из PandaScore-кэша (закрывает дыру в BetsAPI) ─────────
    ps_matches, ps_fetched_at = load_pandascore_cache()
    ps_added = 0
    for pm in ps_matches:
        if pm.get('status') not in ('not_started', 'running'):
            continue
        key = (normalize_team(pm['home_team']), normalize_team(pm['away_team']),
               pm['start_time'] // 3600)
        if key in seen_keys:
            continue  # уже есть из BetsAPI — не дублируем
        seen_keys.add(key)
        matches.append((f"ps_{pm['ps_id']}", pm['home_team'], pm['away_team'],
                        pm['league'], pm['start_time']))
        ps_added += 1
    matches.sort(key=lambda r: r[4])

    out = []
    out_meta = {'ps_added': ps_added, 'ps_fetched_at': ps_fetched_at,
                'ps_total': len(ps_matches)}
    for eid, home, away, league, st in matches:
        eh, ea = elo.get(home, START_ELO), elo.get(away, START_ELO)
        elo_diff = eh - ea
        ep_h = elo_exp(eh, ea)

        bms = hcon.execute("""
            SELECT bookmaker, open_home, open_away FROM odds_summary
            WHERE event_id=? AND market='151_1' AND open_home>1 AND open_away>1
        """, (eid,)).fetchall()

        best = None
        for bm, oh, oa in bms:
            mh, ma = novig(oh, oa)
            if mh is None:
                continue
            edge_h = ep_h - mh
            cand = {'bm': bm, 'oh': oh, 'oa': oa, 'mh': mh, 'ma': ma, 'edge_h': edge_h}
            if best is None or abs(edge_h) > abs(best['edge_h']):
                best = cand

        out.append({
            'event_id': eid, 'league': league, 'home': home, 'away': away,
            'start_time': st, 'elo_home': eh, 'elo_away': ea, 'elo_diff': elo_diff,
            'elo_prob_home': ep_h, 'odds': best,
        })

    hcon.close()
    return out, out_meta

def main():
    if not os.path.exists(PAPER_DB):
        print("paper_trading.db не найдена — нечего генерировать.")
        return

    con = sqlite3.connect(PAPER_DB, timeout=10)
    now = int(datetime.datetime.utcnow().timestamp())
    horizon = now + HORIZON_H * 3600

    rows = con.execute("""
        SELECT strategy_name, division, bookmaker, league, home_team, away_team,
               start_time, event_id, bet_team, odds, market_prob, model_prob, edge
        FROM paper_bets
        WHERE settled = 0 AND start_time BETWEEN ? AND ?
        ORDER BY start_time ASC
    """, (now - 3600, horizon)).fetchall()

    # Группируем по (event_id, bet_team) — собираем список стратегий вместе
    grouped = {}
    for (strat, div, bm, league, home, away, st, eid, bet_team, odds,
         mkt_p, model_p, edge) in rows:
        key = (eid, bet_team)
        if key not in grouped:
            grouped[key] = {
                'league': league, 'home': home, 'away': away, 'start_time': st,
                'bet_team': bet_team, 'best_odds': odds, 'best_bm': bm,
                'mkt_p': mkt_p, 'model_p': model_p, 'edge': edge,
                'strats': set(),
            }
        g = grouped[key]
        g['strats'].add(strat)
        if odds > g['best_odds']:
            g['best_odds'] = odds
            g['best_bm'] = bm

    preds = sorted(grouped.values(), key=lambda g: g['start_time'])

    # Гейт Rule C (M05)
    m05_settled = con.execute(
        "SELECT COUNT(*) FROM paper_bets WHERE strategy_name='M05' AND settled=1"
    ).fetchone()[0] or 0
    m05_pending = con.execute(
        "SELECT COUNT(*) FROM paper_bets WHERE strategy_name='M05' AND settled=0"
    ).fetchone()[0] or 0

    # ── Виртуальный банк (только LIVE-сигналы, без demo-backtest периода) ──────
    today_str = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    week_ago_ts = now - 7 * 86400

    live_pnl_total = con.execute(
        "SELECT SUM(pnl) FROM paper_bets WHERE settled=1 AND start_time>=?",
        (LIVE_SINCE,)
    ).fetchone()[0] or 0.0
    virtual_bank = STARTING_BANK + live_pnl_total

    live_pnl_week = con.execute(
        "SELECT SUM(pnl) FROM paper_bets WHERE settled=1 AND start_time>=? "
        "AND settled_ts >= ?",
        (LIVE_SINCE, datetime.datetime.utcfromtimestamp(week_ago_ts).isoformat())
    ).fetchone()[0] or 0.0

    live_pnl_today = con.execute(
        "SELECT SUM(pnl) FROM paper_bets WHERE settled=1 AND start_time>=? "
        "AND settled_ts LIKE ?",
        (LIVE_SINCE, today_str + '%')
    ).fetchone()[0] or 0.0

    live_open_count, live_open_stake = con.execute(
        "SELECT COUNT(*), SUM(stake_usd) FROM paper_bets "
        "WHERE settled=0 AND start_time>=?", (LIVE_SINCE,)
    ).fetchone()
    live_open_count = live_open_count or 0
    live_open_stake = live_open_stake or 0.0

    # Ставки, размещённые СЕГОДНЯ (по дате run_ts, не по дате матча)
    today_placed_rows = con.execute(
        "SELECT strategy_name, division, home_team, away_team, bet_team, "
        "odds, edge, stake_usd, start_time, settled, outcome, pnl, league "
        "FROM paper_bets WHERE start_time>=? AND run_ts LIKE ? "
        "ORDER BY start_time",
        (LIVE_SINCE, today_str + '%')
    ).fetchall()

    today_grouped = {}
    for (strat, div, home, away, bet_team, odds, edge, stake, st,
         settled_f, outcome, pnl, league) in today_placed_rows:
        key = (home, away, bet_team)
        if key not in today_grouped:
            today_grouped[key] = {
                'home': home, 'away': away, 'bet_team': bet_team,
                'odds': odds, 'edge': edge, 'start_time': st, 'league': league,
                'n_strats': 0, 'total_stake': 0.0, 'settled': settled_f,
                'outcome': outcome, 'pnl': 0.0,
            }
        g = today_grouped[key]
        g['n_strats'] += 1
        g['total_stake'] += stake
        # pnl/outcome/settled накапливаем по группе — раньше брался только
        # первый встреченный bet, что давало в N раз меньшую цифру, чем
        # реальный суммарный P&L по всем стратегиям, поставившим на этот матч
        g['settled'] = settled_f
        g['outcome'] = outcome
        g['pnl'] += (pnl or 0.0)

    today_placed = sorted(today_grouped.values(), key=lambda g: g['start_time'])

    # Общая сводка по settled-ставкам (все стратегии)
    total, settled, wins, losses = con.execute("""
        SELECT COUNT(*), SUM(settled),
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END),
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)
        FROM paper_bets
    """).fetchone()
    gross = con.execute("SELECT SUM(pnl) FROM paper_bets WHERE settled=1").fetchone()[0] or 0
    stake = con.execute(
        "SELECT SUM(stake_usd) FROM paper_bets WHERE settled=1 AND outcome!='void'"
    ).fetchone()[0] or 1
    wr = wins/(wins+losses)*100 if wins and (wins+losses) > 0 else 0
    roi = gross/stake*100 if stake else 0

    # Топ-10 стратегий по P&L (settled)
    top_strats = con.execute("""
        SELECT strategy_name, division, COUNT(*) bets,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) w,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) l,
               ROUND(SUM(pnl),2) pnl
        FROM paper_bets WHERE settled=1
        GROUP BY strategy_name, division
        ORDER BY pnl DESC LIMIT 10
    """).fetchall()

    con.close()

    elo_forecast, elo_meta = build_elo_schedule_forecast()

    # Историческая симуляция "чистый Elo, flat $20" за 60 дней
    # (см. scripts/backtest_elo_pure_60d.py — отдельная разовая выгрузка JSON,
    # не пересчитывается на каждом запуске дэшборда, чтобы не дублировать
    # дорогой full-history пересчёт; обновляется по запуску бэктест-скрипта).
    elo_backtest = None
    if os.path.exists(BACKTEST_JSON_PATH):
        try:
            with open(BACKTEST_JSON_PATH, encoding='utf-8') as f:
                elo_backtest = json.load(f)
        except Exception:
            elo_backtest = None

    today_html = ""
    if today_placed:
        for g in today_placed:
            dt = fmt_dt(g['start_time'])
            if g['settled']:
                status = (f"<span class='pos'>выиграли (+${g['pnl']:.2f})</span>"
                           if g['outcome'] == 'win'
                           else f"<span class='neg'>проиграли (${g['pnl']:.2f})</span>")
            else:
                status = '<span class="muted2">ожидаем результат</span>'
            today_html += f"""
            <tr>
              <td>{html.escape(dt)}</td>
              <td>{html.escape(g['league'] or '')}</td>
              <td>{html.escape(g['home'])} vs {html.escape(g['away'])}</td>
              <td class="bet">{html.escape(g['bet_team'])}</td>
              <td>{g['odds']:.3f}</td>
              <td class="{'pos' if g['edge']>0 else 'neg'}">{fmt_pct(g['edge'])}</td>
              <td>{g['n_strats']}</td>
              <td>${g['total_stake']:.0f}</td>
              <td>{status}</td>
            </tr>"""
    else:
        today_html = '<tr><td colspan="9" class="empty">Сегодня ставок ещё не размещали</td></tr>'

    gen_time = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    # ── HTML ────────────────────────────────────────────────────────────────
    rows_html = ""
    if preds:
        for g in preds:
            dt = fmt_dt(g['start_time'])
            strats = ', '.join(sorted(g['strats']))
            rows_html += f"""
            <tr>
              <td>{html.escape(dt)}</td>
              <td>{html.escape(g['league'] or '')}</td>
              <td>{html.escape(g['home'])} vs {html.escape(g['away'])}</td>
              <td class="bet">{html.escape(g['bet_team'])}</td>
              <td>{g['best_odds']:.3f}</td>
              <td>{fmt_pct(g['mkt_p'])}</td>
              <td>{fmt_pct(g['model_p'])}</td>
              <td class="{'pos' if g['edge']>0 else 'neg'}">{fmt_pct(g['edge'])}</td>
              <td>{html.escape(strats)}</td>
              <td>{html.escape(g['best_bm'])}</td>
            </tr>"""
    else:
        rows_html = '<tr><td colspan="10" class="empty">Нет сигналов на ближайшие 48ч</td></tr>'

    gate_pct = min(100, round(m05_settled / GATE_TARGET * 100))
    gate_done = m05_settled >= GATE_TARGET

    elo_html = ""
    if elo_forecast:
        for m in elo_forecast:
            dt = fmt_dt(m['start_time'])
            fav = m['home'] if m['elo_prob_home'] >= 0.5 else m['away']
            fav_p = m['elo_prob_home'] if m['elo_prob_home'] >= 0.5 else (1 - m['elo_prob_home'])
            if m['odds']:
                o = m['odds']
                odds_cell = f"{o['oh']:.3f} / {o['oa']:.3f} ({html.escape(o['bm'])})"
                edge_cell = f"<span class=\"{'pos' if o['edge_h']>0 else 'neg'}\">{fmt_pct(o['edge_h'])}</span>"
            else:
                odds_cell = '<span class="muted2">нет кэша одсов</span>'
                edge_cell = '—'
            elo_html += f"""
            <tr>
              <td>{html.escape(dt)}</td>
              <td>{html.escape(m['league'] or '')}</td>
              <td>{html.escape(m['home'])} vs {html.escape(m['away'])}</td>
              <td>{m['elo_home']:.0f} / {m['elo_away']:.0f} ({m['elo_diff']:+.0f})</td>
              <td class="bet">{html.escape(fav)} {fav_p*100:.0f}%</td>
              <td>{odds_cell}</td>
              <td>{edge_cell}</td>
            </tr>"""
    else:
        elo_html = '<tr><td colspan="7" class="empty">Нет матчей в расписании (upcoming) в harvest DB</td></tr>'

    elo_backtest_html = ""
    if elo_backtest:
        bt = elo_backtest
        pnl_cls = 'pos' if (bt.get('total_pnl') or 0) >= 0 else 'neg'
        gen_dt = bt.get('generated_at', '')[:16].replace('T', ' ')
        elo_backtest_html = f"""
  <div class="card">
    <h2>Историческая симуляция: чистый Elo, последние {bt['window_days']} дней</h2>
    <div class="stats">
      <div class="stat"><div class="val">{bt['n_bets']}</div><div class="lbl">ставок (flat ${bt['stake_flat']:.0f}, только где были реальные котировки)</div></div>
      <div class="stat"><div class="val">{bt['winrate_pct']:.2f}%</div><div class="lbl">win rate ({bt['n_wins']}/{bt['n_bets']})</div></div>
      <div class="stat"><div class="val {pnl_cls}">${bt['total_pnl']:+,.2f}</div><div class="lbl">P&amp;L (поставлено ${bt['total_staked']:,.0f})</div></div>
      <div class="stat"><div class="val {pnl_cls}">{bt['roi_pct']:+.2f}%</div><div class="lbl">ROI</div></div>
      <div class="stat"><div class="val">${bt['starting_bank']:,.0f} → ${bt['final_bank']:,.2f}</div><div class="lbl">гипотетический банк</div></div>
    </div>
    <div class="note">
      Гипотетика: если бы мы ВСЕГДА ставили flat ${bt['stake_flat']:.0f} на Elo-фаворита (без Rule C / H2H / edge-фильтров),
      используя Elo, обновлённый объединённой историей BetsAPI + PandaScore (см. scripts/compute_elo_winrate.py,
      scripts/backtest_elo_pure_60d.py). Это НЕ то же самое, что реальный paper-трейдинг по 35 стратегиям выше —
      отдельный пример того, на что способна чистая Elo-модель сама по себе. Снимок от {gen_dt} UTC,
      обновляется запуском backtest_elo_pure_60d.py.
    </div>
  </div>"""

    top_html = ""
    for name, div, bets, w, l, pnl in top_strats:
        wr_s = w/(w+l)*100 if (w+l) > 0 else 0
        cls = 'pos' if pnl > 0 else 'neg'
        top_html += f"""
        <tr>
          <td>{html.escape(name)}</td><td>{html.escape(div)}</td>
          <td>{bets}</td><td>{wr_s:.1f}%</td>
          <td class="{cls}">${pnl:+.2f}</td>
        </tr>"""

    out = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="1800">
<title>Dota2 Paper-Trading Dashboard</title>
<style>
  :root {{
    --bg: #0f1115; --card: #171a21; --border: #2a2e38;
    --text: #e6e8eb; --muted: #8a8f98;
    --pos: #3ddc84; --neg: #ff5c5c; --accent: #5b8cff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 32px;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .updated {{ color: var(--muted); font-size: 13px; margin-bottom: 28px; }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 24px; margin-bottom: 24px;
  }}
  .card h2 {{ font-size: 15px; margin: 0 0 16px; color: var(--muted);
              text-transform: uppercase; letter-spacing: .04em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th {{ text-align: left; color: var(--muted); font-weight: 500;
        padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  .bet {{ font-weight: 600; color: var(--accent); }}
  .pos {{ color: var(--pos); font-weight: 600; }}
  .neg {{ color: var(--neg); font-weight: 600; }}
  .empty {{ text-align: center; color: var(--muted); padding: 24px; }}
  .muted2 {{ color: var(--muted); font-style: italic; }}
  .note {{ color: var(--muted); font-size: 12.5px; margin-top: 10px; }}
  .stats {{ display: flex; gap: 28px; flex-wrap: wrap; }}
  .stat {{ min-width: 120px; }}
  .stat .val {{ font-size: 24px; font-weight: 700; }}
  .stat .lbl {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .gate-bar {{ background: var(--border); border-radius: 6px; height: 10px;
               overflow: hidden; margin-top: 10px; }}
  .gate-fill {{ background: {'var(--pos)' if gate_done else 'var(--accent)'};
                height: 100%; width: {gate_pct}%; }}
</style>
</head>
<body>
  <h1>🎮 Dota 2 — Paper Trading Dashboard</h1>
  <div class="updated">Обновлено: {gen_time} &nbsp;·&nbsp; авто-обновление страницы каждые 30 мин &nbsp;·&nbsp; данные перегенерируются launchd каждые 6ч</div>

  <div class="card">
    <h2>Предикты — ближайшие {HORIZON_H}ч</h2>
    <table>
      <tr>
        <th>Время</th><th>Лига</th><th>Матч</th><th>Ставить</th>
        <th>Odds</th><th>Mkt%</th><th>Model%</th><th>Edge</th>
        <th>Стратегии</th><th>BK</th>
      </tr>
      {rows_html}
    </table>
  </div>

  <div class="card">
    <h2>Виртуальный банк (live, без demo-периода)</h2>
    <div class="stats">
      <div class="stat"><div class="val">${virtual_bank:,.2f}</div><div class="lbl">текущий банк (старт ${STARTING_BANK:,.0f})</div></div>
      <div class="stat"><div class="val {'pos' if live_pnl_week>=0 else 'neg'}">${live_pnl_week:+,.2f}</div><div class="lbl">изменение за неделю</div></div>
      <div class="stat"><div class="val {'pos' if live_pnl_today>=0 else 'neg'}">${live_pnl_today:+,.2f}</div><div class="lbl">изменение за сегодня</div></div>
      <div class="stat"><div class="val">${STAKE_FLAT:.0f}</div><div class="lbl">стейк на 1 сигнал</div></div>
      <div class="stat"><div class="val">{live_open_count}</div><div class="lbl">открытых ставок (${live_open_stake:,.0f} экспозиция)</div></div>
    </div>
    <div class="note">Банк считается только по LIVE-сигналам (матчи с {datetime.datetime.utcfromtimestamp(LIVE_SINCE).strftime('%Y-%m-%d')} и позже) — старый demo-backtest на TEST-периоде (Jan-Jun 2026) не входит в этот баланс. Каждый сигнал каждой стратегии — это отдельная виртуальная ставка по ${STAKE_FLAT:.0f}, поэтому на одном матче может стоять сразу несколько ставок.</div>
  </div>

  <div class="card">
    <h2>Ставки, размещённые сегодня ({today_str})</h2>
    <table>
      <tr>
        <th>Время матча</th><th>Лига</th><th>Матч</th><th>Ставили на</th>
        <th>Odds</th><th>Edge</th><th>#Стратегий</th><th>Всего $</th><th>Статус</th>
      </tr>
      {today_html}
    </table>
  </div>

  <div class="card">
    <h2>Elo-прогноз по всему расписанию (не требует живого API)</h2>
    <table>
      <tr>
        <th>Время</th><th>Лига</th><th>Матч</th><th>Elo (Δ)</th>
        <th>Фаворит по Elo</th><th>Кэш одсов</th><th>Edge</th>
      </tr>
      {elo_html}
    </table>
    <div class="note">
      Elo считается из истории матчей в harvest DB (BetsAPI). Расписание добрано из PandaScore
      (+{elo_meta['ps_added']} матчей из {elo_meta['ps_total']} в кэше, обновлён: {elo_meta['ps_fetched_at'] or 'кэш отсутствует — запусти scripts/fetch_pandascore_schedule.py'}) —
      закрывает дыру в покрытии BetsAPI. Edge доступен только там, где есть кэшированные одсы (PandaScore коэффициентов не даёт).
    </div>
  </div>

  {elo_backtest_html}

  <div class="card">
    <h2>Гейт Rule C (M05) — следующий этап исследований</h2>
    <div class="stats">
      <div class="stat"><div class="val">{m05_settled}/{GATE_TARGET}</div><div class="lbl">settled signals</div></div>
      <div class="stat"><div class="val">{m05_pending}</div><div class="lbl">в ожидании результата</div></div>
    </div>
    <div class="gate-bar"><div class="gate-fill"></div></div>
  </div>

  <div class="card">
    <h2>Общий paper P&amp;L (все стратегии, settled)</h2>
    <div class="stats">
      <div class="stat"><div class="val">{settled or 0:,}</div><div class="lbl">ставок урегулировано</div></div>
      <div class="stat"><div class="val">{wr:.1f}%</div><div class="lbl">win rate</div></div>
      <div class="stat"><div class="val {'pos' if gross>=0 else 'neg'}">${gross:+,.2f}</div><div class="lbl">gross P&amp;L</div></div>
      <div class="stat"><div class="val {'pos' if roi>=0 else 'neg'}">{roi:+.1f}%</div><div class="lbl">ROI</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Топ-10 стратегий по P&amp;L</h2>
    <table>
      <tr><th>Стратегия</th><th>Div</th><th>Ставок</th><th>Win%</th><th>P&amp;L</th></tr>
      {top_html}
    </table>
  </div>

</body>
</html>"""

    with open(OUT_HTML, 'w', encoding='utf-8') as f:
        f.write(out)
    print(f"Дэшборд обновлён: {OUT_HTML}")


if __name__ == '__main__':
    main()
