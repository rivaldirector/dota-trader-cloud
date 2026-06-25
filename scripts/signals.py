#!/usr/bin/env python3
"""
signals.py — общий модуль сигналов для принятия решений по ставкам.

Используется как библиотека из elo_auto_bet.py (и будущих скриптов).
Не запускается напрямую.

Содержит:
  compute_form()          — форма команды: win rate за последние N матчей
  compute_h2h()           — H2H: win rate против конкретного оппонента
  composite_prob()        — ансамбль elo + form + h2h → итоговая вероятность
  kelly_stake()           — Kelly criterion с fraction и cap
  adaptive_kelly_mult()   — адаптивный множитель Kelly по rolling ROI
                            (ЗАМЕНА стоп-лоссу: никогда не останавливает
                             полностью, только плавно снижает размер)
  get_league_tier()       — тир лиги из league_tiers
  get_tier_params()       — (edge_min, kelly_cap) для тира

Философия адаптивного Kelly vs стоп-лосс:
  Жёсткий стоп-лосс убивает хорошие ставки после серии неудач — просадка
  может быть просто дисперсией, а не сломанной моделью. Адаптивный Kelly
  реагирует плавно: при плохой серии уменьшает ставки, но не нулит их.
  Это математически оптимально: Kelly уже встроен в теорему об оптимальном
  росте капитала (Kelly = максимум E[log(bankroll)]). Снижение fraction
  при неопределённости — это bayesian conservative update.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

# ─── Константы по умолчанию ─────────────────────────────────────────────────

# Веса ансамбля: ело доминирует, форма и H2H — корректировки
ENSEMBLE_WEIGHTS = {
    "elo":  0.60,
    "form": 0.25,
    "h2h":  0.15,
}

# Fractional Kelly — применяется ВСЕГДА поверх full Kelly
KELLY_FRACTION_DEFAULT = 0.25

# Максимальная доля банка на одну ставку
KELLY_CAP_DEFAULT = 0.05   # 5%

# Минимальный edge по умолчанию (если тир неизвестен)
EDGE_MIN_DEFAULT = 0.03    # 3%

# Нижний порог Kelly-множителя (всегда ставим хоть что-то, если edge есть)
ADAPTIVE_KELLY_MIN = 0.15

# Окно для rolling ROI
ADAPTIVE_WINDOW = 20


# ─── Форма команды (last-N win rate) ────────────────────────────────────────

def normalize_team(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def fuzzy_match(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def compute_form(
    team: str,
    history: list[tuple[int, str, str, float]],  # (start_time, home, away, act_h)
    n: int = 10,
    fuzzy_min: float = 0.72,
) -> Optional[float]:
    """
    Win rate команды за последние n матчей из history.
    history — список (start_time, home, away, actual_home_win 1.0/0.0),
    уже отсортированный по start_time ASC.

    Возвращает float [0.0–1.0] или None если < 3 матчей найдено.
    """
    team_n = normalize_team(team)
    results = []

    for _st, home, away, act_h in reversed(history):
        if len(results) >= n:
            break
        home_n = normalize_team(home)
        away_n = normalize_team(away)

        # Fuzzy match по нормализованным именам
        score_home = fuzzy_match(team_n, home_n)
        score_away = fuzzy_match(team_n, away_n)

        if score_home >= fuzzy_min and score_home >= score_away:
            results.append(1.0 if act_h >= 0.5 else 0.0)
        elif score_away >= fuzzy_min:
            results.append(0.0 if act_h >= 0.5 else 1.0)

    if len(results) < 3:
        return None
    return round(sum(results) / len(results), 4)


# ─── H2H ────────────────────────────────────────────────────────────────────

def compute_h2h(
    our_team: str,
    opponent: str,
    history: list[tuple[int, str, str, float]],
    n: int = 8,
    fuzzy_min: float = 0.72,
) -> Optional[float]:
    """
    Win rate our_team против opponent за последние n личных встреч.
    Возвращает float [0.0–1.0] или None если < 2 встреч найдено.
    """
    our_n = normalize_team(our_team)
    opp_n = normalize_team(opponent)
    results = []

    for _st, home, away, act_h in reversed(history):
        if len(results) >= n:
            break
        home_n = normalize_team(home)
        away_n = normalize_team(away)

        home_is_our  = fuzzy_match(our_n, home_n) >= fuzzy_min
        away_is_our  = fuzzy_match(our_n, away_n) >= fuzzy_min
        home_is_opp  = fuzzy_match(opp_n, home_n) >= fuzzy_min
        away_is_opp  = fuzzy_match(opp_n, away_n) >= fuzzy_min

        if home_is_our and away_is_opp:
            results.append(1.0 if act_h >= 0.5 else 0.0)
        elif away_is_our and home_is_opp:
            results.append(0.0 if act_h >= 0.5 else 1.0)

    if len(results) < 2:
        return None
    return round(sum(results) / len(results), 4)


# ─── Ансамбль ────────────────────────────────────────────────────────────────

def composite_prob(
    elo_prob: float,
    form_score: Optional[float],
    h2h_score: Optional[float],
    weights: dict[str, float] = ENSEMBLE_WEIGHTS,
    fatigue_adj: float = 0.0,
) -> float:
    """
    Взвешенный ансамбль вероятностей + поправка на усталость.

    elo_prob    — вероятность победы по Elo (0.0–1.0)
    form_score  — win rate за последние N матчей (или None)
    h2h_score   — H2H win rate (или None)
    weights     — веса из model_config или ENSEMBLE_WEIGHTS
    fatigue_adj — корректировка из fatigue_adjustment() (±0.05 max)

    Возвращает вероятность (0.01–0.99), rounded 4.
    """
    w_elo  = weights.get("elo",  0.60)
    w_form = weights.get("form", 0.25)
    w_h2h  = weights.get("h2h",  0.15)

    total_w  = w_elo
    weighted = w_elo * elo_prob

    if form_score is not None:
        total_w  += w_form
        weighted += w_form * form_score
    if h2h_score is not None:
        total_w  += w_h2h
        weighted += w_h2h * h2h_score

    base = weighted / total_w
    adjusted = base + fatigue_adj
    return round(min(0.99, max(0.01, adjusted)), 4)


# ─── Kelly sizing ────────────────────────────────────────────────────────────

def kelly_stake(
    p: float,
    odds: float,
    bankroll: float,
    fraction: float = KELLY_FRACTION_DEFAULT,
    cap: float = KELLY_CAP_DEFAULT,
    adaptive_mult: float = 1.0,
) -> float:
    """
    Размер ставки по Kelly criterion.

    p        — вероятность победы (composite_prob)
    odds     — десятичные коэффициенты (напр. 1.85)
    bankroll — текущий банк ($)
    fraction — дробный Kelly (0.25 = conservative, 0.5 = moderate)
    cap      — максимальная доля банка (0.05 = 5%)
    adaptive_mult — множитель из adaptive_kelly_mult() (0.15–1.0)

    Возвращает размер ставки в $, округлённый до 1$.
    Возвращает 0.0 если Kelly отрицательный (нет edge).
    """
    if odds <= 1.0 or p <= 0.0 or p >= 1.0:
        return 0.0

    b = odds - 1.0          # чистая прибыль на единицу ставки
    q = 1.0 - p             # вероятность проигрыша
    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        return 0.0          # отрицательный Kelly = нет edge = не ставим

    f = full_kelly * fraction * adaptive_mult
    f = min(f, cap)         # жёсткий кэп от банка

    stake = round(bankroll * f, 1)
    return max(stake, 1.0)  # минимум $1


# ─── Адаптивный Kelly-множитель (замена стоп-лосса) ─────────────────────────

def adaptive_kelly_mult(
    recent_settled: list[dict],
    window: int = ADAPTIVE_WINDOW,
) -> float:
    """
    Множитель [ADAPTIVE_KELLY_MIN .. 1.0] на основе rolling ROI
    за последние `window` урегулированных ставок.

    Логика:
      ROI >= 0%          → 1.00  (полный Kelly, всё хорошо)
      ROI -5% .. 0%      → 0.75  (лёгкое снижение)
      ROI -10% .. -5%    → 0.50  (умеренное снижение)
      ROI -20% .. -10%   → 0.25  (существенное снижение)
      ROI < -20%         → 0.15  (минимум — НИКОГДА не 0)

    Почему не стоп-лосс:
      После просадки рынок не становится эффективнее — вероятно, это
      просто дисперсия. Полная остановка = пропускаем следующую
      выигрышную серию. Снижение size сохраняет участие при контроле риска.

    recent_settled — последние N settled ставок из elo_paper_bets,
    каждая dict с полями 'stake_usd', 'pnl', 'outcome', 'real_odds'.
    """
    bets = [b for b in recent_settled if b.get("settled") and b.get("outcome")]
    bets = bets[-window:]

    if len(bets) < 5:
        return 0.50   # bootstrap: мало данных → консервативно

    total_staked = sum(float(b.get("stake_usd") or 20) for b in bets)
    if total_staked <= 0:
        return 0.50

    total_pnl = sum(_effective_pnl_simple(b) for b in bets)
    roi = total_pnl / total_staked

    if roi >= 0.0:
        return 1.00
    elif roi >= -0.05:
        return 0.75
    elif roi >= -0.10:
        return 0.50
    elif roi >= -0.20:
        return 0.25
    else:
        return ADAPTIVE_KELLY_MIN   # 0.15 — никогда не 0


def _effective_pnl_simple(b: dict) -> float:
    """P&L с real_odds если есть, иначе из поля pnl."""
    outcome = b.get("outcome")
    stake_v = float(b.get("stake_usd") or 20)
    real_o  = b.get("real_odds")
    if real_o and outcome:
        return round(stake_v * (float(real_o) - 1), 2) if outcome == "win" else -stake_v
    return float(b.get("pnl") or 0)


# ─── League tiers ────────────────────────────────────────────────────────────

def get_league_tier(league_name: str | None, tiers: list[dict]) -> int:
    """
    Определяет тир лиги по паттернам из таблицы league_tiers.
    Возвращает 1, 2 или 3 (default=3 если не найдено).
    tiers — список dict с полями 'pattern', 'tier'.
    """
    if not league_name:
        return 3
    ln = league_name.lower()
    for row in sorted(tiers, key=lambda r: r.get("tier", 3)):
        pat = (row.get("pattern") or "").lower()
        if pat and pat in ln:
            return int(row.get("tier", 3))
    return 3


def get_tier_params(tier: int, tiers: list[dict]) -> tuple[float, float]:
    """
    Возвращает (edge_min, kelly_cap) для данного тира.
    Берёт первую строку с matching tier из tiers.
    Fallback: (EDGE_MIN_DEFAULT, KELLY_CAP_DEFAULT).
    """
    for row in tiers:
        if int(row.get("tier", 0)) == tier:
            return (
                float(row.get("edge_min", EDGE_MIN_DEFAULT)),
                float(row.get("kelly_cap", KELLY_CAP_DEFAULT)),
            )
    return (EDGE_MIN_DEFAULT, KELLY_CAP_DEFAULT)


# ─── Fatigue signal ──────────────────────────────────────────────────────────

def compute_fatigue(
    team: str,
    history: list[tuple[int, str, str, float]],
    match_start_ts: int,
    fuzzy_min: float = 0.72,
) -> float:
    """
    Усталость команды перед матчем: насколько недавно они играли.

    Возвращает float [0.0–1.0]:
      0.0 = отдохнувшие (7+ дней без матча или нет истории)
      1.0 = играли вчера / в тот же день

    Формула: fatigue = max(0, 1 - days_since / 7)
    """
    team_n = normalize_team(team)
    last_ts = None

    for ts, home, away, _ in reversed(history):
        if ts >= match_start_ts:
            continue  # только матчи ДО текущего
        hn = normalize_team(home)
        an = normalize_team(away)
        if fuzzy_match(team_n, hn) >= fuzzy_min or fuzzy_match(team_n, an) >= fuzzy_min:
            last_ts = ts
            break

    if last_ts is None:
        return 0.0  # нет истории → не штрафуем

    days_since = (match_start_ts - last_ts) / 86400.0
    return round(max(0.0, 1.0 - days_since / 7.0), 4)


def fatigue_adjustment(our_fatigue: float, opp_fatigue: float) -> float:
    """
    Корректировка вероятности на основе разницы усталости.
    Positive = наша команда свежее (это хорошо для нас).
    Вес: max ±5%.
    """
    diff = opp_fatigue - our_fatigue   # +1 = мы свежее на 7 дней
    return round(diff * 0.05, 4)       # каждый день разницы ≈ 0.71% к вероятности
