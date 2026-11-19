"""Research-only momentum signal engine. It never places orders."""
from __future__ import annotations

from math import isfinite
from typing import Sequence


def _ema(values: Sequence[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values for EMA")
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) <= period:
        raise ValueError(f"Need more than {period} values for RSI")
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _vwap(candles: Sequence[dict]) -> float:
    volume = sum(float(c["volume"]) for c in candles)
    if volume <= 0:
        return 0.0
    return sum(
        ((float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0)
        * float(c["volume"])
        for c in candles
    ) / volume


def generate_signal(
    candles: Sequence[dict],
    ticker: dict[str, float],
    *,
    max_spread_bps: float = 8.0,
) -> dict:
    """Return LONG/SHORT/FLAT with diagnostics; only closed candles are expected."""
    if len(candles) < 30:
        return {"signal": "FLAT", "reason": "insufficient_closed_candles"}

    closes = [float(c["close"]) for c in candles]
    if not all(isfinite(x) and x > 0 for x in closes):
        return {"signal": "FLAT", "reason": "invalid_price_data"}

    bid, ask = float(ticker["bid"]), float(ticker["ask"])
    mid = (bid + ask) / 2.0
    if bid <= 0 or ask <= 0 or ask < bid or mid <= 0:
        return {"signal": "FLAT", "reason": "invalid_quote"}
    spread_bps = (ask - bid) / mid * 10_000
    if spread_bps > max_spread_bps:
        return {
            "signal": "FLAT", "reason": "spread_too_wide",
            "spread_bps": round(spread_bps, 2),
        }

    ema_fast = _ema(closes[-60:], 9)
    ema_slow = _ema(closes[-60:], 21)
    rsi = _rsi(closes[-30:], 14)
    vwap = _vwap(candles[-20:])
    last = closes[-1]

    direction = "FLAT"
    reasons = []
    if ema_fast > ema_slow and last > vwap and 50 <= rsi <= 70:
        direction = "LONG"
        reasons = ["ema9_above_ema21", "close_above_20bar_vwap", "rsi_momentum_positive"]
    elif ema_fast < ema_slow and last < vwap and 30 <= rsi <= 50:
        direction = "SHORT"
        reasons = ["ema9_below_ema21", "close_below_20bar_vwap", "rsi_momentum_negative"]
    else:
        reasons = ["momentum_conditions_not_aligned"]

    return {
        "signal": direction,
        "reason": reasons,
        "last_price": round(last, 8),
        "ema9": round(ema_fast, 8),
        "ema21": round(ema_slow, 8),
        "vwap20": round(vwap, 8),
        "rsi14": round(rsi, 2),
        "spread_bps": round(spread_bps, 2),
        "candle_start_ms": int(candles[-1]["start"]),
        "execution": "DISABLED",
    }
