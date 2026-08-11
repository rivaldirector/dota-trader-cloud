"""Технические индикаторы на pandas, без внешних TA-библиотек."""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(series: pd.Series, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(period).std()
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + mult * std, "bb_lower": mid - mult * std})


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет к OHLCV все индикаторы, которые использует стратегия."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], 20)
    out["ema_slow"] = ema(out["close"], 50)
    out["ema_trend"] = ema(out["close"], 200)
    out["rsi"] = rsi(out["close"], 14)
    out = out.join(macd(out["close"]))
    out["atr"] = atr(out, 14)
    out = out.join(bollinger(out["close"]))
    return out
