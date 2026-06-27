#!/usr/bin/env python3
"""
grid_search.py — локальный перебор параметров backtest без записи в Supabase.

Скачивает исторические данные ОДИН РАЗ, затем в памяти прогоняет
все комбинации параметров и выводит топ-20 по final bankroll.

Run: python3 scripts/grid_search.py
"""
from __future__ import annotations

import os
import sys
import re
import json
import itertools
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from math import pow, log, exp
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT / "scripts"))
from signals import normalize_team, fatigue_adjustment

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL / SUPABASE_ANON_KEY не заданы")
    sys.exit(1)

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# ── Константы (не меняем в grid) ─────────────────────────────────────────────
START_ELO          = 1500.0
K_FACTOR           = 32.0
FUZZY_MIN          = 0.72
AVG_OVERROUND_HIST = 1.0585
INITIAL_BANKROLL   = 1000.0
BACKTEST_START_TS  = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())

# ── Grid параметров ───────────────────────────────────────────────────────────
GRID = {
    "edge_min":       [0.04, 0.05, 0.07, 0.08, 0.10, 0.12],
    "kelly_fraction": [0.25, 0.33, 0.40, 0.50],
    "kelly_cap":      [0.04, 0.05, 0.06, 0.08],
    "comp_min":       [0.52, 0.55, 0.58, 0.60],   # min composite prob to bet
    "und_mode":       [False, True],               # True = запретить underdog
    "elo_weight":     [0.60, 0.70, 0.80],          # вес Elo в ансамбле
}
# Итого: 6*4*4*4*2*3 = 2304 комбинации

# ── Суточные лимиты (не меняем) ──────────────────────────────────────────────
DAILY_STAKE_CAP = 0.20
MAX_BETS_DAY    = 5
MAX_TEAM_BETS   = 2
MAX_LEAGUE_PCT  = 0.30
ADAPTIVE_KELLY_MIN = 0.15


def sb_paginate(path: str, page: int = 1000) -> list:
    result, offset = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{path}{sep}limit={page}&offset={offset}",
            headers=SB_HEADERS, timeout=30
        )
        r.raise_for_status()
        chunk = r.json()
        if not chunk:
            break
        result.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return result


def elo_exp_fn(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def elo_update_fn(ra: float, rb: float, won: bool):
    ea = elo_exp_fn(ra, rb)
    sa = 1.0 if won else 0.0
    return ra + K_FACTOR * (sa - ea), rb + K_FACTOR * ((1-sa) - (1-ea))


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def best_elo_match(name: str, elo: dict) -> Optional[str]:
    best, score = None, 0.0
    nn = normalize_team(name)
    for t in elo:
        s = fuzzy(nn, t)
        if s > score:
            best, score = t, s
    return best if score >= FUZZY_MIN else None


def composite_prob_fn(elo_p: float, form: Optional[float], h2h: Optional[float],
                      elo_w: float, fat_adj: float = 0.0) -> float:
    form_w = (1.0 - elo_w) * 0.625   # пропорционально (форма:h2h = 5:3)
    h2h_w  = (1.0 - elo_w) * 0.375
    total_w  = elo_w
    weighted = elo_w * elo_p
    if form is not None:
        total_w  += form_w
        weighted += form_w * form
    if h2h is not None:
        total_w  += h2h_w
        weighted += h2h_w * h2h
    base = weighted / total_w
    return round(min(0.99, max(0.01, base + fat_adj)), 4)


def kelly_stake_fn(p: float, odds: float, bankroll: float,
                   fraction: float, cap: float, a_mult: float = 1.0) -> float:
    if odds <= 1.0 or p <= 0 or p >= 1:
        return 0.0
    b = odds - 1.0
    q = 1.0 - p
    fk = (b * p - q) / b
    if fk <= 0:
        return 0.0
    f = min(fk * fraction * a_mult, cap)
    return max(round(bankroll * f, 1), 1.0)


def adaptive_mult_fn(recent: list) -> float:
    bets = [b for b in recent if b.get("settled")][-20:]
    if len(bets) < 5:
        return 0.50
    ts = sum(float(b.get("stake_usd", 20)) for b in bets)
    if ts <= 0:
        return 0.50
    tp = sum(float(b.get("pnl", 0)) for b in bets)
    roi = tp / ts
    if roi >= 0:
        return 1.00
    elif roi >= -0.05:
        return 0.75
    elif roi >= -0.10:
        return 0.50
    elif roi >= -0.20:
        return 0.25
    else:
        return ADAPTIVE_KELLY_MIN


def run_sim(combined: list, tiers_map: dict, alias_map: dict, params: dict) -> dict:
    """Запускает одну симуляцию в памяти и возвращает метрики."""
    edge_min       = params["edge_min"]
    kelly_frac     = params["kelly_fraction"]
    kelly_cap      = params["kelly_cap"]
    comp_min       = params["comp_min"]
    no_underdogs   = params["und_mode"]
    elo_w          = params["elo_weight"]

    def resolve(name: str) -> str:
        k = normalize_team(re.sub(r"\s+", " ", (name or "").strip()))
        return alias_map.get(k, name)

    elo: dict[str, float] = {}
    team_history: dict[str, list] = defaultdict(list)
    bankroll = INITIAL_BANKROLL
    peak     = INITIAL_BANKROLL
    recent_bets: list[dict] = []

    day_key = ""
    day_staked = 0.0
    day_bets   = 0
    day_team_bets: dict[str, int] = defaultdict(int)
    day_league_staked: dict[str, float] = defaultdict(float)

    n_bet = wins = losses = 0
    total_pnl = 0.0
    total_staked = 0.0

    for match in combined:
        t1_raw = match["home_team"]
        t2_raw = match["away_team"]
        winner = match["winner"]
        ts     = match["start_time"]
        league = match["league_name"]

        t1 = resolve(t1_raw)
        t2 = resolve(t2_raw)
        t1_n = normalize_team(t1)
        t2_n = normalize_team(t2)

        # Day reset
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        dk = dt.strftime("%Y-%m-%d")
        if dk != day_key:
            day_key = dk
            day_staked = 0.0
            day_bets   = 0
            day_team_bets = defaultdict(int)
            day_league_staked = defaultdict(float)

        e1 = elo.get(t1_n, START_ELO)
        e2 = elo.get(t2_n, START_ELO)
        elo_prob_t1 = elo_exp_fn(e1, e2)

        # Форма и h2h из кеша
        def _form(tn: str) -> Optional[float]:
            g = team_history[tn]
            if len(g) < 2:
                return None
            r = g[-10:]
            return round(sum(w for _, _, w in r) / len(r), 4)

        def _h2h(tn1: str, tn2: str) -> Optional[float]:
            vs = [(ts2, w) for ts2, opp, w in team_history[tn1] if opp == tn2]
            if len(vs) < 2:
                return None
            r = vs[-8:]
            return round(sum(w for _, w in r) / len(r), 4)

        def _fatigue(tn: str) -> int:
            cutoff = ts - 7 * 86400
            return sum(1 for ts2, _, _ in team_history[tn] if ts2 >= cutoff)

        form_t1 = _form(t1_n)
        form_t2 = _form(t2_n)
        h2h_t1  = _h2h(t1_n, t2_n)
        fat_t1  = _fatigue(t1_n)
        fat_t2  = _fatigue(t2_n)

        fav_is_t1 = elo_prob_t1 >= 0.5
        elo_p_fav = elo_prob_t1 if fav_is_t1 else (1 - elo_prob_t1)
        elo_p_und = 1.0 - elo_p_fav
        form_fav  = form_t1 if fav_is_t1 else form_t2
        form_und  = form_t2 if fav_is_t1 else form_t1
        h2h_fav   = h2h_t1
        if not fav_is_t1 and h2h_fav is not None:
            h2h_fav = 1 - h2h_fav
        h2h_und = (1.0 - h2h_fav) if h2h_fav is not None else None
        fat_fav = fat_t1 if fav_is_t1 else fat_t2
        fat_opp = fat_t2 if fav_is_t1 else fat_t1
        fat_adj_fav = fatigue_adjustment(fat_fav, fat_opp)
        fat_adj_und = fatigue_adjustment(fat_opp, fat_fav)

        comp_p_fav = composite_prob_fn(elo_p_fav, form_fav, h2h_fav, elo_w, fat_adj_fav)
        comp_p_und = composite_prob_fn(elo_p_und, form_und, h2h_und, elo_w, fat_adj_und)

        notional_odds_fav = round(1.0 / (elo_p_fav * AVG_OVERROUND_HIST), 4)
        notional_odds_und = round(1.0 / max(elo_p_und, 0.05) / AVG_OVERROUND_HIST, 4)

        edge_fav = round(comp_p_fav * notional_odds_fav - 1, 4)
        edge_und = round(comp_p_und * notional_odds_und - 1, 4)

        # Тир → league edge_min override
        tier_edge = tiers_map.get(league.lower() if league else "", edge_min)
        cur_edge_min = max(edge_min, tier_edge) if tier_edge else edge_min

        # Выбираем сторону
        if no_underdogs or edge_fav >= edge_und:
            edge       = edge_fav
            comp_p     = comp_p_fav
            notional   = notional_odds_fav
            bet_on_fav = True
        else:
            edge       = edge_und
            comp_p     = comp_p_und
            notional   = notional_odds_und
            bet_on_fav = False

        # Обновляем Elo и history
        home_won = normalize_team(winner) == normalize_team(t1_raw)
        new_e1, new_e2 = elo_update_fn(e1, e2, home_won)
        elo[t1_n] = new_e1
        elo[t2_n] = new_e2
        team_history[t1_n].append((ts, t2_n, 1.0 if home_won else 0.0))
        team_history[t2_n].append((ts, t1_n, 0.0 if home_won else 1.0))

        if ts < BACKTEST_START_TS:
            continue

        # Фильтры
        if edge < cur_edge_min:
            continue
        if comp_p < comp_min:
            continue
        # Underdog safety: если ставим на аутсайдера, его Elo должен быть ≥ 33%
        if not bet_on_fav and elo_p_und < 0.33:
            continue

        a_mult = adaptive_mult_fn(recent_bets)
        stake  = kelly_stake_fn(comp_p, notional, bankroll, kelly_frac, kelly_cap, a_mult)
        if stake <= 0:
            continue

        # Корреляция
        bet_n_corr = normalize_team(t1 if (bet_on_fav == fav_is_t1) else t2)
        if day_team_bets[bet_n_corr] >= MAX_TEAM_BETS:
            continue

        league_budget_max = bankroll * DAILY_STAKE_CAP * MAX_LEAGUE_PCT
        if day_league_staked.get(league, 0.0) + stake > league_budget_max:
            remaining = max(0.0, league_budget_max - day_league_staked.get(league, 0.0))
            if remaining < 1.0:
                continue
            stake = round(remaining, 1)

        daily_cap = bankroll * DAILY_STAKE_CAP
        if day_staked + stake > daily_cap or day_bets >= MAX_BETS_DAY:
            continue

        # Исход
        fav_won = (fav_is_t1 and home_won) or (not fav_is_t1 and not home_won)
        bet_won = fav_won if bet_on_fav else not fav_won
        pnl = round((notional - 1.0) * stake, 2) if bet_won else round(-stake, 2)

        bankroll = round(bankroll + pnl, 2)
        peak     = max(peak, bankroll)
        recent_bets.append({"settled": True, "outcome": "win" if bet_won else "loss",
                             "stake_usd": stake, "pnl": pnl})
        day_staked += stake
        day_bets   += 1
        day_team_bets[bet_n_corr] = day_team_bets.get(bet_n_corr, 0) + 1
        day_league_staked[league] = day_league_staked.get(league, 0.0) + stake
        total_staked += stake
        total_pnl    += pnl
        n_bet += 1
        if bet_won:
            wins += 1
        else:
            losses += 1

    roi  = total_pnl / total_staked * 100 if total_staked > 0 else 0
    wr   = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    return {
        "params":   params,
        "bets":     n_bet,
        "wins":     wins,
        "losses":   losses,
        "wr":       round(wr, 1),
        "pnl":      round(total_pnl, 2),
        "roi":      round(roi, 2),
        "final_bk": round(bankroll, 2),
        "peak":     round(peak, 2),
    }


def main():
    print("=== Grid Search START ===")

    # 1) Загружаем данные из Supabase
    print("\n[1] Загружаю исторические данные...")
    all_ps = sb_paginate(
        "elo_pandascore_history"
        "?winner=neq.&select=ps_id,home_team,away_team,winner,start_time,league"
        "&order=start_time.asc"
    )
    print(f"  PandaScore: {len(all_ps)} матчей")

    bapi_rows = []
    try:
        bapi_rows = sb_paginate(
            "betsapi_events"
            "?sport_tag=eq.dota2&status=eq.ended&winner=neq."
            "&select=event_id,home_team,away_team,winner,start_time,league"
            "&order=start_time.asc"
        )
        print(f"  BetsAPI: {len(bapi_rows)} матчей")
    except Exception as ex:
        print(f"  [WARN] betsapi_events: {ex}")

    # Объединяем и дедуплицируем
    combined: list[dict] = []
    seen: set = set()

    for r in bapi_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen:
            continue
        seen.add(key)
        combined.append({"home_team": t1, "away_team": t2, "winner": w,
                         "start_time": int(st), "league_name": r.get("league") or ""})

    for r in all_ps:
        t1, t2, w = r.get("home_team"), r.get("away_team"), r.get("winner")
        st_raw = r.get("start_time")
        if not t1 or not t2 or not w or st_raw is None:
            continue
        if isinstance(st_raw, str):
            try:
                st = int(datetime.fromisoformat(st_raw.replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
        else:
            st = int(st_raw)
        key = (normalize_team(t1), normalize_team(t2), st // 3600)
        if key in seen:
            continue
        seen.add(key)
        combined.append({"home_team": t1, "away_team": t2, "winner": w,
                         "start_time": st, "league_name": r.get("league") or ""})

    combined.sort(key=lambda x: x["start_time"])
    print(f"  Итого: {len(combined)} уникальных матчей")

    # 2) Загружаем тиры и алиасы
    print("\n[2] Загружаю тиры и алиасы...")
    tiers_raw = requests.get(
        f"{SUPABASE_URL}/rest/v1/league_tiers?select=pattern,tier,edge_min",
        headers=SB_HEADERS, timeout=20
    ).json()
    # Для быстроты: {pattern_lower → edge_min}
    tiers_map: dict[str, float] = {}
    for t in (tiers_raw or []):
        pat = (t.get("pattern") or "").lower()
        em  = t.get("edge_min")
        if pat and em:
            tiers_map[pat] = float(em)

    alias_map: dict[str, str] = {}
    try:
        for r in requests.get(
            f"{SUPABASE_URL}/rest/v1/team_aliases?select=alias_name,canonical_name",
            headers=SB_HEADERS, timeout=20
        ).json():
            if r.get("alias_name") and r.get("canonical_name"):
                alias_map[normalize_team(r["alias_name"])] = r["canonical_name"]
    except Exception as ex:
        print(f"  [WARN] team_aliases: {ex}")

    print(f"  Тиров: {len(tiers_map)}, алиасов: {len(alias_map)}")

    # 3) Grid search
    keys   = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    total  = len(combos)
    print(f"\n[3] Grid search: {total} комбинаций...")

    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        res = run_sim(combined, tiers_map, alias_map, params)
        results.append(res)
        if (i + 1) % 200 == 0 or (i + 1) == total:
            best_so_far = max(results, key=lambda r: r["final_bk"])
            print(f"  [{i+1:4d}/{total}] best bank=${best_so_far['final_bk']:,.0f} "
                  f"ROI={best_so_far['roi']:+.1f}% WR={best_so_far['wr']:.1f}% "
                  f"bets={best_so_far['bets']}")

    # 4) Сортируем по final bankroll
    results.sort(key=lambda r: r["final_bk"], reverse=True)

    print(f"\n{'='*90}")
    print(f"{'RANK':4} {'BANK':>8} {'ROI':>7} {'WR':>6} {'BETS':>5} "
          f"{'EDGE':>5} {'KF':>5} {'KCAP':>5} {'CP_MIN':>6} {'NO_UND':>6} {'ELO_W':>6}")
    print(f"{'='*90}")

    for rank, r in enumerate(results[:30], 1):
        p = r["params"]
        marker = " ★" if r["final_bk"] >= 2000 else ""
        print(f"{rank:4d} ${r['final_bk']:>7,.0f} {r['roi']:>+6.1f}% {r['wr']:>5.1f}% "
              f"{r['bets']:>5d} "
              f"{p['edge_min']:>5.2f} {p['kelly_fraction']:>5.2f} "
              f"{p['kelly_cap']:>5.2f} {p['comp_min']:>6.2f} "
              f"{'Y' if p['und_mode'] else 'N':>6} {p['elo_weight']:>6.2f}"
              f"{marker}")

    winner = results[0]
    wp = winner["params"]
    print(f"\n{'='*90}")
    print(f"🏆 WINNER: bank=${winner['final_bk']:,.2f} | ROI={winner['roi']:+.1f}% | "
          f"WR={winner['wr']:.1f}% | bets={winner['bets']}")
    print(f"   edge_min={wp['edge_min']} | kelly_fraction={wp['kelly_fraction']} | "
          f"kelly_cap={wp['kelly_cap']}")
    print(f"   comp_min={wp['comp_min']} | no_underdogs={wp['und_mode']} | "
          f"elo_weight={wp['elo_weight']}")

    # Также ищем лучший по ROI с достаточным числом ставок
    qualified = [r for r in results if r["bets"] >= 50]
    if qualified:
        best_roi = max(qualified, key=lambda r: r["roi"])
        brp = best_roi["params"]
        print(f"\n📈 BEST ROI (≥50 bets): bank=${best_roi['final_bk']:,.2f} | "
              f"ROI={best_roi['roi']:+.1f}% | WR={best_roi['wr']:.1f}% | bets={best_roi['bets']}")
        print(f"   edge_min={brp['edge_min']} | kelly_fraction={brp['kelly_fraction']} | "
              f"kelly_cap={brp['kelly_cap']}")
        print(f"   comp_min={brp['comp_min']} | no_underdogs={brp['und_mode']} | "
              f"elo_weight={brp['elo_weight']}")

    # Сохраняем полный результат в JSON
    out_path = ROOT / "scripts" / "grid_results.json"
    with open(out_path, "w") as f:
        json.dump(results[:50], f, indent=2)
    print(f"\n✓ Топ-50 сохранены в {out_path}")
    print("=== Grid Search DONE ===")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
