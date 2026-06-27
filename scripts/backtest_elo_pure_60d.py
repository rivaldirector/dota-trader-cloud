#!/usr/bin/env python3
"""
backtest_elo_pure_60d.py — гипотетический P&L "чистой" Elo-стратегии
(ставим flat $20 на Elo-фаворита, без Rule C / H2H / доп. фильтров) за
последние 60 дней, используя ОБНОВЛЁННЫЙ Elo (BetsAPI + PandaScore история).

Важно про источники:
  - Elo считается по ПОЛНОЙ объединённой истории (как в compute_elo_winrate.py),
    чтобы рейтинги команд на момент каждого матча были максимально точные.
  - Реально "проставить" можно только то, на что есть котировки — то есть
    только BetsAPI-матчи с записью в odds_summary (PandaScore их не даёт).
    PandaScore-матчи участвуют ТОЛЬКО в обновлении Elo, не в P&L.
  - Котировки: open_home/open_away из odds_summary (тот же источник, что
    использует paper_trading.py --mode bet) — приоритет PinnacleSports,
    иначе любой доступный букмекер.

Это ОТДЕЛЬНАЯ гипотетическая симуляция "если бы мы всегда ставили flat $20
на Elo-фаворита" — НЕ то же самое, что реальный paper-трейдинг по 35
стратегиям (там Rule C / H2H / edge-фильтры). Числа печатаются и сохраняются
в data/elo_pure_60d_backtest.json для подстановки в dashboard.html.
Ничего не пишет в paper_trading.db / betsapi_harvest.db.
"""
import os, sqlite3, re, json, datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
HARVEST_DB = os.path.join(_DIR, '../storage/betsapi_harvest.db')
PS_HIST_DB = os.path.join(_DIR, '../data/pandascore_history.db')
OUT_JSON   = os.path.join(_DIR, '../data/elo_pure_60d_backtest.json')

K_FACTOR   = 32.0
START_ELO  = 1000.0
STAKE_FLAT = 20.0
STARTING_BANK = 1000.0
PREFERRED_BM = ['PinnacleSports', 'Bet365', 'GGBet', 'MelBet', 'YSB88']
WINDOW_DAYS = 60


def normalize_team(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def elo_exp(ea, eb):
    return 1.0 / (1.0 + 10 ** ((eb - ea) / 400.0))


def parse_score(score):
    if not score:
        return None, None
    s = str(score).strip().lower()
    if s == 'home':
        return 1, 0
    if s == 'away':
        return 0, 1
    if '-' in s:
        parts = s.split('-')
        try:
            return int(parts[0].strip()), int(parts[1].strip())
        except Exception:
            return None, None
    return None, None


def load_merged_history_with_ids():
    """Как в compute_elo_winrate.py, но дополнительно несём event_id для
    BetsAPI-записей (нужно для подбора реальных котировок). PandaScore-записи
    участвуют только в обновлении Elo, event_id=None."""
    history = []
    seen = set()

    hcon = sqlite3.connect(HARVEST_DB, timeout=10)
    for eid, home, away, score, st in hcon.execute("""
        SELECT event_id, home_team, away_team, score, CAST(start_time AS INTEGER)
        FROM raw_events
        WHERE league LIKE 'DOTA2%' AND status='ended'
          AND score IS NOT NULL AND score != ''
    """).fetchall():
        sh, sa = parse_score(score)
        if sh is None:
            continue
        act_h = 1.0 if sh > sa else (0.0 if sa > sh else 0.5)
        key = (normalize_team(home), normalize_team(away), st // 3600)
        if key in seen:
            continue
        seen.add(key)
        history.append((st, home, away, act_h, str(eid)))
    hcon.close()

    if os.path.exists(PS_HIST_DB):
        pscon = sqlite3.connect(PS_HIST_DB, timeout=10)
        for home, away, st, winner in pscon.execute("""
            SELECT home_team, away_team, start_time, winner FROM ps_matches
            WHERE winner IS NOT NULL
        """).fetchall():
            key = (normalize_team(home), normalize_team(away), st // 3600)
            if key in seen:
                continue
            nw = normalize_team(winner)
            if nw == normalize_team(home):
                act_h = 1.0
            elif nw == normalize_team(away):
                act_h = 0.0
            else:
                continue
            seen.add(key)
            history.append((st, home, away, act_h, None))  # без odds -> не bettable
        pscon.close()

    history.sort(key=lambda r: r[0])
    return history


def load_odds_map():
    """event_id -> {bookmaker: (open_home, open_away)}"""
    hcon = sqlite3.connect(HARVEST_DB, timeout=10)
    odds_map = {}
    for eid, bm, oh, oa in hcon.execute("""
        SELECT event_id, bookmaker, open_home, open_away
        FROM odds_summary
        WHERE market = '151_1' AND open_home > 1 AND open_away > 1
    """).fetchall():
        odds_map.setdefault(str(eid), {})[bm] = (float(oh), float(oa))
    hcon.close()
    return odds_map


def pick_odds(bm_odds: dict):
    for bm in PREFERRED_BM:
        if bm in bm_odds:
            return bm, bm_odds[bm]
    if bm_odds:
        bm = next(iter(bm_odds))
        return bm, bm_odds[bm]
    return None, None


def main():
    history = load_merged_history_with_ids()
    odds_map = load_odds_map()

    now = datetime.datetime.utcnow()
    since_ts = int((now - datetime.timedelta(days=WINDOW_DAYS)).timestamp())

    elo = {}
    bets = []  # (st, home, away, bet_side, bookmaker, odds, win, pnl)

    for st, home, away, act_h, eid in history:
        eh, ea = elo.get(home, START_ELO), elo.get(away, START_ELO)
        exp_h = elo_exp(eh, ea)

        if eid is not None and st >= since_ts and act_h != 0.5:
            bm_odds = odds_map.get(eid)
            if bm_odds:
                bm, (oh, oa) = pick_odds(bm_odds)
                fav_home = exp_h >= 0.5
                bet_side = 'home' if fav_home else 'away'
                odds_used = oh if fav_home else oa
                won = (fav_home and act_h == 1.0) or ((not fav_home) and act_h == 0.0)
                pnl = STAKE_FLAT * (odds_used - 1.0) if won else -STAKE_FLAT
                bets.append({
                    'start_time': st, 'home': home, 'away': away,
                    'bet_side': bet_side, 'bookmaker': bm, 'odds': odds_used,
                    'elo_prob_fav': exp_h if fav_home else (1 - exp_h),
                    'won': won, 'pnl': round(pnl, 2),
                })

        # обновляем Elo по факту матча (после оценки/ставки)
        elo[home] = eh + K_FACTOR * (act_h - exp_h)
        elo[away] = ea + K_FACTOR * ((1 - act_h) - (1 - exp_h))

    n_bets = len(bets)
    n_wins = sum(1 for b in bets if b['won'])
    total_staked = n_bets * STAKE_FLAT
    total_pnl = round(sum(b['pnl'] for b in bets), 2)
    winrate = (n_wins / n_bets) if n_bets else None
    roi_pct = (total_pnl / total_staked * 100) if total_staked else None
    final_bank = round(STARTING_BANK + total_pnl, 2)

    print("=" * 70)
    print(f"ГИПОТЕТИЧЕСКИЙ P&L: flat $20 на Elo-фаворита, последние {WINDOW_DAYS} дней")
    print(f"(Elo обновлён: BetsAPI + PandaScore история, см. compute_elo_winrate.py)")
    print("=" * 70)
    print(f"Ставок (только где были реальные котировки): {n_bets}")
    print(f"Побед: {n_wins} ({winrate*100:.2f}%)" if n_bets else "Нет ставок в окне")
    print(f"Поставлено всего: ${total_staked:,.2f}")
    print(f"P&L: {'+' if total_pnl >= 0 else ''}${total_pnl:,.2f}  (ROI {roi_pct:+.2f}%)" if n_bets else "")
    print(f"Гипотетический банк: ${STARTING_BANK:,.2f} -> ${final_bank:,.2f}")

    out = {
        'generated_at': now.isoformat(),
        'window_days': WINDOW_DAYS,
        'stake_flat': STAKE_FLAT,
        'starting_bank': STARTING_BANK,
        'n_bets': n_bets,
        'n_wins': n_wins,
        'winrate_pct': round(winrate * 100, 2) if winrate is not None else None,
        'total_staked': total_staked,
        'total_pnl': total_pnl,
        'roi_pct': round(roi_pct, 2) if roi_pct is not None else None,
        'final_bank': final_bank,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nСохранено: {OUT_JSON}")
    print("Запусти scripts/generate_dashboard.py чтобы добавить блок в дэшборд.")


if __name__ == '__main__':
    main()
