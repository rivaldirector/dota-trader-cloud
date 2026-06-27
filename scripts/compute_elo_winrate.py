#!/usr/bin/env python3
"""
compute_elo_winrate.py — бэктест точности (winrate) ЧИСТО Elo-модели на
объединённой истории (BetsAPI + PandaScore), без правил Rule C / H2H / одсов.

Методология (без утечки данных):
  - Один проход по всем матчам, СТРОГО хронологически.
  - Для каждого матча: сначала читаем текущий Elo обеих команд (ДО матча),
    favorite = команда с более высоким Elo, prob = elo_exp(...).
  - Затем сравниваем favorite с реальным исходом → hit/miss.
  - И только ПОСЛЕ этого обновляем Elo по факту матча (K=32).
  - Elo НЕ меняется, формула та же, что в generate_dashboard.py / paper_trading.py.

Ничего не пишет в БД — чисто диагностика/отчёт.
"""
import os, sqlite3, re, math, datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
HARVEST_DB   = os.path.join(_DIR, '../storage/betsapi_harvest.db')
PS_HIST_DB   = os.path.join(_DIR, '../data/pandascore_history.db')

K_FACTOR  = 32.0
START_ELO = 1000.0


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


def load_merged_history():
    """Возвращает (history, n_betsapi, n_ps_new, n_ps_dup) —
    history: list of (start_time, home, away, act_h) chronologically sorted,
    act_h: 1.0 если home победил, 0.0 если away, 0.5 draw."""
    history = []
    seen = set()
    n_betsapi = 0

    hcon = sqlite3.connect(HARVEST_DB, timeout=10)
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
        if key in seen:
            continue
        seen.add(key)
        history.append((st, home, away, act_h))
        n_betsapi += 1
    hcon.close()

    n_ps_new, n_ps_dup = 0, 0
    if os.path.exists(PS_HIST_DB):
        pscon = sqlite3.connect(PS_HIST_DB, timeout=10)
        for home, away, st, winner in pscon.execute("""
            SELECT home_team, away_team, start_time, winner FROM ps_matches
            WHERE winner IS NOT NULL
        """).fetchall():
            key = (normalize_team(home), normalize_team(away), st // 3600)
            if key in seen:
                n_ps_dup += 1
                continue
            nw = normalize_team(winner)
            if nw == normalize_team(home):
                act_h = 1.0
            elif nw == normalize_team(away):
                act_h = 0.0
            else:
                continue
            seen.add(key)
            history.append((st, home, away, act_h))
            n_ps_new += 1
        pscon.close()

    history.sort(key=lambda r: r[0])
    return history, n_betsapi, n_ps_new, n_ps_dup


def backtest(history, since_ts=None):
    """Прогоняет rolling-Elo по history. Если since_ts задан — считает
    winrate/калибровку ТОЛЬКО на матчах с start_time>=since_ts, но Elo всё
    равно обновляется по ВСЕЙ истории (до since_ts тоже), иначе на свежих
    матчах будут стартовые 1000/1000 рейтинги вместо реальных."""
    elo = {}
    n_eval, n_hit, n_draw_skip = 0, 0, 0
    prob_sum_correct = 0.0  # для Brier score / калибровки

    brier_sum = 0.0
    for st, home, away, act_h in history:
        eh, ea = elo.get(home, START_ELO), elo.get(away, START_ELO)
        exp_h = elo_exp(eh, ea)

        if since_ts is None or st >= since_ts:
            if act_h != 0.5:  # пропускаем "draw" — в Dota по сути не бывает,
                              # но если просочился такой кейс — не считаем как win/loss
                fav_is_home = exp_h >= 0.5
                actual_home_won = act_h == 1.0
                hit = (fav_is_home == actual_home_won)
                n_eval += 1
                n_hit += 1 if hit else 0
                brier_sum += (exp_h - act_h) ** 2
            else:
                n_draw_skip += 1

        # обновляем Elo по факту матча (после оценки!)
        elo[home] = eh + K_FACTOR * (act_h - exp_h)
        elo[away] = ea + K_FACTOR * ((1 - act_h) - (1 - exp_h))

    winrate = n_hit / n_eval if n_eval else None
    brier = brier_sum / n_eval if n_eval else None
    return n_eval, n_hit, winrate, brier, n_draw_skip


def main():
    history, n_bets, n_ps_new, n_ps_dup = load_merged_history()
    total = len(history)
    now = datetime.datetime.utcnow()

    print("=" * 70)
    print("ИСТОЧНИКИ ИСТОРИИ (после объединения BetsAPI + PandaScore):")
    print(f"  BetsAPI ended-матчей:                {n_bets}")
    print(f"  PandaScore новых (не было в BetsAPI): {n_ps_new}")
    print(f"  PandaScore дублей (уже были):         {n_ps_dup}")
    print(f"  ИТОГО уникальных матчей в истории:    {total}")
    print("=" * 70)

    # 1) Winrate чистого Elo-фаворита на ВСЕЙ истории
    n_eval, n_hit, wr, brier, draws = backtest(history, since_ts=None)
    print(f"\n[ВСЯ ИСТОРИЯ] оценено матчей: {n_eval} (пропущено draw: {draws})")
    print(f"  Elo-фаворит угадал победителя: {n_hit}/{n_eval} = {wr*100:.2f}%")
    print(f"  Brier score (чем ниже — тем лучше калибровка): {brier:.4f}")

    # 2) Только последние 60 дней (релевантно для текущей формы команд)
    since_60d = int((now - datetime.timedelta(days=60)).timestamp())
    n_eval2, n_hit2, wr2, brier2, draws2 = backtest(history, since_ts=since_60d)
    print(f"\n[ПОСЛЕДНИЕ 60 ДНЕЙ] оценено матчей: {n_eval2} (пропущено draw: {draws2})")
    if n_eval2:
        print(f"  Elo-фаворит угадал победителя: {n_hit2}/{n_eval2} = {wr2*100:.2f}%")
        print(f"  Brier score: {brier2:.4f}")
    else:
        print("  Нет матчей в этом окне.")

    # 3) Только последние 14 дней (самая свежая форма — TI quals / EPL период)
    since_14d = int((now - datetime.timedelta(days=14)).timestamp())
    n_eval3, n_hit3, wr3, brier3, draws3 = backtest(history, since_ts=since_14d)
    print(f"\n[ПОСЛЕДНИЕ 14 ДНЕЙ] оценено матчей: {n_eval3} (пропущено draw: {draws3})")
    if n_eval3:
        print(f"  Elo-фаворит угадал победителя: {n_hit3}/{n_eval3} = {wr3*100:.2f}%")
        print(f"  Brier score: {brier3:.4f}")
    else:
        print("  Нет матчей в этом окне.")

    # 4) Контрольный прогон БЕЗ PandaScore-добавок — чтобы видеть эффект обновления
    history_no_ps = [(st, h, a, r) for (st, h, a, r) in history]
    # нужно пересобрать без PS-матчей, а не фильтровать готовый список,
    # т.к. порядок Elo зависит от полного набора. Делаем отдельный проход:
    history_betsapi_only = []
    seen2 = set()
    hcon = sqlite3.connect(HARVEST_DB, timeout=10)
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
        if key in seen2:
            continue
        seen2.add(key)
        history_betsapi_only.append((st, home, away, act_h))
    hcon.close()
    history_betsapi_only.sort(key=lambda r: r[0])

    n_eval4, n_hit4, wr4, brier4, draws4 = backtest(history_betsapi_only, since_ts=since_60d)
    print(f"\n[СРАВНЕНИЕ] Только BetsAPI (без PandaScore), последние 60 дней:")
    if n_eval4:
        print(f"  оценено: {n_eval4}, угадано: {n_hit4} = {wr4*100:.2f}%, Brier={brier4:.4f}")
    else:
        print("  Нет матчей в этом окне.")
    print(f"\n[СРАВНЕНИЕ] BetsAPI + PandaScore, последние 60 дней:")
    print(f"  оценено: {n_eval2}, угадано: {n_hit2} = {wr2*100:.2f}%, Brier={brier2:.4f}")
    delta_n = n_eval2 - n_eval4
    print(f"\n  → Обновление дало +{delta_n} дополнительных оценённых матчей за 60 дней"
          f" (+{(delta_n/n_eval4*100 if n_eval4 else 0):.1f}%)")


if __name__ == '__main__':
    main()
