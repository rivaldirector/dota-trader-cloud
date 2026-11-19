# Bybit public signal research MVP

This module is isolated from the existing esports betting engine. It reads public Bybit V5 market data and produces diagnostic LONG/SHORT/FLAT signals. It does not require an API key and cannot place orders.

## Run

```bash
python -m trading.run_signals --symbol BTCUSDT --interval 1 --once
python -m unittest tests.test_bybit_signal_engine -v
```

The first version uses closed 1-minute candles and the public ticker. Signal rules are experimental: EMA(9/21) trend alignment, 20-candle VWAP, RSI(14), and a spread filter. These are hypotheses to evaluate, not a profitability claim.

## Safety

- No private API credentials are read.
- No order, position, wallet, or leverage endpoints are called.
- Execution is hard-coded as DISABLED in signal output.
- Do not treat a signal as a trade recommendation.
- Before any paper/live execution, add fees, slippage, instrument filters, position sizing, order-state reconciliation, and independent tests.
