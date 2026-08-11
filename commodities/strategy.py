"""Генератор сигналов: тренд по EMA + подтверждения RSI/MACD/Bollinger, уровни по ATR.

Логика (симметрична для BUY и SELL):
  1. Фильтр тренда: цена и EMA50 относительно EMA200 задают разрешённое направление.
  2. Очки подтверждения (score 0..5):
     - EMA20 выше/ниже EMA50 (импульс совпадает с трендом)
     - пересечение MACD с сигнальной линией в сторону сделки на последних барах
     - RSI вышел из зоны отката (для BUY: поднялся из <=45; для SELL: опустился из >=55)
     - гистограмма MACD растёт/падает три бара подряд
     - цена оттолкнулась от нижней/верхней полосы Боллинджера за последние бары
  3. Сигнал выдаётся, если score >= min_score. Стоп и цель — от ATR.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime

import pandas as pd

from .config import SYMBOL_NAMES, settings
from .indicators import add_indicators

MACD_CROSS_WINDOW = 3   # свежесть пересечения MACD, баров
BB_TOUCH_WINDOW = 5     # окно касания полосы Боллинджера, баров


@dataclass
class Signal:
    symbol: str
    name: str
    side: str            # BUY / SELL
    time: str            # время бара (UTC)
    price: float         # цена входа (закрытие бара)
    stop: float
    target: float
    atr: float
    score: int
    max_score: int
    reasons: list[str] = field(default_factory=list)
    position_size: float = 0.0   # объём в единицах инструмента под риск risk_pct

    @property
    def rr(self) -> float:
        risk = abs(self.price - self.stop)
        return abs(self.target - self.price) / risk if risk else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rr"] = round(self.rr, 2)
        return d

    def render(self) -> str:
        arrow = "🟢 ПОКУПКА" if self.side == "BUY" else "🔴 ПРОДАЖА"
        lines = [
            f"{arrow} — {self.name} ({self.symbol})",
            f"Время бара: {self.time} UTC",
            f"Вход:  {self.price:.2f}",
            f"Стоп:  {self.stop:.2f}",
            f"Цель:  {self.target:.2f}  (R:R = 1:{self.rr:.1f})",
            f"Сила сигнала: {self.score}/{self.max_score}",
            f"Объём под риск {settings.risk_pct:.1%} счёта: {self.position_size:.2f}",
            "Подтверждения: " + "; ".join(self.reasons),
        ]
        return "\n".join(lines)


def _macd_crossed(df: pd.DataFrame, up: bool, window: int = MACD_CROSS_WINDOW) -> bool:
    hist = df["hist"].tail(window + 1)
    if len(hist) < 2:
        return False
    crossed = (hist.shift(1) <= 0) & (hist > 0) if up else (hist.shift(1) >= 0) & (hist < 0)
    return bool(crossed.tail(window).any())


def _rsi_recovering(df: pd.DataFrame, up: bool, window: int = BB_TOUCH_WINDOW) -> bool:
    recent = df["rsi"].tail(window + 1)
    if up:
        return bool((recent.iloc[:-1] <= 45).any() and recent.iloc[-1] > recent.iloc[:-1].min())
    return bool((recent.iloc[:-1] >= 55).any() and recent.iloc[-1] < recent.iloc[:-1].max())


def _hist_momentum(df: pd.DataFrame, up: bool) -> bool:
    h = df["hist"].tail(3)
    if len(h) < 3:
        return False
    diffs = h.diff().dropna()
    return bool((diffs > 0).all()) if up else bool((diffs < 0).all())


def _bb_rejection(df: pd.DataFrame, up: bool, window: int = BB_TOUCH_WINDOW) -> bool:
    recent = df.tail(window)
    if up:
        touched = (recent["low"] <= recent["bb_lower"]).any()
        return bool(touched and recent["close"].iloc[-1] > recent["bb_lower"].iloc[-1])
    touched = (recent["high"] >= recent["bb_upper"]).any()
    return bool(touched and recent["close"].iloc[-1] < recent["bb_upper"].iloc[-1])


def evaluate(symbol: str, df: pd.DataFrame, min_score: int | None = None) -> Signal | None:
    """Возвращает сигнал по последнему бару или None."""
    min_score = settings.min_score if min_score is None else min_score
    if len(df) < 210:
        return None
    ind = df if "ema_trend" in df.columns else add_indicators(df)
    last = ind.iloc[-1]
    if pd.isna(last["ema_trend"]) or pd.isna(last["atr"]) or last["atr"] <= 0:
        return None

    uptrend = last["close"] > last["ema_trend"] and last["ema_slow"] > last["ema_trend"]
    downtrend = last["close"] < last["ema_trend"] and last["ema_slow"] < last["ema_trend"]
    if not uptrend and not downtrend:
        return None
    up = uptrend

    checks: list[tuple[bool, str]] = [
        (
            (last["ema_fast"] > last["ema_slow"]) if up else (last["ema_fast"] < last["ema_slow"]),
            "импульс EMA20/EMA50 в сторону сделки",
        ),
        (_macd_crossed(ind, up), "свежее пересечение MACD"),
        (_rsi_recovering(ind, up), "RSI разворачивается после отката"),
        (_hist_momentum(ind, up), "гистограмма MACD ускоряется"),
        (_bb_rejection(ind, up), "отбой от полосы Боллинджера"),
    ]
    score = sum(1 for ok, _ in checks if ok)
    if score < min_score:
        return None

    price = float(last["close"])
    atr_val = float(last["atr"])
    stop = price - settings.atr_stop_mult * atr_val if up else price + settings.atr_stop_mult * atr_val
    target = price + settings.atr_target_mult * atr_val if up else price - settings.atr_target_mult * atr_val
    risk_amount = settings.account_size * settings.risk_pct
    position_size = risk_amount / (settings.atr_stop_mult * atr_val)

    bar_time = ind.index[-1]
    time_str = bar_time.strftime("%Y-%m-%d %H:%M") if isinstance(bar_time, (pd.Timestamp, datetime)) else str(bar_time)

    return Signal(
        symbol=symbol,
        name=SYMBOL_NAMES.get(symbol, symbol),
        side="BUY" if up else "SELL",
        time=time_str,
        price=round(price, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        atr=round(atr_val, 2),
        score=score,
        max_score=len(checks),
        reasons=[label for ok, label in checks if ok],
        position_size=round(position_size, 2),
    )
