#!/usr/bin/env python3
"""
enhanced_backtest.py — Полный бэктест AUTO_ELO_FLAT стратегии.

Использует ВСЕ сигналы live-системы на исторических данных:
  - Rolling Elo (K=32, хронологически по elo_pandascore_history)
  - Form (win rate последних 10 игр)
  - H2H (последние 8 личных встреч)
  - Fatigue (усталость — игры за 7 дней)
  - Composite probability (elo 60% + form 25% + h2h 15%)
  - Kelly sizing (fraction=0.25, cap=4%)
  - Edge filter (≥EDGE_MIN для нотиональных коэффов)
  - Дневные лимиты (≤20% банка, ≤5 ставок в день)
  - Корреляционный лимит (≤2 ставки на команду, ≤30% банка на турнир)

Результат:
  DELETE FROM elo_paper_bets WHERE division IN ('BACKTEST','SIM_3M_SUPERSEDED')
  INSERT AUTO_ELO_FLAT / division='BACKTEST'

Run: python3 scripts/enhanced_backtest.py
GH Actions: .github/workflows/run_backtest.yml (workflow_dispatch)
"""
from __future__ import annotations

import os
import sys
import re
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from math import pow
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT / "scripts"))
from signals import (
    compute_form, compute_h2h, compute_fatigue, fatigue_adjustment,
    composite_prob, kelly_stake, adaptive_kelly_mult,
    KELLY_FRACTION_DEFAULT, ENSEMBLE_WEIGHTS,
    normalize_team,
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL / SUPABASE_ANON_KEY не заданы")
    sys.exit(1)

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

# ── Константы стратегии (идентичны live elo_auto_bet.py) ────────────────────
STRATEGY_NAME      = "AUTO_ELO_FLAT"
DIVISION           = "BACKTEST"
START_ELO          = 1500.0
K_FACTOR           = 32.0
FUZZY_MIN          = 0.72
AVG_OVERROUND_HIST = 1.0585  # средний overround букмекера по истории
EDGE_MIN_DEFAULT   = 0.05    # мин. edge для нотиональных коэффов
KELLY_CAP          = 0.04    # max 4% банка на ставку
MIN_UND_ELO_P      = 0.33   # аутсайдер должен иметь ≥33% шанс по Elo (≤3:1)
DAILY_STAKE_CAP    = 0.20    # max 20% банка в сутки
MAX_BETS_DAY       = 5       # max ставок в день
MAX_TEAM_BETS_DAY  = 2       # корреляция команды
MAX_LEAGUE_PCT     = 0.30    # max 30% банка на один турнир в день
INITIAL_BANKROLL   = 1000.0


def sb_get(table: str, qs: str) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=SB_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_delete(table: str, qs: str) -> None:
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=SB_HEADERS, timeout=30)
    if r.status_code not in (200, 204):
        print(f"  [DELETE ERR] {table}: {r.status_code} {r.text[:200]}")


def sb_insert_batch(table: str, rows: list[dict], batch: int = 200) -> int:
    inserted = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i: i + batch]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**SB_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=chunk,
            timeout=60,
        )
        if r.status_code not in (200, 201, 204):
            print(f"  [INSERT ERR] batch {i}: {r.status_code} {r.text[:200]}")
        else:
            inserted += len(chunk)
    return inserted


# ── Elo ──────────────────────────────────────────────────────────────────────

def elo_exp(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def elo_update(ra: float, rb: float, won: bool) -> tuple[float, float]:
    ea = elo_exp(ra, rb)
    sa = 1.0 if won else 0.0
    new_ra = ra + K_FACTOR * (sa - ea)
    new_rb = rb + K_FACTOR * ((1 - sa) - (1 - ea))
    return new_ra, new_rb


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()


def best_elo_match(name: str, elo: dict[str, float]) -> tuple[Optional[str], float]:
    best, score = None, 0.0
    name_n = normalize_team(name)
    for team in elo:
        s = fuzzy(name_n, team)
        if s > score:
            best, score = team, s
    return (best, score) if score >= FUZZY_MIN else (None, 0.0)


# ── League tiers ──────────────────────────────────────────────────────────────

def fetch_league_tiers() -> list[dict]:
    try:
        return sb_get("league_tiers", "select=pattern,tier,edge_min,kelly_cap&order=tier.asc")
    except Exception as ex:
        print(f"  [WARN] league_tiers: {ex}")
        return []


def get_tier_for_league(league: str, tiers: list[dict]) -> tuple[int, float, float]:
    """Возвращает (tier, edge_min, kelly_cap) для лиги."""
    if not league:
        return (3, EDGE_MIN_DEFAULT, KELLY_CAP)
    lc = league.lower()
    for t in tiers:
        pat = (t.get("pattern") or "").lower()
        if pat and pat in lc:
            return (t.get("tier", 3), float(t.get("edge_min") or EDGE_MIN_DEFAULT),
                    float(t.get("kelly_cap") or KELLY_CAP))
    return (3, EDGE_MIN_DEFAULT, KELLY_CAP)


# ── Main simulation ───────────────────────────────────────────────────────────

def main():
    print("=== Enhanced Backtest START ===")
    print(f"Strategy: {STRATEGY_NAME} / {DIVISION}")
    print(f"Initial bankroll: ${INITIAL_BANKROLL:,.0f}")

    # 1) Загружаем все матчи из PandaScore (хронологически)
    print("\n[1] Загружаю elo_pandascore_history...")
    all_rows = []
    offset, page = 0, 1000
    while True:
        chunk = sb_get(
            "elo_pandascore_history",
            f"winner=neq.&select=ps_id,home_team,away_team,winner,start_time,league"
            f"&order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    print(f"  Загружено: {len(all_rows)} матчей")
    if not all_rows:
        print("  Нет данных — выходим")
        sys.exit(1)

    # Добавляем betsapi_events (если есть, для более ранней истории)
    bapi_rows = []
    try:
        offset2 = 0
        while True:
            chunk = sb_get(
                "betsapi_events",
                f"sport_tag=eq.dota2&status=eq.ended&winner=neq.&"
                f"select=event_id,home_team,away_team,winner,start_time,league&"
                f"order=start_time.asc&limit={page}&offset={offset2}",
            )
            if not chunk:
                break
            bapi_rows.extend(chunk)
            if len(chunk) < page:
                break
            offset2 += page
        print(f"  BetsAPI events: {len(bapi_rows)} матчей")
    except Exception as ex:
        print(f"  [WARN] betsapi_events: {ex}")

    # Объединяем и дедуплицируем по (norm_home, norm_away, hour)
    combined: list[dict] = []
    seen: set[tuple] = set()

    for r in bapi_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen:
            continue
        seen.add(key)
        combined.append({
            "event_id": f"bapi_{r.get('event_id', st)}",
            "home_team": t1, "away_team": t2, "winner": w,
            "start_time": int(st), "league_name": r.get("league") or "",
        })

    for r in all_rows:
        t1, t2, w = r.get("home_team"), r.get("away_team"), r.get("winner")
        st_raw = r.get("start_time")
        if not t1 or not t2 or not w or st_raw is None:
            continue
        # start_time может быть строкой ISO или int
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
        combined.append({
            "event_id": f"ps_{r.get('ps_id', st)}",
            "home_team": t1, "away_team": t2, "winner": w,
            "start_time": st, "league_name": r.get("league") or "",
        })

    combined.sort(key=lambda x: x["start_time"])
    print(f"  Итого уникальных: {len(combined)} матчей")

    # 2) Загружаем тиры лиг
    print("\n[2] Загружаю league_tiers...")
    tiers = fetch_league_tiers()
    print(f"  Тиров: {len(tiers)}")

    # 3) Загружаем alias-маппинг
    print("\n[3] Загружаю team_aliases...")
    alias_map: dict[str, str] = {}
    try:
        for r in sb_get("team_aliases", "select=alias_name,canonical_name"):
            if r.get("alias_name") and r.get("canonical_name"):
                alias_map[normalize_team(r["alias_name"])] = r["canonical_name"]
        print(f"  Алиасов: {len(alias_map)}")
    except Exception as ex:
        print(f"  [WARN] team_aliases: {ex}")

    def resolve(name: str) -> str:
        k = normalize_team(re.sub(r"\s+", " ", (name or "").strip()))
        return alias_map.get(k, name)

    # 4) Rolling simulation
    print("\n[4] Запускаю rolling simulation...")
    elo: dict[str, float] = {}               # team_lower → elo
    history: list[tuple[int, str, str, float]] = []  # (ts, home, away, home_won)
    # Кеш: team_name → список (ts, opponent, won) для быстрого form/h2h
    team_history: dict[str, list[tuple]] = defaultdict(list)
    bankroll = INITIAL_BANKROLL
    recent_bets: list[dict] = []             # для adaptive Kelly (dict с settled/outcome/stake_usd/pnl)

    # Daily tracking
    day_key: str = ""
    day_staked: float = 0.0
    day_bets: int = 0
    day_team_bets: dict[str, int] = defaultdict(int)
    day_league_staked: dict[str, float] = defaultdict(float)

    sim_rows: list[dict] = []
    n_total = len(combined)
    n_bet = n_skip_elo = n_skip_edge = n_skip_daily = n_skip_corr = 0

    for i, match in enumerate(combined):
        t1_raw  = match["home_team"]
        t2_raw  = match["away_team"]
        winner  = match["winner"]
        ts      = match["start_time"]
        league  = match["league_name"]
        eid     = match["event_id"]

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
            day_bets = 0
            day_team_bets = defaultdict(int)
            day_league_staked = defaultdict(float)

        # ── Elo для обеих команд (текущее состояние ПЕРЕД этим матчем) ────────
        e1 = elo.get(t1_n, START_ELO)
        e2 = elo.get(t2_n, START_ELO)
        elo_prob_t1 = elo_exp(e1, e2)

        # ── Сигналы (по кешу ДО этого матча) — O(1)/O(k) вместо O(n) ─────────
        def _form(tn: str, n: int = 10) -> Optional[float]:
            games = team_history[tn]
            if len(games) < 2:
                return None
            recent = games[-n:]
            return round(sum(w for _, _, w in recent) / len(recent), 4)

        def _h2h(tn1: str, tn2: str, n: int = 8) -> Optional[float]:
            vs = [(ts2, w) for ts2, opp, w in team_history[tn1] if opp == tn2]
            if len(vs) < 2:
                return None
            recent = vs[-n:]
            return round(sum(w for _, w in recent) / len(recent), 4)

        def _fatigue(tn: str, cur_ts: int) -> int:
            cutoff = cur_ts - 7 * 86400
            return sum(1 for ts2, _, _ in team_history[tn] if ts2 >= cutoff)

        form_t1  = _form(t1_n)
        form_t2  = _form(t2_n)
        h2h_t1   = _h2h(t1_n, t2_n)
        fat_t1   = _fatigue(t1_n, ts)
        fat_t2   = _fatigue(t2_n, ts)

        fav_is_t1  = elo_prob_t1 >= 0.5
        fav        = t1 if fav_is_t1 else t2
        und        = t2 if fav_is_t1 else t1
        fav_n      = t1_n if fav_is_t1 else t2_n
        und_n      = t2_n if fav_is_t1 else t1_n
        elo_p_fav  = elo_prob_t1 if fav_is_t1 else (1 - elo_prob_t1)
        elo_p_und  = 1.0 - elo_p_fav
        form_fav   = form_t1 if fav_is_t1 else form_t2
        form_und   = form_t2 if fav_is_t1 else form_t1
        h2h_fav    = h2h_t1 if h2h_t1 is not None else None
        if not fav_is_t1 and h2h_fav is not None:
            h2h_fav = 1 - h2h_fav
        h2h_und    = (1.0 - h2h_fav) if h2h_fav is not None else None
        fat_fav    = fat_t1 if fav_is_t1 else fat_t2
        fat_opp    = fat_t2 if fav_is_t1 else fat_t1
        fat_adj_fav = fatigue_adjustment(fat_fav, fat_opp)
        fat_adj_und = fatigue_adjustment(fat_opp, fat_fav)

        # Composite prob для ОБЕИХ сторон
        comp_p_fav = composite_prob(elo_p_fav, form_fav, h2h_fav,
                                    weights=ENSEMBLE_WEIGHTS, fatigue_adj=fat_adj_fav)
        comp_p_und = composite_prob(elo_p_und, form_und, h2h_und,
                                    weights=ENSEMBLE_WEIGHTS, fatigue_adj=fat_adj_und)

        # Нотиональные коэффы для ОБЕИХ сторон
        # Рынок = Elo + overround; мы ищем edge от form/H2H поверх Elo-рынка
        notional_odds_fav = round(1.0 / (elo_p_fav * AVG_OVERROUND_HIST), 4)
        notional_odds_und = round(1.0 / max(elo_p_und, 0.05) / AVG_OVERROUND_HIST, 4)

        edge_fav = round(comp_p_fav * notional_odds_fav - 1, 4)
        edge_und = round(comp_p_und * notional_odds_und - 1, 4)

        # Тир лиги
        _tier, edge_min, kelly_cap = get_tier_for_league(league, tiers)

        # ── Выбираем сторону с лучшим edge ───────────────────────────────────
        if edge_fav >= edge_und:
            edge          = edge_fav
            comp_p        = comp_p_fav
            notional_odds = notional_odds_fav
            bet_on_fav    = True
        else:
            edge          = edge_und
            comp_p        = comp_p_und
            notional_odds = notional_odds_und
            bet_on_fav    = False

        # ── Обновляем Elo и history (для СЛЕДУЮЩИХ матчей) ───────────────────
        home_won = normalize_team(winner) == normalize_team(t1_raw)
        new_e1, new_e2 = elo_update(e1, e2, home_won)
        elo[t1_n] = new_e1
        elo[t2_n] = new_e2
        history.append((ts, t1_raw, t2_raw, 1.0 if home_won else 0.0))
        # Быстрый кеш: (ts, opponent_norm, win_flag)
        team_history[t1_n].append((ts, t2_n, 1.0 if home_won else 0.0))
        team_history[t2_n].append((ts, t1_n, 0.0 if home_won else 1.0))

        # ── Фильтры ──────────────────────────────────────────────────────────
        if edge < edge_min:
            n_skip_edge += 1
            continue

        # Не ставим на огромных аутсайдеров (Elo < 33%) — форма/H2H там ненадёжна
        if not bet_on_fav and elo_p_und < MIN_UND_ELO_P:
            n_skip_edge += 1
            continue

        # Adaptive Kelly
        a_mult = adaptive_kelly_mult(recent_bets[-20:]) if recent_bets else 1.0

        stake = kelly_stake(
            p=comp_p, odds=notional_odds, bankroll=bankroll,
            fraction=KELLY_FRACTION_DEFAULT, cap=kelly_cap, adaptive_mult=a_mult,
        )
        if stake <= 0:
            n_skip_edge += 1
            continue

        # Корреляционный лимит — отслеживаем по команде на которую ставим
        bet_n_for_corr = fav_n if bet_on_fav else und_n
        if day_team_bets[bet_n_for_corr] >= MAX_TEAM_BETS_DAY:
            n_skip_corr += 1
            continue

        league_budget_max = bankroll * DAILY_STAKE_CAP * MAX_LEAGUE_PCT
        if day_league_staked[league] + stake > league_budget_max:
            remaining = max(0.0, league_budget_max - day_league_staked[league])
            if remaining < 1.0:
                n_skip_corr += 1
                continue
            stake = round(remaining, 1)

        # Дневной лимит
        daily_cap = bankroll * DAILY_STAKE_CAP
        if day_staked + stake > daily_cap or day_bets >= MAX_BETS_DAY:
            n_skip_daily += 1
            continue

        # ── Исход ────────────────────────────────────────────────────────────
        fav_won = (fav_is_t1 and home_won) or (not fav_is_t1 and not home_won)
        if bet_on_fav:
            bet_side   = "home" if fav_is_t1 else "away"
            bet_winner = fav
            bet_won    = fav_won
        else:
            bet_side   = "away" if fav_is_t1 else "home"
            bet_winner = und
            bet_won    = not fav_won
        outcome = "win" if bet_won else "loss"
        pnl     = round((notional_odds - 1.0) * stake, 2) if bet_won else round(-stake, 2)

        bankroll  = round(bankroll + pnl, 2)
        recent_bets.append({
            "settled": True, "outcome": outcome,
            "stake_usd": stake, "pnl": pnl, "real_odds": notional_odds,
        })
        day_staked += stake
        day_bets   += 1
        day_team_bets[bet_n_for_corr] += 1
        day_league_staked[league] += stake
        n_bet += 1

        # Для хранения: форма/h2h для ВЫБРАННОЙ стороны
        form_stored = (form_fav if bet_on_fav else form_und)
        h2h_stored  = (h2h_fav  if bet_on_fav else h2h_und)
        elo_stored  = (elo_p_fav if bet_on_fav else elo_p_und)

        b_raw  = notional_odds - 1.0
        full_k = (b_raw * comp_p - (1 - comp_p)) / b_raw if b_raw > 0 else 0
        kf     = round(min(full_k * KELLY_FRACTION_DEFAULT * a_mult, kelly_cap), 5)

        run_ts = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sim_rows.append({
            "run_ts":         run_ts,
            "strategy_name":  STRATEGY_NAME,
            "event_id":       eid,
            "division":       DIVISION,
            "league":         league,
            "home_team":      t1_raw,
            "away_team":      t2_raw,
            "start_time":     ts,
            "bookmaker":      "NOTIONAL_HIST_AVG",
            "bet_team":       bet_side,
            "odds":           notional_odds,
            "market_prob":    round(1.0 / notional_odds, 4),
            "model_prob":     round(elo_stored, 4),
            "composite_prob": comp_p,
            "edge":           edge,
            "stake_usd":      round(stake, 2),
            "settled":        True,
            "outcome":        outcome,
            "pnl":            pnl,
            "real_odds":      None,
            "real_bookmaker": None,
            "form_score":     round(form_stored, 4) if form_stored is not None else None,
            "h2h_score":      round(h2h_stored,  4) if h2h_stored  is not None else None,
            "kelly_f":        kf,
            "league_tier":    _tier,
            "bet_market":     "moneyline",
        })

        side_label = "fav" if bet_on_fav else "UND"
        if n_bet <= 10 or n_bet % 50 == 0:
            print(f"  [{n_bet:4d}] {t1_raw} vs {t2_raw} — bet={bet_winner}({side_label}), "
                  f"comp_p={comp_p:.3f}, odds={notional_odds:.2f}, "
                  f"edge={edge:+.1%}, stake=${stake:.1f}, {outcome}, "
                  f"pnl=${pnl:+.2f}, bank=${bankroll:,.0f}")

    print(f"\n── Simulation Results ─────────────────────────────────────")
    print(f"  Всего матчей:   {n_total}")
    print(f"  Ставок:         {n_bet}")
    print(f"  Пропущено edge: {n_skip_edge}")
    print(f"  Пропущено daily:{n_skip_daily}")
    print(f"  Пропущено corr: {n_skip_corr}")
    wins   = sum(1 for r in sim_rows if r["outcome"] == "win")
    losses = sum(1 for r in sim_rows if r["outcome"] == "loss")
    total_pnl = sum(r["pnl"] for r in sim_rows)
    total_staked = sum(r["stake_usd"] for r in sim_rows)
    wr  = wins/(wins+losses)*100 if (wins+losses) > 0 else 0.0
    roi = total_pnl/total_staked*100 if total_staked > 0 else 0.0
    print(f"  Win rate:       {wins}/{wins+losses} = {wr:.1f}%")
    print(f"  PnL:            ${total_pnl:+,.2f}")
    print(f"  ROI:            {roi:+.1f}%")
    print(f"  Final bankroll: ${bankroll:,.2f}")

    # 5) Удаляем старые бэктест-данные и вставляем новые
    print("\n[5] Удаляю старые данные бэктеста...")
    sb_delete("elo_paper_bets",
              f"strategy_name=eq.SIM_3M")
    sb_delete("elo_paper_bets",
              f"strategy_name=eq.{STRATEGY_NAME}&division=eq.{DIVISION}")
    print("  Удалено SIM_3M + AUTO_ELO_FLAT/BACKTEST")

    print(f"\n[6] Вставляю {len(sim_rows)} строк в elo_paper_bets...")
    inserted = sb_insert_batch("elo_paper_bets", sim_rows)
    print(f"  Вставлено: {inserted} строк")

    # 6) Обновляем bankroll_state на основе ВСЕХ ставок (BACKTEST + LIVE)
    print("\n[7] Пересчитываю bankroll_state...")
    all_settled = sb_get(
        "elo_paper_bets",
        f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.true&stake_usd=gt.0"
        "&select=pnl,stake_usd&order=run_ts.asc",
    )
    all_pnl = sum(float(r.get("pnl") or 0) for r in all_settled)
    all_bets = len(all_settled)
    new_balance = round(INITIAL_BANKROLL + all_pnl, 2)

    # Rolling peak
    peak = INITIAL_BANKROLL
    running = INITIAL_BANKROLL
    for r in all_settled:
        running += float(r.get("pnl") or 0)
        peak = max(peak, running)

    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/bankroll_state?strategy=eq.{STRATEGY_NAME}",
            headers=SB_HEADERS, json={
                "balance": new_balance,
                "peak": round(peak, 2),
                "total_bets": all_bets,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }, timeout=20,
        )
        if r.status_code not in (200, 204):
            print(f"  [WARN] bankroll_state patch: {r.status_code}")
        else:
            print(f"  bankroll_state: balance=${new_balance:,.2f}, peak=${peak:,.2f}, bets={all_bets}")
    except Exception as ex:
        print(f"  [ERR] bankroll_state: {ex}")

    print("\n=== Enhanced Backtest DONE ===")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
