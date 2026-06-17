"""
Elo-based team rating для Dota 2.

Улучшения v2:
  - Tier-weighted K: победа на TI/Major весит больше чем на региональном квале
  - Time decay: матчи старше 365 дней весят меньше (через пересчёт с decay)
  - Форма: last5 / last10 остаётся как дополнительный признак

Tier → K multiplier:
  S (TI, Major)         → K × 1.5
  A (DPC League, ESL)   → K × 1.2
  B (Regional qualifier) → K × 1.0
  C (online cup)         → K × 0.7
  unknown               → K × 1.0
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from math import exp


BASE_K = 32

# Ключевые слова для tier-определения по названию лиги
TIER_S_KEYWORDS = [
    "the international", " ti ", "ti10", "ti11", "ti12", "ti13",
    "major", "dpc", "blast", "esl one", "dreamleague",
]
TIER_A_KEYWORDS = [
    "esl", "dreamhack", "dpc", "regional league", "division",
    "xtreme gaming", "betboom", "riyadh",
]
TIER_C_KEYWORDS = [
    "qualifier", "closed qualifier", "open qualifier",
    "showmatch", "show match", "cup", "series",
]

K_MULTIPLIERS = {"S": 1.5, "A": 1.2, "B": 1.0, "C": 0.7}


def _tier_k(league_name: str) -> float:
    name = (league_name or "").lower()
    for kw in TIER_S_KEYWORDS:
        if kw in name:
            return BASE_K * K_MULTIPLIERS["S"]
    for kw in TIER_A_KEYWORDS:
        if kw in name:
            return BASE_K * K_MULTIPLIERS["A"]
    for kw in TIER_C_KEYWORDS:
        if kw in name:
            return BASE_K * K_MULTIPLIERS["C"]
    return BASE_K * K_MULTIPLIERS["B"]


def _time_weight(begin_at: str, now: datetime, half_life_days: int = 365) -> float:
    """
    Экспоненциальный decay: матч год назад весит 0.5, два года — 0.25.
    Отключить можно передав half_life_days=0.
    """
    if not begin_at or half_life_days == 0:
        return 1.0
    try:
        dt = datetime.fromisoformat(begin_at.replace("Z", "+00:00"))
        days_ago = (now - dt).days
        return 0.5 ** (days_ago / half_life_days)
    except Exception:
        return 1.0


def logistic(x: float) -> float:
    return 1.0 / (1.0 + exp(-x))


def build_team_ratings(db, use_tier_k: bool = True,
                       half_life_days: int = 365) -> dict:
    """
    Строит рейтинги командам по всем finished матчам.

    Параметры:
      use_tier_k     — взвешивать K по tier турнира
      half_life_days — период полураспада для time decay (0 = без decay)
    """
    rows = db.fetchall("""
    SELECT begin_at, name, league_tier,
           team_1_name, team_2_name, winner_name
    FROM matches
    WHERE status='finished'
      AND team_1_name IS NOT NULL
      AND team_2_name IS NOT NULL
      AND winner_name IS NOT NULL
    ORDER BY begin_at ASC
    """)

    now = datetime.now(timezone.utc)
    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    history: dict = defaultdict(lambda: deque(maxlen=10))
    matches_count: dict[str, int] = defaultdict(int)
    wins_count: dict[str, int]    = defaultdict(int)

    for r in rows:
        t1, t2, winner = r["team_1_name"], r["team_2_name"], r["winner_name"]
        league_name = r["name"] or ""
        tier_str    = (r["league_tier"] or "").upper() if "league_tier" in r.keys() else ""

        # K
        if use_tier_k:
            if tier_str in K_MULTIPLIERS:
                k = BASE_K * K_MULTIPLIERS[tier_str]
            else:
                k = _tier_k(league_name)
        else:
            k = float(BASE_K)

        # Time decay
        w = _time_weight(r["begin_at"], now, half_life_days)
        k = k * w

        r1, r2 = elo[t1], elo[t2]
        e1 = 1.0 / (1.0 + 10.0 ** ((r2 - r1) / 400.0))
        e2 = 1.0 - e1

        s1 = 1 if winner == t1 else 0
        s2 = 1 - s1

        elo[t1] = r1 + k * (s1 - e1)
        elo[t2] = r2 + k * (s2 - e2)

        history[t1].append(s1)
        history[t2].append(s2)
        matches_count[t1] += 1
        matches_count[t2] += 1
        wins_count[t1] += s1
        wins_count[t2] += s2

    ratings = {}
    for team in matches_count:
        results = list(history[team])
        ratings[team] = {
            "team":    team,
            "elo":     elo[team],
            "matches": matches_count[team],
            "wins":    wins_count[team],
            "winrate": wins_count[team] / matches_count[team],
            "last5":   sum(results[-5:]) / min(5, len(results)) if results else 0.5,
            "last10":  sum(results) / len(results) if results else 0.5,
        }

    return ratings


def predict_team_a_win(rating_a: dict, rating_b: dict) -> float:
    """
    Вероятность победы team_a.
    Комбинирует Elo-разницу и форму (last5/last10).
    """
    elo_component = (rating_a["elo"] - rating_b["elo"]) / 400.0
    form_component = (
        (rating_a["last5"]  - rating_b["last5"])  * 1.5 +
        (rating_a["last10"] - rating_b["last10"]) * 0.75
    )
    return logistic(elo_component + form_component)


def find_team(ratings: dict, query: str) -> str | None:
    q = query.lower().strip()
    for name in ratings:
        if name.lower() == q:
            return name
    found = [name for name in ratings if q in name.lower()]
    if len(found) == 1:
        return found[0]
    return None


def top_teams(ratings: dict, n: int = 20) -> list[dict]:
    """Топ-N команд по Elo."""
    return sorted(ratings.values(), key=lambda r: -r["elo"])[:n]
