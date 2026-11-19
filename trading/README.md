# Bybit public signal research MVP

This module is isolated from the existing esports betting engine. It reads public Bybit V5 market data and produces diagnostic LONG/SHORT/FLAT signals. It does not require an API key and cannot place orders.

## Live public signal monitor

```bash
python -m trading.run_signals --symbol BTCUSDT --interval 1 --once
python -m trading.run_signals --symbol BTCUSDT --interval 1 --poll-seconds 30
``

Signal rules are experimental: EMA(9/21) trend alignment, 20-candle VWAP, RSI(14), and a spread filter. They are hypotheses to evaluate, not a profitability claim.

## Historical backtest

```bash
# About 5-minute bars; 50,000 bars is roughly 5.8 months
python -m trading.backtest --symbol BTCUSDT --interval 5 --bars 50000 --equity 10

# Shorter 1-minute sample; 50,000 bars is roughly 35 days
python -m trading.backtest --symbol BTCUSDT --interval 1 --bars 50000 --equity 10
```

The downloader paginates the public V5 kline endpoint and excludes the still-forming candle. The backtest evaluates a signal at candle close and enters at the next candle open. It assumes the stop is hit first if stop and target are both inside one candle. It charges taker fees on both sides plus configurable spread and slippage. It reads the current instrument minimum notional dynamically.

The report is written to `reports/bybit_backtest.json`. Review the assumptions in that report: funding and liquidation are not modelled, and OHLC bars cannot reproduce queue priority or the exact intrabar path. Do not treat these results as proof of future profitability.

## Tests

```bash
python -m unittest tests.test_bybit_signal_engine tests.test_bybit_backtest -v
```

A GitHub Actions workflow runs these deterministic unit tests on changes to this module.

## Safety

- No private API credentials are read.
- No order, position, wallet, or leverage endpoints are called.
- Signal execution remains hard-coded as DISABLED.
- Do not treat a signal as a trade recommendation.
- Live execution requires a separate implementation, review, explicit enablement, and extensive paper/Testnet validation.
