#!/usr/bin/env python3
"""
elo_auto_bet.py — АВТОНОМНАЯ машина решений: сама выбирает фаворита по Elo
на каждый предстоящий матч и сама фиксирует виртуальную ставку. Без участия
человека и без сигналов на ревью — ты только смотришь /dashboard.

ВАЖНО про деньги — честно, без иллюзий:
  Рыночных коэффициентов на эти матчи НЕТ (BetsAPI мёртв с 17 июня, ни
  локально, ни в облаке). Поэтому "odds" здесь — НЕ рыночная цена, а
  условная оценка: notional_odds = 1 / (model_prob * AVG_OVERROUND_HIST),
  где AVG_OVERROUND_HIST=1.0585 — это РЕАЛЬНЫЙ средний оверраунд (маржа
  букмекера), посчитанный по 68 733 историческим строкам odds_summary в
  этой же базе (см. SQL в чате, "avg_overround":1.0585). Это иллюстрация
  правдоподобного исхода, НЕ настоящий edge — пока одсы не подключены,
  главная метрика, на которую смотрим — это winrate Elo-фаворита, а не $.

Источники (оба бесплатные, без ключа — те же, что в prematch_free_predict.py):
  1. Расписание: https://dota.haglund.dev/v1/matches (Liquipedia)
  2. Elo: walk-forward по betsapi_events (sport_tag=dota2, status=ended) +
     elo_pandascore_history (доп. покрытие лиг, недообсчитанных BetsAPI —
     TI Quals, EPL и т.п., см. fetch_pandascore_history_cloud.py), мёрдж
     и дедуп как в локальном generate_dashboard.py.

Стратегия: strategy_name='AUTO_ELO_FLAT', division='FREE' — ОТДЕЛЬНАЯ от
Rule C (M05/M06/M36 family), которая остаётся frozen и нетронутой. Ставим
ВСЕГДА на фаворита (не фильтруем по edge — edge тут не считается осмысленно
без реальной цены), flat $20, идемпотентно (UNIQUE(strategy_name,event_id,
division) в elo_paper_bets — повторный запуск не задвоит ставку).

Run:
    python3 scripts/elo_auto_bet.py

GitHub Actions: каждые 2 часа (см. elo_auto_pipeline.yml), вместе с settle.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from math import pow
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

MATCHES_URL = "https://dota.haglund.dev/v1/matches"
HOURS_AHEAD = 72            # горизонт прогноза — зафиксирован 72ч (как в local dashboard)
START_ELO = 1500.0
K_FACTOR = 32
FUZZY_MIN = 0.72
STAKE_USD = 20.0
AVG_OVERROUND_HIST = 1.0585  # реальный средний оверраунд по 68733 историческим строкам odds_summary
STRATEGY_NAME = "AUTO_ELO_FLAT"
DIVISION = "FREE"


def elo_exp(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def sb_get(table: str, qs: str) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
                      headers={**SB_HEADERS, "Prefer": "return=representation"}, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                       headers=SB_HEADERS, json=rows, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] upsert {table}: {r.status_code} {r.text[:200]}")


def normalize_team(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def clean_team_name(name: str | None) -> str:
    """Убирает суффикс ' (page does not exist)', который dota.haglund.dev
    сам добавляет в name, если у команды нет статьи на Liquipedia."""
    if not name:
        return name or "?"
    return name.split(" (page does not exist)")[0].strip()


def fetch_team_aliases() -> dict[str, str]:
    """alias_name (нормализованное) -> canonical_name. Команды иногда играют
    под другим именем — например, PARIVISION выступает как TEAM VISION на
    TI2026 квалах из-за правила Valve против спонсоров-букмекеров (тот же
    состав/организация). Таблица team_aliases в Supabase — ручной список,
    дополняемый по мере обнаружения новых случаев (полностью автоматическое
    обнаружение ребрендов ненадёжно без платного API с историей ростеров)."""
    try:
        rows = sb_get("team_aliases", "select=alias_name,canonical_name")
    except Exception as ex:
        print(f"  [WARN] team_aliases недоступна: {ex}")
        return {}
    return {
        normalize_team(r["alias_name"]): r["canonical_name"]
        for r in rows if r.get("alias_name") and r.get("canonical_name")
    }


def resolve_alias(name: str | None, alias_map: dict[str, str]) -> str | None:
    if not name:
        return name
    key = normalize_team(clean_team_name(name))
    return alias_map.get(key, name)


def build_elo_from_supabase() -> dict[str, float]:
    print("Тяну историю dota2 матчей из Supabase для Elo (BetsAPI + PandaScore)...")
    page = 1000
    history: list[tuple[int, str, str, float]] = []  # (start_time, home, away, act_h)
    seen_keys: set[tuple[str, str, int]] = set()

    # 1) BetsAPI — основной источник, winner уже бинарный (без "ничьих")
    rows, offset = [], 0
    while True:
        chunk = sb_get(
            "betsapi_events",
            f"sport_tag=eq.dota2&status=eq.ended&winner=neq.&"
            f"select=home_team,away_team,winner,start_time&"
            f"order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    for r in rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        history.append((int(st), t1, t2, 1.0 if w == t1 else 0.0))
    n_betsapi = len(history)

    # 2) PandaScore — добор лиг, недообсчитанных BetsAPI (см. docstring выше)
    ps_rows, offset = [], 0
    while True:
        chunk = sb_get(
            "elo_pandascore_history",
            f"winner=neq.&select=home_team,away_team,winner,start_time&"
            f"order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk:
            break
        ps_rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    ps_added = 0
    for r in ps_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys:
            continue  # уже есть из BetsAPI — не дублируем
        nw = normalize_team(w)
        if nw == normalize_team(t1):
            act_h = 1.0
        elif nw == normalize_team(t2):
            act_h = 0.0
        else:
            continue  # не смогли определить сторону — пропускаем
        seen_keys.add(key)
        history.append((int(st), t1, t2, act_h))
        ps_added += 1

    history.sort(key=lambda r: r[0])  # хронологически — обязательно для Elo (no leakage)

    elo: dict[str, float] = {}
    for st, t1, t2, act_h in history:
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        ea = elo_exp(e1, e2)
        elo[t1] = e1 + K_FACTOR * (act_h - ea)
        elo[t2] = e2 + K_FACTOR * ((1 - act_h) - (1 - ea))
    print(f"  матчей в истории: {len(history)} (BetsAPI: {n_betsapi}, +PandaScore: {ps_added})  "
          f"|  команд с Elo: {len(elo)}")
    return elo


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_elo_match(name: str, elo: dict[str, float]) -> tuple[str | None, float]:
    best, score = None, 0.0
    for team in elo:
        s = fuzzy(name, team)
        if s > score:
            best, score = team, s
    return (best, score) if score >= FUZZY_MIN else (None, score)


def fetch_upcoming_matches() -> list[dict]:
    r = requests.get(MATCHES_URL, timeout=20)
    r.raise_for_status()
    return r.json()


def is_real_team(name: str | None) -> bool:
    return bool(name) and name != "TBD"


def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    try:
        matches = fetch_upcoming_matches()
    except Exception as ex:
        print(f"ERROR: dota.haglund.dev недоступен: {ex}")
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_AHEAD)
    soon = []
    for m in matches:
        starts_at = m.get("startsAt")
        if not starts_at:
            continue
        try:
            ts = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts <= cutoff:
            teams = m.get("teams") or [None, None]
            t1 = (teams[0] or {}).get("name") if teams[0] else None
            t2 = (teams[1] or {}).get("name") if teams[1] else None
            if is_real_team(t1) and is_real_team(t2):
                soon.append({**m, "_t1": t1, "_t2": t2, "_ts": ts})

    print(f"Матчей в окне {HOURS_AHEAD}ч: {len(soon)}")
    if not soon:
        print("Нет матчей — выходим, ничего не ставим.")
        return

    elo = build_elo_from_supabase()
    alias_map = fetch_team_aliases()
    if alias_map:
        print(f"  алиасов команд загружено: {len(alias_map)}")

    # Уже поставленные (идемпотентность на стороне клиента — не обязательно,
    # UNIQUE constraint в БД и так не даст задвоить, но так меньше шума в логах)
    existing = sb_get(
        "elo_paper_bets",
        f"strategy_name=eq.{STRATEGY_NAME}&division=eq.{DIVISION}&select=event_id",
    )
    done_ids = {r["event_id"] for r in existing}

    rows = []
    for m in soon:
        eid = f"liq_{m.get('hash')}"
        if eid in done_ids:
            continue
        t1, t2 = m["_t1"], m["_t2"]
        t1_lookup = resolve_alias(t1, alias_map)
        t2_lookup = resolve_alias(t2, alias_map)
        if t1_lookup != t1:
            print(f"  [алиас] {t1} -> {t1_lookup}")
        if t2_lookup != t2:
            print(f"  [алиас] {t2} -> {t2_lookup}")
        m1, _ = best_elo_match(t1_lookup, elo)
        m2, _ = best_elo_match(t2_lookup, elo)
        if not (m1 and m2):
            print(f"  ? {t1} vs {t2} — нет Elo-истории для обеих команд, пропуск (не ставим без данных)")
            continue

        e1, e2 = elo[m1], elo[m2]
        model_prob_1 = elo_exp(e1, e2)
        fav_is_t1 = model_prob_1 >= 0.5
        fav_team = t1 if fav_is_t1 else t2
        fav_prob = model_prob_1 if fav_is_t1 else (1 - model_prob_1)

        notional_odds = round(1.0 / (fav_prob * AVG_OVERROUND_HIST), 3)
        notional_market_prob = round(1.0 / notional_odds, 4)
        edge = round(fav_prob - notional_market_prob, 4)

        print(f"\n  {t1} vs {t2}  ({m.get('leagueName')})  старт {m['_ts'].strftime('%Y-%m-%d %H:%M')}Z")
        print(f"    АВТОНОМНОЕ РЕШЕНИЕ: ставим ${STAKE_USD:.0f} на {fav_team} "
              f"(model_prob={fav_prob:.1%}, notional_odds={notional_odds})")

        rows.append({
            "run_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy_name": STRATEGY_NAME,
            "event_id": eid,
            "division": DIVISION,
            "league": m.get("leagueName"),
            "home_team": t1, "away_team": t2,
            "start_time": int(m["_ts"].timestamp()),
            "bookmaker": "NOTIONAL_HIST_AVG",
            "bet_team": "home" if fav_is_t1 else "away",
            "odds": notional_odds,
            "market_prob": notional_market_prob,
            "model_prob": round(fav_prob, 4),
            "edge": edge,
            "stake_usd": STAKE_USD,
            "settled": False,
        })

    sb_upsert("elo_paper_bets", rows, on_conflict="strategy_name,event_id,division")
    print(f"\nАвтономно поставлено новых ставок: {len(rows)}")


if __name__ == "__main__":
    main()
