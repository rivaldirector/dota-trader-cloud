#!/usr/bin/env python3
"""
backtest_simulation.py — историческая симуляция стратегии AUTO_ELO_FLAT
за последние ~3 месяца (elo_pandascore_history, Apr-Jun 2026).

Использует те же параметры что и elo_auto_bet.py:
  - Elo rating (строится из всей истории)
  - Notional odds = 1 / (elo_prob * AVG_OVERROUND_HIST)
  - Edge filter: edge >= 2%
  - Kelly fraction 0.25, cap 4%
  - Daily stake cap 20% банка

Результат вставляется в elo_paper_bets с strategy_name='SIM_3M'.
Запускать один раз (или для сброса).

Run:
    python3 scripts/backtest_simulation.py [--reset]
"""
from __future__ import annotations

import os
import sys
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

# ── Параметры стратегии (те же что AUTO_ELO_FLAT) ────────────────────────────
STRATEGY_NAME   = "SIM_3M"
DIVISION        = "BACKTEST"
START_ELO       = 1500.0
K_FACTOR        = 32
FUZZY_MIN       = 0.72
AVG_OVERROUND   = 1.0585   # типичный book overround
KELLY_FRACTION  = 0.25
KELLY_CAP       = 0.04     # max 4% банка на ставку
DAILY_CAP_PCT   = 0.20     # max 20% банка в сутки
EDGE_MIN        = 0.02     # минимальный edge (2%)
START_BANKROLL  = 1000.0
# Период симуляции: последние ~3 месяца
SIM_FROM_TS     = 1745280000  # 2026-04-22 UTC (примерно)

# ── Supabase helpers ──────────────────────────────────────────────────────────

def sb_get(path: str) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def sb_post(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict=strategy_name,event_id,division",
        headers=SB_HEADERS,
        json=rows,
        timeout=60,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] {table}: {r.status_code} {r.text[:300]}")


def sb_delete(table: str, qs: str) -> None:
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers=SB_HEADERS,
        timeout=30,
    )
    if r.status_code not in (200, 204):
        print(f"  [SB ERROR] DELETE {table}: {r.status_code} {r.text[:200]}")


# ── Elo ───────────────────────────────────────────────────────────────────────

def normalize(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def elo_exp(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))


def fuzzy(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_elo_match(name: str, elo: dict) -> tuple[str | None, float]:
    best, score = None, 0.0
    for team in elo:
        s = fuzzy(name, team)
        if s > score:
            best, score = team, s
    return (best, score) if score >= FUZZY_MIN else (None, 0.0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    reset = "--reset" in sys.argv

    print("=== Dota Trader — Backtest Simulation (SIM_3M) ===\n")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_ANON_KEY не заданы"); sys.exit(1)

    # 1. Тянем всю историю для построения Elo
    print("Загружаю историю матчей...")
    history: list[tuple[int, str, str, float]] = []
    seen: set[tuple] = set()

    # BetsAPI
    offset, page = 0, 2000
    while True:
        chunk = sb_get(
            f"betsapi_events?sport_tag=eq.dota2&status=eq.ended&winner=neq."
            f"&select=home_team,away_team,winner,start_time"
            f"&order=start_time.asc&limit={page}&offset={offset}"
        )
        if not chunk: break
        for r in chunk:
            t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
            if not (t1 and t2 and w and st): continue
            key = (normalize(t1), normalize(t2), int(st) // 3600)
            if key in seen: continue
            seen.add(key)
            history.append((int(st), t1, t2, 1.0 if w == t1 else 0.0))
        if len(chunk) < page: break
        offset += page

    n_betsapi = len(history)

    # PandaScore
    offset = 0
    while True:
        chunk = sb_get(
            f"elo_pandascore_history?winner=neq."
            f"&select=home_team,away_team,winner,start_time"
            f"&order=start_time.asc&limit={page}&offset={offset}"
        )
        if not chunk: break
        for r in chunk:
            t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
            if not (t1 and t2 and w and st): continue
            key = (normalize(t1), normalize(t2), int(st) // 3600)
            if key in seen: continue
            seen.add(key)
            nw = normalize(w)
            act = 1.0 if nw == normalize(t1) else (0.0 if nw == normalize(t2) else None)
            if act is None: continue
            history.append((int(st), t1, t2, act))
        if len(chunk) < page: break
        offset += page

    history.sort(key=lambda x: x[0])
    print(f"  Всего матчей для Elo: {len(history)} (BetsAPI:{n_betsapi} +PS:{len(history)-n_betsapi})")

    # 2. Выделяем SIM-матчи (PandaScore за последние ~3 месяца)
    print(f"\nЗагружаю PandaScore матчи с {datetime.fromtimestamp(SIM_FROM_TS, tz=timezone.utc).date()}...")
    ps_all = sb_get(
        f"elo_pandascore_history?winner=neq."
        f"&start_time=gte.{SIM_FROM_TS}"
        f"&select=ps_id,home_team,away_team,winner,start_time,league"
        f"&order=start_time.asc&limit=5000"
    )
    print(f"  PandaScore матчей в периоде: {len(ps_all)}")

    # 3. Тянем тиры лиг
    tiers = []
    try:
        tiers = sb_get("league_tiers?select=pattern,tier,edge_min,kelly_cap&order=tier.asc")
    except Exception:
        pass

    def get_tier(league: str | None) -> int:
        if not league: return 3
        lc = (league or "").lower()
        for t in tiers:
            pat = (t.get("pattern") or "").lower()
            if pat and pat in lc:
                return int(t.get("tier", 3))
        return 3

    def get_edge_min(tier: int) -> float:
        for t in tiers:
            if t.get("tier") == tier:
                v = t.get("edge_min")
                if v: return float(v)
        return {1: 0.02, 2: 0.03}.get(tier, 0.05)

    # 4. Строим rolling Elo и симулируем
    print("\nЗапускаю rolling Elo симуляцию...")

    # Строим Elo на всех данных ДО начала периода симуляции
    elo: dict[str, float] = {}
    pre_history = [r for r in history if r[0] < SIM_FROM_TS]
    sim_history  = [r for r in history if r[0] >= SIM_FROM_TS]

    for st, t1, t2, act in pre_history:
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        ea = elo_exp(e1, e2)
        elo[t1] = e1 + K_FACTOR * (act - ea)
        elo[t2] = e2 + K_FACTOR * ((1 - act) - (1 - ea))

    print(f"  Elo на старте периода: {len(elo)} команд")

    # Формируем индекс: (norm_t1, norm_t2, ts//3600) → actual outcome
    sim_idx: dict[tuple, tuple] = {}
    for st, t1, t2, act in sim_history:
        key = (normalize(t1), normalize(t2), st // 3600)
        sim_idx[key] = (t1, t2, act)

    bankroll    = START_BANKROLL
    rows_out: list[dict] = []
    day_staked: dict[str, float] = {}   # date_str → staked
    team_count_today: dict[str, dict[str, int]] = {}  # date_str → {team: count}

    # Идём по PandaScore матчам в хронологическом порядке
    bets_placed = 0
    skipped_no_elo = 0
    skipped_edge   = 0
    skipped_daily  = 0

    for r in ps_all:
        t1  = r.get("home_team")
        t2  = r.get("away_team")
        w   = r.get("winner")
        st  = int(r.get("start_time", 0))
        pid = r.get("ps_id")
        league = r.get("league") or ""
        if not (t1 and t2 and w and st and pid):
            continue

        # Ищем текущее Elo для обеих команд
        m1, _ = best_elo_match(t1, elo)
        m2, _ = best_elo_match(t2, elo)

        # Обновляем Elo ПОСЛЕ решения (не смотрим вперёд)
        act = 1.0 if normalize(w) == normalize(t1) else 0.0
        if m1 and m2:
            e1, e2 = elo[m1], elo[m2]
            ea = elo_exp(e1, e2)
            elo[m1] = e1 + K_FACTOR * (act - ea)
            elo[m2] = e2 + K_FACTOR * ((1 - act) - (1 - ea))
        elif m1:
            e1 = elo[m1]
            elo[m1] = e1 + K_FACTOR * (act - 0.5)
        elif m2:
            e2 = elo[m2]
            elo[m2] = e2 + K_FACTOR * ((1 - act) - 0.5)
        else:
            skipped_no_elo += 1
            continue

        # Решение принимается с данными ДО матча
        # (m1/m2 были найдены до обновления Elo, но elo[m1] уже обновлён…)
        # → нужно использовать сохранённые значения до update
        # Переделаем: сначала принимаем решение, потом обновляем Elo

        # Восстанавливаем значения до update
        if m1 and m2:
            # Откатываем обновление
            elo[m1] = e1
            elo[m2] = e2
            e1_pre, e2_pre = e1, e2
            # Принимаем решение
            p1 = elo_exp(e1_pre, e2_pre)
            fav_is_t1 = p1 >= 0.5
            fav_team  = t1 if fav_is_t1 else t2
            elo_p_fav = p1 if fav_is_t1 else (1 - p1)
            # Notional odds
            notional_odds = 1.0 / (elo_p_fav * AVG_OVERROUND)
            edge = round(elo_p_fav * notional_odds - 1, 4)
            tier = get_tier(league)
            edge_min_t = get_edge_min(tier)

            # Обновляем Elo после принятия решения
            ea = elo_exp(e1_pre, e2_pre)
            elo[m1] = e1_pre + K_FACTOR * (act - ea)
            elo[m2] = e2_pre + K_FACTOR * ((1 - act) - (1 - ea))

            if edge < edge_min_t:
                skipped_edge += 1
                continue

            # Дневной лимит
            date_key = datetime.fromtimestamp(st, tz=timezone.utc).strftime("%Y-%m-%d")
            daily_cap = bankroll * DAILY_CAP_PCT
            today_staked = day_staked.get(date_key, 0.0)
            if today_staked >= daily_cap:
                skipped_daily += 1
                continue

            # Командный лимит (не более 2× одну команду в день)
            tc = team_count_today.setdefault(date_key, {})
            if tc.get(fav_team, 0) >= 2:
                continue

            # Kelly sizing
            b = notional_odds - 1.0
            full_k = (b * elo_p_fav - (1 - elo_p_fav)) / b if b > 0 else 0
            kf = min(full_k * KELLY_FRACTION, KELLY_CAP)
            stake = round(bankroll * kf, 1)
            if stake < 1.0:
                continue

            stake = min(stake, daily_cap - today_staked)
            stake = round(stake, 1)
            if stake < 1.0:
                continue

            # Исход
            bet_side = "home" if fav_is_t1 else "away"
            outcome  = "win" if (act == 1.0 and fav_is_t1) or (act == 0.0 and not fav_is_t1) else "loss"
            pnl      = round(stake * (notional_odds - 1) if outcome == "win" else -stake, 2)
            bankroll = round(bankroll + pnl, 2)

            day_staked[date_key]    = today_staked + stake
            tc[fav_team] = tc.get(fav_team, 0) + 1

            rows_out.append({
                "run_ts":         datetime.fromtimestamp(st, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "strategy_name":  STRATEGY_NAME,
                "event_id":       f"ps_{pid}",
                "division":       DIVISION,
                "league":         league,
                "home_team":      t1,
                "away_team":      t2,
                "start_time":     st,
                "bookmaker":      "NOTIONAL",
                "bet_team":       bet_side,
                "bet_market":     "moneyline",
                "odds":           round(notional_odds, 3),
                "market_prob":    round(1.0 / notional_odds, 4),
                "model_prob":     round(elo_p_fav, 4),
                "composite_prob": round(elo_p_fav, 4),
                "edge":           edge,
                "stake_usd":      stake,
                "settled":        True,
                "settled_ts":     datetime.fromtimestamp(st + 7200, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outcome":        outcome,
                "pnl":            pnl,
                "real_odds":      None,
                "real_bookmaker": None,
                "form_score":     None,
                "h2h_score":      None,
                "kelly_f":        round(kf, 5),
                "league_tier":    tier,
            })
            bets_placed += 1

    print(f"\n── Результаты симуляции ──────────────────────────────")
    print(f"  Ставок сделано:       {bets_placed}")
    print(f"  Пропущено (нет Elo):  {skipped_no_elo}")
    print(f"  Пропущено (edge мал): {skipped_edge}")
    print(f"  Пропущено (дн. лимит):{skipped_daily}")
    if rows_out:
        wins = sum(1 for r in rows_out if r["outcome"] == "win")
        losses = sum(1 for r in rows_out if r["outcome"] == "loss")
        total_staked = sum(r["stake_usd"] for r in rows_out)
        total_pnl    = sum(r["pnl"] for r in rows_out)
        roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
        print(f"  W/L:                  {wins}/{losses}  ({round(wins/(wins+losses)*100,1) if (wins+losses) > 0 else 0}%)")
        print(f"  Total staked:         ${total_staked:.2f}")
        print(f"  Total PnL:            ${total_pnl:.2f}")
        print(f"  ROI:                  {roi:.1f}%")
        print(f"  Final bankroll:       ${bankroll:.2f}")

    # 5. Сохраняем в Supabase
    if reset or True:
        print(f"\nОчищаю старые SIM_3M записи...")
        sb_delete("elo_paper_bets", f"strategy_name=eq.{STRATEGY_NAME}&division=eq.{DIVISION}")

    print(f"Вставляю {len(rows_out)} записей в Supabase...")
    CHUNK = 100
    for i in range(0, len(rows_out), CHUNK):
        sb_post("elo_paper_bets", rows_out[i:i+CHUNK])
        print(f"  [{i+CHUNK}/{len(rows_out)}] ✓")

    print("\n✅ Backtest simulation complete!")


if __name__ == "__main__":
    main()
