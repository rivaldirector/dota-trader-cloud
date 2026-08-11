"""Бэктест стратегии: проход по истории, симуляция сделок со стопом и целью."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import settings
from .indicators import add_indicators
from .strategy import evaluate

WARMUP_BARS = 210  # минимум истории для EMA200 и стабильного ATR


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: str
    entry: float
    stop: float
    target: float
    exit_time: str = ""
    exit: float = 0.0
    result: str = ""   # target / stop / eod
    pnl_r: float = 0.0  # результат в R (кратно риску)


@dataclass
class BacktestReport:
    symbol: str
    trades: list[Trade]

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def winrate(self) -> float:
        return sum(1 for t in self.trades if t.pnl_r > 0) / self.n if self.n else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_r for t in self.trades if t.pnl_r > 0)
        losses = -sum(t.pnl_r for t in self.trades if t.pnl_r < 0)
        return gains / losses if losses else float("inf")

    @property
    def max_drawdown_r(self) -> float:
        equity = peak = dd = 0.0
        for t in self.trades:
            equity += t.pnl_r
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        return dd

    def render(self) -> str:
        if not self.n:
            return f"{self.symbol}: сигналов за период не было"
        return (
            f"{self.symbol}: сделок {self.n}, winrate {self.winrate:.0%}, "
            f"итог {self.total_r:+.1f}R, profit factor {self.profit_factor:.2f}, "
            f"макс. просадка {self.max_drawdown_r:.1f}R"
        )


def run_backtest(symbol: str, df: pd.DataFrame, min_score: int | None = None) -> BacktestReport:
    """Идёт по барам; вход по сигналу на закрытии бара, выход по стопу/цели на следующих барах."""
    ind = add_indicators(df)
    trades: list[Trade] = []
    open_trade: Trade | None = None

    for i in range(WARMUP_BARS, len(ind)):
        bar = ind.iloc[i]
        bar_time = str(ind.index[i])

        if open_trade is not None:
            long = open_trade.side == "BUY"
            hit_stop = bar["low"] <= open_trade.stop if long else bar["high"] >= open_trade.stop
            hit_target = bar["high"] >= open_trade.target if long else bar["low"] <= open_trade.target
            # Консервативно: если в одном баре достижимы и стоп, и цель — считаем стоп.
            if hit_stop:
                open_trade.exit, open_trade.result, open_trade.pnl_r = open_trade.stop, "stop", -1.0
            elif hit_target:
                risk = abs(open_trade.entry - open_trade.stop)
                open_trade.exit = open_trade.target
                open_trade.result = "target"
                open_trade.pnl_r = abs(open_trade.target - open_trade.entry) / risk
            if open_trade.result:
                open_trade.exit_time = bar_time
                trades.append(open_trade)
                open_trade = None
            continue  # одна позиция за раз; новый вход не раньше следующего бара

        window = ind.iloc[: i + 1]
        sig = evaluate(symbol, window, min_score=min_score)
        if sig is not None:
            open_trade = Trade(
                symbol=symbol,
                side=sig.side,
                entry_time=bar_time,
                entry=sig.price,
                stop=sig.stop,
                target=sig.target,
            )

    if open_trade is not None:
        # Закрываем незавершённую сделку по последнему close.
        last_close = float(ind["close"].iloc[-1])
        risk = abs(open_trade.entry - open_trade.stop)
        signed = (last_close - open_trade.entry) if open_trade.side == "BUY" else (open_trade.entry - last_close)
        open_trade.exit = last_close
        open_trade.exit_time = str(ind.index[-1])
        open_trade.result = "eod"
        open_trade.pnl_r = signed / risk if risk else 0.0
        trades.append(open_trade)

    return BacktestReport(symbol=symbol, trades=trades)
