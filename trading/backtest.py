"""Historical Bybit candle downloader and conservative signal backtester."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Sequence

from trading.bybit_public import BybitPublicClient
from trading.signal_engine import generate_signal


def download_history(client: BybitPublicClient, symbol="BTCUSDT", interval="1", bars=60000):
    """Download up to bars candles using Bybit reverse-time pagination."""
    interval_ms = client._interval_ms(interval)
    end_ms = None
    collected = {}
    while len(collected) < bars:
        params = {"category": "linear", "symbol": symbol, "interval": interval,
                  "limit": min(1000, bars - len(collected))}
        if end_ms is not None:
            params["end"] = end_ms
        result = client._get("/v5/market/kline", params)
        rows = result.get("list", [])
        if not rows:
            break
        oldest = min(int(row[0]) for row in rows)
        for row in rows:
            candle = {"start": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                      "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                      "turnover": float(row[6])}
            if candle["start"] + interval_ms <= __import__("time").time() * 1000:
                collected[candle["start"]] = candle
        if oldest <= 0 or (end_ms is not None and oldest >= end_ms):
            break
        end_ms = oldest - 1
        if len(rows) < 2:
            break
    return sorted(collected.values(), key=lambda c: c["start"])[-bars:]


def _metrics(equity_curve, trades):
    initial = equity_curve[0] if equity_curve else 0.0
    final = equity_curve[-1] if equity_curve else initial
    peak, max_dd = initial, 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    gp = sum(t["net_pnl"] for t in wins)
    gl = abs(sum(t["net_pnl"] for t in losses))
    return {
        "initial_equity": round(initial, 8), "final_equity": round(final, 8),
        "net_pnl": round(final - initial, 8),
        "return_pct": round((final / initial - 1) * 100, 4) if initial else 0.0,
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gp / gl, 4) if gl else None,
        "expectancy_usdt": round(mean(t["net_pnl"] for t in trades), 8) if trades else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 4),
        "fees_paid": round(sum(t["fees"] for t in trades), 8),
    }


def run_backtest(candles: Sequence[dict], *, initial_equity=10.0, risk_fraction=0.01,
                 max_leverage=2.0, stop_pct=0.005, target_pct=0.0075,
                 taker_fee=0.00055, slippage_bps=2.0, spread_bps=1.0,
                 max_spread_bps=8.0, min_notional=5.0, max_trades=0):
    """Evaluate close signals, enter next open, use conservative OHLC stop/target fills."""
    if initial_equity <= 0 or not (0 < risk_fraction < 1):
        raise ValueError("Invalid equity or risk_fraction")
    if not (0 < stop_pct < 1 and 0 < target_pct < 1):
        raise ValueError("stop_pct and target_pct must be in (0,1)")
    if min(taker_fee, slippage_bps, spread_bps) < 0 or max_leverage <= 0:
        raise ValueError("Invalid fee, spread, slippage, or leverage")
    if len(candles) < 40:
        return {"metrics": _metrics([initial_equity], []), "trades_detail": []}

    equity, curve, trades = initial_equity, [initial_equity], []
    i = 30
    slip, half_spread = slippage_bps / 10000, spread_bps / 20000

    while i < len(candles) - 1:
        history = candles[:i + 1]
        close = float(history[-1]["close"])
        quote = {"bid": close * (1 - half_spread), "ask": close * (1 + half_spread)}
        signal = generate_signal(history, quote, max_spread_bps=max_spread_bps)
        side = signal["signal"]
        if side not in ("LONG", "SHORT"):
            i += 1
            continue

        direction = 1.0 if side == "LONG" else -1.0
        entry_bar = candles[i + 1]
        entry = float(entry_bar["open"]) * (1 + direction * (half_spread + slip))
        risk_per_notional = stop_pct + 2 * (taker_fee + half_spread + slip)
        notional = min(equity * risk_fraction / risk_per_notional, equity * max_leverage)
        if notional < min_notional or equity <= 0:
            i += 1
            continue

        stop = entry * (1 - direction * stop_pct)
        target = entry * (1 + direction * target_pct)
        exit_price, reason, exit_i = None, "end_of_data", len(candles) - 1
        for j in range(i + 1, len(candles)):
            high, low = float(candles[j]["high"]), float(candles[j]["low"])
            stop_hit = low <= stop if side == "LONG" else high >= stop
            target_hit = high >= target if side == "LONG" else low <= target
            if stop_hit:  # conservative: stop first if both levels touched
                exit_price = stop * (1 - direction * (half_spread + slip))
                reason, exit_i = "stop", j
                break
            if target_hit:
                exit_price = target * (1 - direction * (half_spread + slip))
                reason, exit_i = "target", j
                break
        if exit_price is None:
            exit_price = float(candles[-1]["close"]) * (1 - direction * (half_spread + slip))

        gross = notional * direction * (exit_price / entry - 1)
        fees = notional * taker_fee * 2
        net = gross - fees
        equity += net
        trades.append({"side": side, "entry_time": int(entry_bar["start"]),
                       "exit_time": int(candles[exit_i]["start"]),
                       "entry": round(entry, 8), "exit": round(exit_price, 8),
                       "notional": round(notional, 8), "gross_pnl": round(gross, 8),
                       "fees": round(fees, 8), "net_pnl": round(net, 8),
                       "exit_reason": reason})
        curve.append(equity)
        if equity <= 0 or (max_trades and len(trades) >= max_trades):
            break
        i = exit_i + 1

    return {
        "metrics": _metrics(curve, trades), "trades_detail": trades,
        "assumptions": {
            "signal_timing": "closed candle; entry next candle open",
            "same_bar_stop_target": "stop first",
            "fee_per_side": taker_fee, "slippage_bps_per_side": slippage_bps,
            "spread_bps": spread_bps, "funding": "not modelled",
            "liquidation": "not modelled",
            "fills": "all taker; OHLC approximation, no queue position or intrabar path",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Bybit public-data historical backtest")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5")
    parser.add_argument("--bars", type=int, default=50000)
    parser.add_argument("--equity", type=float, default=10.0)
    parser.add_argument("--output", default="reports/bybit_backtest.json")
    args = parser.parse_args()
    client = BybitPublicClient()
    candles = download_history(client, args.symbol, args.interval, args.bars)
    instrument = client.instrument_info(args.symbol)
    result = run_backtest(candles, initial_equity=args.equity,
                          min_notional=instrument["min_notional"])
    result["instrument"] = instrument
    result["data"] = {"symbol": args.symbol, "category": "linear",
                      "interval": args.interval, "bars": len(candles),
                      "from_ms": candles[0]["start"] if candles else None,
                      "to_ms": candles[-1]["start"] if candles else None}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"data": result["data"], "metrics": result["metrics"],
                      "assumptions": result.get("assumptions")}, indent=2))


if __name__ == "__main__":
    main()
