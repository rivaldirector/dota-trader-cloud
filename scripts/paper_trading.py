#!/usr/bin/env python3
"""
paper_trading.py  —  Бумажный турнир для 35 стабильных стратегий.

WORKFLOW (строго слепой — результаты матчей не видны при ставках):

  Шаг 1: python3 scripts/paper_trading.py --mode bet
          → Ставки на новые матчи.  Результаты НЕ смотрим.

  Шаг 2: python3 scripts/paper_trading.py --mode settle
          → Подтягиваем результаты завершённых матчей, считаем P&L.

  Шаг 3: python3 scripts/paper_trading.py --mode report
          → Лидерборд по стратегиям.

Параметры:
  --since  TIMESTAMP  Начало периода (default: TEST_END+1 = 1781654400)
  --demo              Демо-режим: ставим на TEST-период (с 2026-01-01)
"""
from __future__ import annotations
import sqlite3, os, argparse, datetime, sys

_DIR       = os.path.dirname(os.path.abspath(__file__))
HARVEST_DB = os.path.join(_DIR, '../storage/betsapi_harvest.db')
TOURN_DB   = os.path.join(_DIR, '../data/model_tournament.db')
PAPER_DB   = os.path.join(_DIR, '../data/paper_trading.db')

TEST_END   = 1781654399   # 2026-06-15 23:59:59 UTC
VAL_END    = 1767225599   # 2025-12-31 UTC  (demo-старт для TEST-периода)
LIVE_SINCE = TEST_END + 1 # граница между demo-backtest и реальными live-сигналами
K_FACTOR   = 32.0
START_ELO  = 1000.0
STAKE_FLAT = 20.0

# ── Риск-контроль ────────────────────────────────────────────────────────────
# Без этого лимита N коррелированных стратегий (Elo50/75/100 и т.п. — по сути
# одна и та же идея с разными порогами) могут синхронно поставить на ОДНУ
# сторону одного матча по $20 каждая. На практике уже бывало 15-18 стратегий
# сразу — то есть $300-360 экспозиции на одном исходе одного матча. Два таких
# матча, оба "против" — банк уничтожен за день. MAX_MATCH_STAKE ограничивает
# суммарную ставку на (матч, сторона) независимо от того, сколько стратегий
# совпали — это risk-management слой, НЕ изменение логики/фильтров стратегий.
MAX_MATCH_STAKE = 100.0   # = 5 стратегий по $20 максимум на один исход
STARTING_BANK   = 1000.0  # см. generate_dashboard.py — единая точка отсчёта банка
MAX_DAILY_STAKE_PCT = 0.25  # максимум 25% ТЕКУЩЕГО банка новых ставок за 1 календарный день размещения

PREFERRED_BM = ['PinnacleSports', 'Bet365', 'GGBet', 'MelBet', 'YSB88']

# ── Schema ────────────────────────────────────────────────────────────────────

PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_bets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts         TEXT NOT NULL,
    strategy_name  TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    division       TEXT NOT NULL,
    league         TEXT,
    home_team      TEXT,
    away_team      TEXT,
    start_time     INTEGER,
    bookmaker      TEXT,
    bet_team       TEXT NOT NULL,
    odds           REAL,
    market_prob    REAL,
    model_prob     REAL,
    edge           REAL,
    stake_usd      REAL DEFAULT 20.0,
    settled        INTEGER DEFAULT 0,
    outcome        TEXT,
    pnl            REAL,
    settled_ts     TEXT,
    UNIQUE(strategy_name, event_id, division)
);

CREATE TABLE IF NOT EXISTS paper_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def novig(h, a):
    if not h or not a or h <= 1 or a <= 1:
        return None, None
    rh, ra = 1/h, 1/a
    t = rh + ra
    return rh/t, ra/t

def elo_exp(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

def derive_winner(home, away, winner_col, score_col):
    if winner_col and str(winner_col).strip() not in ('draw', '', 'null', 'None'):
        wc = str(winner_col).strip()
        if wc == home: return 'home'
        if wc == away: return 'away'
    if score_col:
        s = str(score_col).strip().lower()
        if s in ('home', 'away'): return s
        if s in ('', '0-0', '1-1', '2-2', 'draw', 'null'): return None
        parts = s.split('-')
        if len(parts) == 2:
            try:
                h, a = int(parts[0]), int(parts[1])
                if h > a: return 'home'
                if a > h: return 'away'
            except: pass
    return None


class RollingH2H:
    def __init__(self): self._d = {}

    def _k(self, a, b): return tuple(sorted([a, b]))

    def get(self, home, away):
        d = self._d.get(self._k(home, away))
        if not d or d['n'] == 0: return 0, 0.5, 0.5, 0.0
        k = self._k(home, away)
        wh = d['w0'] if k[0] == home else d['w1']
        wa = d['w1'] if k[0] == home else d['w0']
        n = d['n']
        return n, wh/n, wa/n, wh/n - wa/n

    def update(self, home, away, winner):
        k = self._k(home, away)
        if k not in self._d: self._d[k] = {'w0': 0, 'w1': 0, 'n': 0}
        d = self._d[k]
        d['n'] += 1
        if (winner == 'home' and k[0] == home) or (winner == 'away' and k[0] == away):
            d['w0'] += 1
        else:
            d['w1'] += 1


def current_virtual_bank(pcon, live_since):
    """Текущий виртуальный банк = старт + realized P&L по settled LIVE-ставкам."""
    pnl = pcon.execute(
        "SELECT SUM(pnl) FROM paper_bets WHERE settled=1 AND start_time>=?",
        (live_since,)
    ).fetchone()[0] or 0.0
    return STARTING_BANK + pnl


def stake_placed_today(pcon, today_str):
    """Сколько $ уже поставлено СЕГОДНЯ (по дате run_ts) — для дневного лимита."""
    s = pcon.execute(
        "SELECT SUM(stake_usd) FROM paper_bets WHERE run_ts LIKE ?",
        (today_str + '%',)
    ).fetchone()[0]
    return s or 0.0


# ── Elo/H2H state ─────────────────────────────────────────────────────────────

def build_state(up_to_ts: int):
    """
    Строим rolling Elo и H2H по всем историческим матчам (start_time < up_to_ts).
    ТОЛЬКО завершённые матчи с известным результатом.
    """
    hcon = sqlite3.connect(HARVEST_DB)
    rows = hcon.execute("""
        SELECT re.home_team, re.away_team,
               CAST(re.start_time AS INTEGER),
               re.score, re.winner
        FROM raw_events re
        JOIN odds_summary os ON re.event_id = os.event_id
            AND os.market = '151_1'
            AND os.open_home > 1 AND os.open_away > 1
        WHERE re.league LIKE 'DOTA2%'
          AND re.status = 'ended'
          AND CAST(re.start_time AS INTEGER) < ?
        ORDER BY CAST(re.start_time AS INTEGER) ASC
    """, (up_to_ts,)).fetchall()
    hcon.close()

    elo, h2h = {}, RollingH2H()
    for home, away, st, score, winner_col in rows:
        winner = derive_winner(home, away, winner_col, score)
        eh, ea = elo.get(home, START_ELO), elo.get(away, START_ELO)
        if winner:
            exp = elo_exp(eh, ea)
            act = 1.0 if winner == 'home' else 0.0
            elo[home] = eh + K_FACTOR * (act - exp)
            elo[away] = ea + K_FACTOR * ((1 - act) - (1 - exp))
            h2h.update(home, away, winner)

    print(f"  Elo state: {len(elo)} команд из {len(rows):,} матчей", flush=True)
    return elo, h2h


# ── Load stable strategies ────────────────────────────────────────────────────

#  M05 = Rule C FROZEN — протокол требует копить settled-сигналы вживую
#  НЕЗАВИСИМО от VAL/TEST backtest-метрик (см. договорённость:
#  "Следующий этап исследований начинать только после появления
#   минимум 30 новых settled Rule C signals"). Поэтому M05 всегда
#  включён в paper-trading, даже если он не проходит динамический
#  фильтр стабильности ниже. M06/M36 — соседи из той же Rule-C
#  семьи, держим их рядом для сравнения, но они НЕ считаются к гейту.
ALWAYS_INCLUDE = {'M05', 'M06', 'M36'}


def load_stable_strategies():
    """Читает из tournament_metrics стабильные стратегии (VAL>0, TEST>15%)
    + принудительно включает ALWAYS_INCLUDE (Rule C family) независимо от фильтра."""
    if not os.path.exists(TOURN_DB):
        print("ОШИБКА: tournament_db не найдена. Запусти tournament_* скрипты сначала.")
        sys.exit(1)

    con = sqlite3.connect(TOURN_DB)
    codes = {r[0] for r in con.execute("""
        SELECT DISTINCT v.strategy_name
        FROM tournament_metrics v
        JOIN tournament_metrics t ON v.strategy_name = t.strategy_name
            AND v.division = t.division
        LEFT JOIN tournament_strategy_registry r ON v.strategy_name = r.strategy_name
        WHERE v.split = 'VAL' AND t.split = 'TEST'
          AND v.roi_pct > 0 AND t.roi_pct > 15 AND t.total_bets >= 10
          AND (r.is_oracle = 0 OR r.is_oracle IS NULL)
          AND (r.is_posthoc = 0 OR r.is_posthoc IS NULL)
    """).fetchall()}
    con.close()

    codes |= ALWAYS_INCLUDE

    sys.path.insert(0, os.path.join(_DIR, '..'))
    from scripts.tournament_run_strategies import STRATEGIES, SMETA, BlindMatch

    paper_strats = {k: v for k, v in STRATEGIES.items() if k in codes}
    if not paper_strats:
        print("ОШИБКА: Нет стабильных стратегий. Проверь tournament_metrics.")
        sys.exit(1)

    forced = sorted(ALWAYS_INCLUDE & paper_strats.keys())
    print(f"  Стратегий: {len(paper_strats)} (включая принудительно: {forced})", flush=True)
    return paper_strats, SMETA, BlindMatch


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: BET  —  слепые ставки (результаты НЕ запрашиваются)
# ══════════════════════════════════════════════════════════════════════════════

def mode_bet(since: int):
    print(f"\n{'═'*60}")
    print(f"PAPER BET  |  since={datetime.datetime.fromtimestamp(since).strftime('%Y-%m-%d')}")
    print(f"{'═'*60}\n")

    print("1. Загружаем стратегии...")
    paper_strats, smeta, BlindMatch = load_stable_strategies()

    print("2. Строим Elo/H2H state (хронологически, без результатов paper-матчей)...")
    elo, h2h = build_state(since)

    hcon = sqlite3.connect(HARVEST_DB)
    os.makedirs(os.path.dirname(PAPER_DB), exist_ok=True)
    pcon = sqlite3.connect(PAPER_DB)
    pcon.executescript(PAPER_SCHEMA)
    pcon.commit()

    # Уже обработанные event_id
    done = {r[0] for r in pcon.execute(
        "SELECT DISTINCT event_id FROM paper_bets"
    ).fetchall()}

    # ── Дневной лимит экспозиции = MAX_DAILY_STAKE_PCT от текущего банка ───────
    today_str = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    bank_now = current_virtual_bank(pcon, LIVE_SINCE)
    already_today = stake_placed_today(pcon, today_str)
    daily_cap = bank_now * MAX_DAILY_STAKE_PCT
    daily_remaining = max(0.0, daily_cap - already_today)
    print(f"   Банк сейчас: ${bank_now:,.2f} | дневной лимит: ${daily_cap:,.2f} "
          f"| уже поставлено сегодня: ${already_today:,.2f} | остаток: ${daily_remaining:,.2f}",
          flush=True)
    daily_used = 0.0

    print("3. Загружаем paper-матчи (без score/winner — BLIND)...", flush=True)
    # КРИТИЧНО: НЕ запрашиваем score, winner, close_*
    matches = hcon.execute("""
        SELECT DISTINCT re.event_id, re.home_team, re.away_team, re.league,
               CAST(re.start_time AS INTEGER)
        FROM raw_events re
        JOIN odds_summary os ON re.event_id = os.event_id
            AND os.market = '151_1'
            AND os.open_home > 1 AND os.open_away > 1
        WHERE re.league LIKE 'DOTA2%'
          AND CAST(re.start_time AS INTEGER) >= ?
        ORDER BY CAST(re.start_time AS INTEGER) ASC
    """, (since,)).fetchall()
    print(f"   Матчей найдено: {len(matches)}", flush=True)

    if not matches:
        print("\n⚠️  Нет новых матчей для ставок (возможно, harvest ещё не собрал данные).")
        print("   Запусти harvest и повтори --mode bet.")
        pcon.close(); hcon.close(); return

    # Открытые котировки всех матчей
    odds_map: dict[str, dict] = {}
    for eid, bm, oh, oa in hcon.execute("""
        SELECT event_id, bookmaker, open_home, open_away
        FROM odds_summary
        WHERE market = '151_1' AND open_home > 1 AND open_away > 1
    """).fetchall():
        odds_map.setdefault(str(eid), {})[bm] = (float(oh), float(oa))

    # Pre-match history
    hist_map: dict[str, list] = {}
    for eid, at, ho, ao in hcon.execute("""
        SELECT oh.event_id, oh.add_time, oh.home_od, oh.away_od
        FROM odds_history oh
        JOIN raw_events re ON re.event_id = oh.event_id
        WHERE re.league LIKE 'DOTA2%'
          AND oh.home_od > 1 AND oh.away_od > 1
        ORDER BY oh.event_id, CAST(oh.add_time AS INTEGER) ASC
    """).fetchall():
        if at:
            hist_map.setdefault(str(eid), []).append((int(at), float(ho), float(ao)))
    hcon.close()

    now_ts = datetime.datetime.utcnow().isoformat()
    total_bets, total_matches = 0, 0

    # ════════════════════════════════════════════════════════════════════════
    #  ПРОХОД 1 — собираем ВСЕ сигналы дня, ничего не пишем в БД.
    #  Это нужно, чтобы посчитать суммарный спрос на дневной бюджет ДО того,
    #  как начнём ставить — иначе матчи, что позже по времени, систематически
    #  обделены (chronological first-come-first-served), хотя сигнал у них
    #  может быть не хуже.
    # ════════════════════════════════════════════════════════════════════════
    print("4a. Собираем сигналы дня (без записи в БД)...", flush=True)
    decisions = []  # каждый элемент: dict со всеми полями для INSERT + group_key
    groups: dict[tuple, dict] = {}  # (eid, bet_team) -> {'n': int, 'natural_stake': float}

    for eid, home, away, league, st in matches:
        seid = str(eid)
        if seid in done:
            continue

        bm_odds = odds_map.get(seid, {})
        if not bm_odds:
            continue

        total_matches += 1

        eh = elo.get(home, START_ELO)
        ea = elo.get(away, START_ELO)
        ed = abs(eh - ea)
        ep_h = elo_exp(eh, ea)

        hn, wh, wa, hd = h2h.get(home, away)
        adj_h = (ep_h + wh) / 2.0 if hn >= 3 else ep_h

        hist = hist_map.get(seid, [])
        pre_pts = len(hist)
        latest_pre_prob = None
        if hist:
            lh, la = hist[-1][1], hist[-1][2]
            mh, _ = novig(lh, la)
            latest_pre_prob = mh

        bm_order = [b for b in PREFERRED_BM if b in bm_odds] + \
                   [b for b in bm_odds if b not in PREFERRED_BM]
        bm_a = next((b for b in bm_order if b != 'PinnacleSports'), None) or bm_order[0]
        bm_pin = 'PinnacleSports' if 'PinnacleSports' in bm_odds else None

        configs = []
        oh_a, oa_a = bm_odds.get(bm_a, (None, None))
        if oh_a:
            mh_a, ma_a = novig(oh_a, oa_a)
            if mh_a:
                configs.append(('A', bm_a, oh_a, oa_a, mh_a, ma_a))
        if bm_pin:
            oh_p, oa_p = bm_odds[bm_pin]
            mh_p, ma_p = novig(oh_p, oa_p)
            if mh_p:
                div_p = 'C' if pre_pts >= 5 else 'B'
                configs.append((div_p, bm_pin, oh_p, oa_p, mh_p, ma_p))

        for div, bm, oh, oa, mkt_h, mkt_a in configs:
            edge_h = ep_h - mkt_h
            m = BlindMatch(
                event_id=seid, match_date='', split='PAPER',
                division=div, league=league or '',
                home_team=home, away_team=away, bookmaker=bm,
                start_time=st,
                open_home=oh, open_away=oa,
                market_prob_home=mkt_h, market_prob_away=mkt_a,
                elo_home=eh, elo_away=ea, elo_diff=ed,
                elo_prob_home=ep_h, edge_home=edge_h,
                h2h_n=hn, h2h_wr_home=wh, h2h_wr_away=wa, h2h_delta=hd,
                adj_prob_home=adj_h,
                pre_match_pts=pre_pts, pre_match_move=None,
                latest_pre_prob=latest_pre_prob,
            )

            for name, func in paper_strats.items():
                d = func(m)
                if not d.bet:
                    continue
                gkey = (seid, d.bet_team)
                g = groups.setdefault(gkey, {'n': 0, 'natural_stake': 0.0})
                # Лимит $100 на (матч, сторону) применяется ЗДЕСЬ, на уровне
                # "сколько сигналов из этой группы вообще допускаем" —
                # дальше дневное масштабирование уменьшит это пропорционально,
                # а не отрежет хвост.
                if g['n'] * STAKE_FLAT >= MAX_MATCH_STAKE:
                    continue
                g['n'] += 1
                g['natural_stake'] = min(g['n'] * STAKE_FLAT, MAX_MATCH_STAKE)
                decisions.append({
                    'name': name, 'seid': seid, 'div': div, 'league': league,
                    'home': home, 'away': away, 'st': st, 'bm': bm,
                    'bet_team': d.bet_team, 'odds': d.odds,
                    'market_prob': d.market_prob, 'model_prob': d.model_prob,
                    'edge': d.edge, 'gkey': gkey,
                })

    # ════════════════════════════════════════════════════════════════════════
    #  ПРОХОД 2 — считаем суммарный спрос и общий масштаб на весь день.
    #  Если спрос больше остатка дневного лимита — ВСЕ группы пропорционально
    #  уменьшаются на один и тот же коэффициент, а не "ранние матчи забрали
    #  всё, поздним не хватило".
    # ════════════════════════════════════════════════════════════════════════
    total_natural_demand = sum(g['natural_stake'] for g in groups.values())
    if total_natural_demand <= daily_remaining or total_natural_demand == 0:
        scale = 1.0
    else:
        scale = daily_remaining / total_natural_demand

    print(f"   Сигналов: {len(decisions)} на {len(groups)} (матч,сторона)-групп | "
          f"natural demand: ${total_natural_demand:,.2f} | "
          f"остаток дневного лимита: ${daily_remaining:,.2f} | "
          f"масштаб: {scale*100:.1f}%", flush=True)
    if scale < 1.0:
        print(f"   ⚠ Спрос превысил дневной лимит — КАЖДЫЙ сигнал сегодня "
              f"уменьшен пропорционально до {scale*100:.1f}% от номинала "
              f"(вместо отсечения поздних матчей)", flush=True)

    # ════════════════════════════════════════════════════════════════════════
    #  ПРОХОД 3 — пишем в БД с финальным per-bet stake = group_natural_stake
    #  * scale / group_n (равная доля внутри группы).
    # ════════════════════════════════════════════════════════════════════════
    print("4b. Записываем ставки в БД...", flush=True)
    for dec in decisions:
        g = groups[dec['gkey']]
        final_group_stake = g['natural_stake'] * scale
        per_bet_stake = final_group_stake / g['n'] if g['n'] else 0.0
        if per_bet_stake <= 0:
            continue
        pcon.execute("""
            INSERT OR IGNORE INTO paper_bets
            (run_ts, strategy_name, event_id, division, league,
             home_team, away_team, start_time, bookmaker,
             bet_team, odds, market_prob, model_prob, edge, stake_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now_ts, dec['name'], dec['seid'], dec['div'], dec['league'],
              dec['home'], dec['away'], dec['st'], dec['bm'],
              dec['bet_team'], dec['odds'], dec['market_prob'], dec['model_prob'],
              dec['edge'], round(per_bet_stake, 4)))
        total_bets += 1
        daily_used += per_bet_stake

    pcon.execute("INSERT OR REPLACE INTO paper_meta VALUES ('last_bet_run',?)",
                 (now_ts,))
    pcon.execute("INSERT OR REPLACE INTO paper_meta VALUES ('since',?)",
                 (str(since),))
    pcon.commit()
    pcon.close()

    print(f"\n{'─'*50}")
    print(f"✅ Ставки размещены: {total_bets:,} на {total_matches:,} матчах")
    print(f"   Результаты НЕ были запрошены — ставки слепые.")
    print(f"   Запусти --mode settle когда матчи завершатся.")


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: SETTLE  —  расчёт результатов
# ══════════════════════════════════════════════════════════════════════════════

def mode_settle():
    print(f"\n{'═'*60}")
    print("PAPER SETTLE  |  подтягиваем результаты завершённых матчей")
    print(f"{'═'*60}\n")

    if not os.path.exists(PAPER_DB):
        print("Нет paper_trading.db. Сначала запусти --mode bet.")
        return

    pcon = sqlite3.connect(PAPER_DB)
    unsettled = pcon.execute("""
        SELECT DISTINCT event_id FROM paper_bets WHERE settled = 0
    """).fetchall()

    if not unsettled:
        print("Нет неурегулированных ставок.")
        pcon.close(); return

    eids = [r[0] for r in unsettled]
    print(f"  Неурегулированных event_id: {len(eids)}", flush=True)

    # Запрашиваем результаты из harvest (ТОЛЬКО сейчас — после всех ставок)
    hcon = sqlite3.connect(HARVEST_DB)
    placeholders = ','.join('?' * len(eids))
    results = hcon.execute(f"""
        SELECT event_id, home_team, away_team, score, winner
        FROM raw_events
        WHERE event_id IN ({placeholders})
    """, eids).fetchall()
    hcon.close()

    settled_n, void_n, pending_n = 0, 0, 0

    for eid, home, away, score, winner_col in results:
        winner = derive_winner(home, away, winner_col, score)
        if winner is None:
            pending_n += 1
            continue

        bets = pcon.execute("""
            SELECT id, bet_team, odds, stake_usd
            FROM paper_bets
            WHERE event_id = ? AND settled = 0
        """, (str(eid),)).fetchall()

        now_ts = datetime.datetime.utcnow().isoformat()
        for row_id, bet_team, odds, stake in bets:
            if bet_team == winner:
                outcome, pnl = 'win', (odds - 1.0) * stake
            else:
                outcome, pnl = 'loss', -stake

            pcon.execute("""
                UPDATE paper_bets
                SET settled=1, outcome=?, pnl=?, settled_ts=?
                WHERE id=?
            """, (outcome, round(pnl, 2), now_ts, row_id))
            settled_n += 1

    # Матчи без результата в harvest — void
    settled_eids = {str(r[0]) for r in results if derive_winner(r[1],r[2],r[4],r[3]) is not None}
    for eid in eids:
        if str(eid) not in settled_eids and str(eid) not in {str(r[0]) for r in results}:
            # Матч ещё не в harvest или нет результата — пропускаем
            pending_n += 1

    pcon.commit()
    pcon.close()

    print(f"\n{'─'*50}")
    print(f"✅ Урегулировано ставок: {settled_n:,}")
    print(f"   Ожидают результата:   {pending_n:,} матчей")


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: REPORT  —  P&L по стратегиям
# ══════════════════════════════════════════════════════════════════════════════

def mode_report():
    print(f"\n{'═'*60}")
    print("PAPER REPORT")
    print(f"{'═'*60}\n")

    if not os.path.exists(PAPER_DB):
        print("Нет paper_trading.db.")
        return

    pcon = sqlite3.connect(PAPER_DB)

    # Общая сводка
    total, settled, wins, losses = pcon.execute("""
        SELECT COUNT(*), SUM(settled),
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END),
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)
        FROM paper_bets
    """).fetchone()
    gross = pcon.execute("SELECT SUM(pnl) FROM paper_bets WHERE settled=1").fetchone()[0] or 0
    stake = pcon.execute("SELECT SUM(stake_usd) FROM paper_bets WHERE settled=1 AND outcome!='void'").fetchone()[0] or 1

    print(f"Всего ставок:    {total or 0:,}")
    print(f"Урегулировано:   {settled or 0:,}")
    print(f"Win / Loss:      {wins or 0} / {losses or 0}")
    wr = wins/(wins+losses)*100 if wins and (wins+losses) > 0 else 0
    roi = gross/stake*100 if stake else 0
    print(f"Win rate:        {wr:.1f}%")
    print(f"Gross P&L:       ${gross:+.2f}")
    print(f"ROI:             {roi:+.1f}%\n")

    # По стратегиям
    rows = pcon.execute("""
        SELECT strategy_name, division,
               COUNT(*) as bets,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               ROUND(SUM(pnl),2) as pnl,
               ROUND(SUM(stake_usd) FILTER (WHERE outcome!='void'),2) as staked
        FROM paper_bets
        WHERE settled = 1
        GROUP BY strategy_name, division
        ORDER BY pnl DESC
    """).fetchall()

    if not rows:
        print("Нет урегулированных ставок. Запусти --mode settle.")
        pcon.close(); return

    print(f"{'Стратегия':<12} {'Div':<4} {'Ставок':>7} {'Win%':>6} {'P&L':>9} {'ROI%':>7}")
    print('─' * 52)
    for name, div, bets, w, l, pnl, staked in rows:
        wr_s = w/(w+l)*100 if (w+l) > 0 else 0
        roi_s = pnl/staked*100 if staked else 0
        flag = '✅' if roi_s > 0 else '❌'
        print(f"{name:<12} {div:<4} {bets:>7} {wr_s:>5.1f}% {pnl:>+9.2f} {roi_s:>+6.1f}% {flag}")

    # ── Гейт: 30 settled Rule C (M05) signals до следующего этапа исследований ──
    GATE_TARGET = 30
    settled_m05 = pcon.execute(
        "SELECT COUNT(*) FROM paper_bets WHERE strategy_name='M05' AND settled=1"
    ).fetchone()[0] or 0
    pending_m05 = pcon.execute(
        "SELECT COUNT(*) FROM paper_bets WHERE strategy_name='M05' AND settled=0"
    ).fetchone()[0] or 0
    print(f"\n{'─'*52}")
    print(f"ГЕЙТ Rule C (M05): {settled_m05}/{GATE_TARGET} settled "
          f"(+{pending_m05} в ожидании результата)")
    if settled_m05 >= GATE_TARGET:
        print("  ✅ Гейт пройден — можно переходить к следующему этапу исследований.")
    else:
        print(f"  ⏳ Осталось {GATE_TARGET - settled_m05} settled сигналов.")

    pcon.close()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Paper trading для стабильных стратегий')
    ap.add_argument('--mode', required=True, choices=['bet', 'settle', 'report'])
    ap.add_argument('--since', type=int, default=TEST_END + 1,
                    help='Начало периода (UNIX timestamp). По умолчанию: TEST_END+1')
    ap.add_argument('--demo', action='store_true',
                    help='Демо-режим: ставим на TEST-период (2026-01-01 — 2026-06-15)')
    args = ap.parse_args()

    since = VAL_END + 1 if args.demo else args.since

    if args.mode == 'bet':
        mode_bet(since)
    elif args.mode == 'settle':
        mode_settle()
    elif args.mode == 'report':
        mode_report()
