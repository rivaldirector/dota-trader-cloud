import unittest
from unittest.mock import patch

from trading import backtest


def make_candles(count=45, price=100.0):
    return [
        {"start": i * 300_000, "open": price, "high": price * 1.001,
         "low": price * 0.999, "close": price, "volume": 100.0,
         "turnover": price * 100.0}
        for i in range(count)
    ]


class BacktestTests(unittest.TestCase):
    def test_short_history_returns_zero_trades(self):
        result = backtest.run_backtest(make_candles(20))
        self.assertEqual(result["metrics"]["trades"], 0)
        self.assertEqual(result["metrics"]["final_equity"], 10.0)

    def test_minimum_notional_blocks_too_small_position(self):
        with patch.object(backtest, "generate_signal", return_value={"signal": "LONG"}):
            result = backtest.run_backtest(make_candles(), min_notional=1000.0)
        self.assertEqual(result["metrics"]["trades"], 0)

    def test_stop_wins_when_stop_and_target_hit_same_bar(self):
        candles = make_candles()
        candles[31]["high"] = 102.0
        candles[31]["low"] = 98.0
        with patch.object(backtest, "generate_signal", return_value={"signal": "LONG"}):
            result = backtest.run_backtest(
                candles, stop_pct=0.005, target_pct=0.0075,
                spread_bps=0.0, slippage_bps=0.0, taker_fee=0.0,
                min_notional=1.0, max_trades=1,
            )
        self.assertEqual(result["trades_detail"][0]["exit_reason"], "stop")

    def test_fees_are_deducted_from_net_pnl(self):
        candles = make_candles()
        candles[31]["high"] = 103.0
        with patch.object(backtest, "generate_signal", return_value={"signal": "LONG"}):
            result = backtest.run_backtest(
                candles, stop_pct=0.005, target_pct=0.0075,
                spread_bps=0.0, slippage_bps=0.0, taker_fee=0.00055,
                min_notional=1.0, max_trades=1,
            )
        trade = result["trades_detail"][0]
        self.assertGreater(trade["fees"], 0)
        self.assertLess(trade["net_pnl"], trade["gross_pnl"])


if __name__ == "__main__":
    unittest.main()
