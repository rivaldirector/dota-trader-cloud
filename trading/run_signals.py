"""Fetch public BTCUSDT perpetual data and print a research signal."""
from __future__ import annotations

import argparse
import json
import time

from trading.bybit_public import BybitPublicClient
from trading.signal_engine import generate_signal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    client = BybitPublicClient()
    while True:
        try:
            candles = client.klines(args.symbol, args.interval, args.limit)
            ticker = client.ticker(args.symbol)
            signal = generate_signal(candles, ticker)
            print(json.dumps({
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "symbol": args.symbol,
                "interval": args.interval,
                "closed_candles": len(candles),
                **signal,
            }, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({"error": str(exc), "symbol": args.symbol}))
        if args.once:
            break
        time.sleep(max(5, args.poll_seconds))


if __name__ == "__main__":
    main()
