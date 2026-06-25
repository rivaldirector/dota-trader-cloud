#!/usr/bin/env python3
"""
totals_model.py — модель для ставок на тоталы (убийства / длительность / карты).

Используется как фоллбэк в elo_auto_bet.py когда у команды нет Elo-истории.

Источники данных:
  OpenDota /api/teams/{id}/matches  — статистика убийств и длительности per game
  Глобальные средние Dota 2         — фоллбэк если OpenDota не знает команду

Рынки BetsAPI (эвристика по значению линии):
  kills   : 15 ≤ line ≤ 80
  duration: 20 ≤ line ≤ 70   (в минутах)
  maps    : line ∈ {1.5, 2.5} (BO3)
"""
from __future__ import annotations

import math
import re
import time
from difflib import SequenceMatcher
from typing import Optional

import requests

# ── Глобальные средние Dota 2 (про-сцена, ~2023-2025) ───────────────────────
GLOBAL_AVG_KILLS    = 32.0   # убийств на игру (одна карта)
GLOBAL_STD_KILLS    = 9.0
GLOBAL_AVG_DUR_MIN  = 38.0   # минут на игру
GLOBAL_STD_DUR_MIN  = 10.0

# Минимальный порог fuzzy-match для поиска команды в OpenDota
OD_FUZZY_MIN = 0.70
# Минимум матчей для считывания статистики (иначе выборка ненадёжная)
MIN_MATCHES = 5

OPENDOTA_URL = "https://api.opendota.com/api"

_od_teams_cache: list | None = None


# ── OpenDota helpers ──────────────────────────────────────────────────────────

def _od_get(path: str, params: dict | None = None) -> dict | list | None:
    try:
        r = requests.get(f"{OPENDOTA_URL}{path}", params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(3)
            r = requests.get(f"{OPENDOTA_URL}{path}", params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


def _get_od_teams() -> list:
    global _od_teams_cache
    if _od_teams_cache is not None:
        return _od_teams_cache
    data = _od_get("/teams")
    _od_teams_cache = data if isinstance(data, list) else []
    return _od_teams_cache


def find_od_team(name: str) -> tuple[int | None, float]:
    """
    Ищет команду в OpenDota по имени.
    Возвращает (team_id, score) или (None, 0).
    """
    teams = _get_od_teams()
    if not teams:
        return None, 0.0

    name_c = re.sub(r"[^a-z0-9 ]", " ", name.strip().lower())
    best_id, best_score = None, 0.0
    for t in teams:
        tname = re.sub(r"[^a-z0-9 ]", " ", (t.get("name") or "").strip().lower())
        s = SequenceMatcher(None, name_c, tname).ratio()
        if s > best_score:
            best_score, best_id = s, t.get("team_id")

    if best_score >= OD_FUZZY_MIN:
        return best_id, best_score
    return None, 0.0


def get_od_team_stats(team_id: int, n: int = 20) -> dict | None:
    """
    Загружает последние n матчей команды из OpenDota.
    Возвращает {avg_kills, std_kills, avg_dur, std_dur, n} или None.

    radiant_score / dire_score = убийства каждой из сторон в этом матче.
    duration = длительность в секундах.
    """
    data = _od_get(f"/teams/{team_id}/matches", {"limit": n})
    if not isinstance(data, list):
        return None

    kills_list: list[float] = []
    dur_list:   list[float] = []

    for m in data:
        rs  = float(m.get("radiant_score") or 0)
        ds  = float(m.get("dire_score")    or 0)
        dur = float(m.get("duration")      or 0)

        total_kills = rs + ds
        if total_kills > 0:
            kills_list.append(total_kills)
        if dur > 600:  # > 10 минут
            dur_list.append(dur / 60.0)

    if len(kills_list) < MIN_MATCHES:
        return None

    def _mean_std(lst: list[float]) -> tuple[float, float]:
        avg = sum(lst) / len(lst)
        std = math.sqrt(sum((x - avg) ** 2 for x in lst) / len(lst))
        return avg, std

    k_avg, k_std = _mean_std(kills_list)
    d_avg, d_std = _mean_std(dur_list) if len(dur_list) >= MIN_MATCHES else (None, None)

    return {
        "avg_kills": round(k_avg, 1),
        "std_kills": round(max(k_std, 3.0), 1),
        "avg_dur":   round(d_avg, 1) if d_avg else None,
        "std_dur":   round(max(d_std, 4.0), 1) if d_std else None,
        "n":         len(kills_list),
    }


# ── Вероятностная модель ──────────────────────────────────────────────────────

def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """P(X ≤ x) для нормального распределения."""
    if sigma <= 0:
        return 0.5
    z = (x - mu) / sigma
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_over(line: float, mu: float, sigma: float) -> float:
    """P(X > line) — вероятность "больше" для нормального X ~ N(mu, sigma)."""
    return 1.0 - _normal_cdf(line, mu, sigma)


def prob_under(line: float, mu: float, sigma: float) -> float:
    return _normal_cdf(line, mu, sigma)


def combine_team_stats(
    home_stats: dict | None,
    away_stats: dict | None,
) -> dict:
    """
    Объединяет статистику двух команд в общий прогноз на матч.
    Если данных нет — использует глобальные средние.
    """
    def _get(stats, key, default):
        return (stats or {}).get(key) or default

    h_kills = _get(home_stats, "avg_kills", GLOBAL_AVG_KILLS)
    a_kills = _get(away_stats, "avg_kills", GLOBAL_AVG_KILLS)
    h_kstd  = _get(home_stats, "std_kills", GLOBAL_STD_KILLS)
    a_kstd  = _get(away_stats, "std_kills", GLOBAL_STD_KILLS)

    exp_kills = (h_kills + a_kills) / 2.0
    # Суммируем дисперсии (независимые команды) → делим на 2 для среднего
    std_kills = math.sqrt(h_kstd ** 2 + a_kstd ** 2) / math.sqrt(2.0)

    h_dur  = _get(home_stats, "avg_dur",  None)
    a_dur  = _get(away_stats, "avg_dur",  None)
    h_dstd = _get(home_stats, "std_dur",  GLOBAL_STD_DUR_MIN)
    a_dstd = _get(away_stats, "std_dur",  GLOBAL_STD_DUR_MIN)

    if h_dur and a_dur:
        exp_dur = (h_dur + a_dur) / 2.0
        std_dur = math.sqrt(h_dstd ** 2 + a_dstd ** 2) / math.sqrt(2.0)
    elif h_dur:
        exp_dur, std_dur = h_dur, h_dstd
    elif a_dur:
        exp_dur, std_dur = a_dur, a_dstd
    else:
        exp_dur, std_dur = GLOBAL_AVG_DUR_MIN, GLOBAL_STD_DUR_MIN

    # Флаг: используем ли только глобальные средние
    using_global = (home_stats is None and away_stats is None)

    return {
        "exp_kills":    round(exp_kills, 1),
        "std_kills":    round(max(std_kills, 3.0), 1),
        "exp_dur":      round(exp_dur, 1),
        "std_dur":      round(max(std_dur, 4.0), 1),
        "using_global": using_global,
    }


# ── Классификация рынка BetsAPI ───────────────────────────────────────────────

def classify_market(market_key: str, line: float) -> str | None:
    """
    Определяет тип тотала по ключу рынка и значению линии.
    Возвращает: 'kills' | 'duration' | 'maps' | None
    """
    key_lower = market_key.lower()

    # Карты (серия): line = 1.5 или 2.5
    if line in (1.5, 2.5):
        return "maps"

    # Убийства: типичная линия 15–80
    kill_keywords = ["kill", "frag", "total_kill"]
    if any(k in key_lower for k in kill_keywords) and 15 <= line <= 80:
        return "kills"

    # Длительность: линия 20–70 мин
    dur_keywords = ["duration", "time", "minute", "length"]
    if any(k in key_lower for k in dur_keywords) and 20 <= line <= 70:
        return "duration"

    # Эвристика только по значению линии (когда ключ неинформативен)
    if 15 <= line <= 80:
        return "kills"   # По умолчанию — тотал убийств
    if 20 <= line <= 70:
        return "duration"

    return None


def parse_totals_from_odds(odds_data: dict) -> list[dict]:
    """
    Извлекает все тотальные рынки из odds_data BetsAPI.
    Возвращает список:
      {market, market_type, line, over_odds, under_odds, bookmaker}
    """
    results = odds_data.get("results", {})
    if not isinstance(results, dict):
        return []

    found: list[dict] = []
    for bm_name, bm_data in results.items():
        if not isinstance(bm_data, dict):
            continue
        for market_key, mdata in bm_data.items():
            if not isinstance(mdata, dict):
                continue
            olist    = mdata.get("odds", [])
            handicap = mdata.get("handicap") or mdata.get("header") or mdata.get("line")
            if handicap is None or not isinstance(olist, list) or len(olist) < 2:
                continue
            try:
                line = float(handicap)
            except (TypeError, ValueError):
                continue

            market_type = classify_market(market_key, line)
            if not market_type:
                continue

            try:
                def _f(x):
                    return float(x["odds"] if isinstance(x, dict) else x)
                o_over  = _f(olist[0])
                o_under = _f(olist[1])
                if o_over <= 1.01 or o_under <= 1.01:
                    continue
            except Exception:
                continue

            found.append({
                "market":      market_key,
                "market_type": market_type,
                "line":        line,
                "over_odds":   round(o_over, 4),
                "under_odds":  round(o_under, 4),
                "bookmaker":   bm_name,
            })

    return found
