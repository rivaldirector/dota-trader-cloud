"""Загрузка котировок: основной источник — Yahoo Finance, запасной — Stooq."""

from __future__ import annotations

import io
import time

import pandas as pd
import requests

from .config import STOOQ_SYMBOLS

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# Yahoo принимает ограниченный набор пар interval/range.
_VALID_INTERVALS = {"15m", "30m", "1h", "4h", "1d"}


class DataFeedError(RuntimeError):
    pass


def fetch_yahoo(symbol: str, interval: str = "1h", lookback: str = "6mo", retries: int = 3) -> pd.DataFrame:
    """Скачивает OHLCV с Yahoo Finance. Возвращает DataFrame с колонками open/high/low/close/volume."""
    if interval not in _VALID_INTERVALS:
        raise ValueError(f"Неподдерживаемый интервал: {interval}")

    # Yahoo не отдаёт 4h напрямую — берём 1h и агрегируем.
    request_interval = "1h" if interval == "4h" else interval
    # Внутридневная история у Yahoo ограничена ~730 днями, для 1h безопасно до 1y.
    if request_interval != "1d" and lookback in {"2y", "5y", "10y", "max"}:
        lookback = "1y"

    params = {"interval": request_interval, "range": lookback, "events": "history"}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(YAHOO_CHART_URL.format(symbol=symbol), params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            result = payload["chart"]["result"][0]
            ts = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            df = pd.DataFrame(
                {
                    "open": quote["open"],
                    "high": quote["high"],
                    "low": quote["low"],
                    "close": quote["close"],
                    "volume": quote["volume"],
                },
                index=pd.to_datetime(ts, unit="s", utc=True),
            )
            df = df.dropna(subset=["close"])
            if df.empty:
                raise DataFeedError(f"Yahoo вернул пустые данные для {symbol}")
            if interval == "4h":
                df = resample_ohlcv(df, "4h")
            return df
        except Exception as err:  # noqa: BLE001 — ретраим любые сетевые/парсинговые сбои
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise DataFeedError(f"Не удалось получить данные Yahoo для {symbol}: {last_err}")


def fetch_stooq(symbol: str) -> pd.DataFrame:
    """Запасной источник: дневные бары со Stooq (без ключа API)."""
    stooq_symbol = STOOQ_SYMBOLS.get(symbol, symbol.lower())
    resp = requests.get(STOOQ_CSV_URL.format(symbol=stooq_symbol), headers=HEADERS, timeout=20)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    if "Close" not in df.columns or df.empty:
        raise DataFeedError(f"Stooq вернул пустые данные для {symbol}")
    df = df.rename(columns=str.lower).set_index(pd.to_datetime(df["Date"], utc=True))
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["close"])


def fetch_ohlcv(symbol: str, interval: str = "1h", lookback: str = "6mo") -> pd.DataFrame:
    """Основная точка входа: Yahoo, при недоступности — Stooq (только дневки)."""
    try:
        return fetch_yahoo(symbol, interval=interval, lookback=lookback)
    except Exception:
        df = fetch_stooq(symbol)
        # Stooq отдаёт только дневные бары — для внутридневных интервалов это деградация,
        # но сигналы по дневкам лучше, чем отсутствие данных.
        return df.tail(400)
