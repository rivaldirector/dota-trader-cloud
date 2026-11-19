"""Unauthenticated Bybit V5 public market-data client. No trading endpoints."""
from __future__ import annotations

import os
import time
from typing import Any

import requests


class BybitPublicClient:
    def __init__(self, base_url: str | None = None, timeout: int = 10):
        self.base_url = (base_url or os.getenv("BYBIT_API_BASE", "https://api.bybit.com")).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "dota-trader-cloud-bybit-signals/0.1"})

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}"
            )
        return payload.get("result", {})

    def klines(
        self, symbol: str = "BTCUSDT", interval: str = "1", limit: int = 200,
        category: str = "linear",
    ) -> list[dict[str, float | int]]:
        result = self._get(
            "/v5/market/kline",
            {"category": category, "symbol": symbol, "interval": interval, "limit": limit},
        )
        rows = result.get("list", [])
        candles = [
            {
                "start": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6]),
            }
            for row in rows
        ]
        # Bybit returns newest first; strategy expects oldest first.
        candles.sort(key=lambda candle: candle["start"])
        # Drop the still-forming candle to avoid using an unstable close.
        now_ms = int(time.time() * 1000)
        interval_ms = self._interval_ms(interval)
        return [c for c in candles if c["start"] + interval_ms <= now_ms]

    def instrument_info(self, symbol: str = "BTCUSDT", category: str = "linear") -> dict[str, Any]:
        result = self._get(
            "/v5/market/instruments-info", {"category": category, "symbol": symbol}
        )
        rows = result.get("list", [])
        if not rows:
            raise RuntimeError(f"No instrument specification returned for {symbol}")
        row = rows[0]
        lot = row.get("lotSizeFilter", {})
        price = row.get("priceFilter", {})
        return {
            "symbol": row.get("symbol"),
            "status": row.get("status"),
            "min_order_qty": float(lot.get("minOrderQty", 0) or 0),
            "qty_step": float(lot.get("qtyStep", 0) or 0),
            "min_notional": float(lot.get("minNotionalValue", 0) or 0),
            "tick_size": float(price.get("tickSize", 0) or 0),
        }

    def ticker(self, symbol: str = "BTCUSDT", category: str = "linear") -> dict[str, float]:
        result = self._get(
            "/v5/market/tickers", {"category": category, "symbol": symbol}
        )
        rows = result.get("list", [])
        if not rows:
            raise RuntimeError(f"No ticker returned for {symbol}")
        row = rows[0]
        return {
            "last": float(row["lastPrice"]),
            "bid": float(row["bid1Price"]),
            "ask": float(row["ask1Price"]),
        }

    @staticmethod
    def _interval_ms(interval: str) -> int:
        if interval.isdigit():
            return int(interval) * 60_000
        return {"D": 86_400_000, "W": 604_800_000, "M": 2_592_000_000}[interval]
