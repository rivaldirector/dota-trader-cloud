"""Тесты робота по золоту и нефти на синтетических данных (без сети)."""

import math

import numpy as np
import pandas as pd

from commodities.backtest import run_backtest
from commodities.indicators import add_indicators, atr, ema, rsi
from commodities.strategy import evaluate


def make_df(n: int = 600, trend: float = 0.05, seed: int = 7, start: float = 2000.0) -> pd.DataFrame:
    """Синтетический ряд: тренд + шум + периодические откаты."""
    rng = np.random.default_rng(seed)
    steps = trend + rng.normal(0, 2.0, n) + 3.0 * np.sin(np.arange(n) / 15)
    close = start + np.cumsum(steps)
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.5, 3.0, n)
    low = close - rng.uniform(0.5, 3.0, n)
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": rng.uniform(1e3, 1e4, n)},
        index=idx,
    )


def test_indicators_shapes_and_ranges():
    df = make_df()
    ind = add_indicators(df)
    assert {"ema_fast", "ema_slow", "ema_trend", "rsi", "macd", "signal", "hist", "atr", "bb_upper", "bb_lower"} <= set(ind.columns)
    r = rsi(df["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert (atr(df).dropna() >= 0).all()
    assert not math.isnan(ema(df["close"], 20).iloc[-1])


def test_signal_levels_consistent():
    # Перебираем сиды, пока стратегия не даст сигнал, и проверяем его геометрию.
    found = 0
    for seed in range(30):
        for trend in (1.2, -1.2):
            df = make_df(trend=trend, seed=seed)
            sig = evaluate("GC=F", add_indicators(df), min_score=2)
            if sig is None:
                continue
            found += 1
            if sig.side == "BUY":
                assert sig.stop < sig.price < sig.target
            else:
                assert sig.target < sig.price < sig.stop
            assert sig.rr > 1.0
            assert sig.position_size > 0
            assert sig.score >= 2
    assert found > 0, "стратегия не дала ни одного сигнала на 60 синтетических рядах"


def test_no_signal_on_short_history():
    df = make_df(n=100)
    assert evaluate("CL=F", df) is None


def test_backtest_produces_trades_and_valid_stats():
    df = make_df(n=900, trend=0.8, seed=3)
    report = run_backtest("GC=F", df, min_score=2)
    assert report.n > 0, "бэктест не дал ни одной сделки"
    for t in report.trades:
        assert t.result in {"stop", "target", "eod"}
        if t.result == "stop":
            assert t.pnl_r == -1.0
        if t.result == "target":
            assert t.pnl_r > 0
    assert 0.0 <= report.winrate <= 1.0
    assert report.max_drawdown_r >= 0
