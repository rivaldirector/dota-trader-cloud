import unittest

from trading.signal_engine import generate_signal


def candles_from_closes(closes):
    return [
        {
            "start": i * 60_000,
            "open": p,
            "high": p * 1.001,
            "low": p * 0.999,
            "close": p,
            "volume": 10.0,
            "turnover": p * 10.0,
        }
        for i, p in enumerate(closes)
    ]


class SignalEngineTests(unittest.TestCase):
    def test_insufficient_history_is_flat(self):
        result = generate_signal(candles_from_closes([100.0] * 20), {"bid": 99.99, "ask": 100.01})
        self.assertEqual(result["signal"], "FLAT")
        self.assertEqual(result["reason"], "insufficient_closed_candles")

    def test_wide_spread_blocks_signal(self):
        result = generate_signal(
            candles_from_closes([100.0 + i * 0.1 for i in range(40)]),
            {"bid": 99.0, "ask": 101.0},
        )
        self.assertEqual(result["signal"], "FLAT")
        self.assertEqual(result["reason"], "spread_too_wide")

    def test_output_never_enables_execution(self):
        result = generate_signal(
            candles_from_closes([100.0 + i * 0.1 for i in range(40)]),
            {"bid": 103.89, "ask": 103.91},
        )
        self.assertEqual(result["execution"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
