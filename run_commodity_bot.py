#!/usr/bin/env python3
"""Торговый робот по золоту и нефти: сигналы, бэктест, live-режим.

Примеры:
  python run_commodity_bot.py signals              # разовый прогон, показать сигналы
  python run_commodity_bot.py backtest             # бэктест на истории по умолчанию
  python run_commodity_bot.py backtest --interval 1d --lookback 2y
  python run_commodity_bot.py live                 # бесконечный цикл с уведомлениями

Настройка через .env: COMMODITY_SYMBOLS, COMMODITY_INTERVAL, COMMODITY_RISK_PCT,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и др. — см. docs/COMMODITY_BOT.md.
"""

from __future__ import annotations

import argparse

from commodities.backtest import run_backtest
from commodities.bot import run_live, scan_once
from commodities.config import settings
from commodities.data import fetch_ohlcv


def cmd_signals() -> None:
    signals = scan_once(state=None)
    if not signals:
        print("Сейчас сигналов нет — рынок не даёт подтверждённой точки входа.")
    for sig in signals:
        print()
        print(sig.render())


def cmd_backtest(interval: str, lookback: str, min_score: int | None) -> None:
    print(f"Бэктест: таймфрейм {interval}, история {lookback}, порог score {min_score or settings.min_score}\n")
    for symbol in settings.symbols:
        try:
            df = fetch_ohlcv(symbol, interval=interval, lookback=lookback)
        except Exception as err:  # noqa: BLE001
            print(f"{symbol}: не удалось получить данные: {err}")
            continue
        report = run_backtest(symbol, df, min_score=min_score)
        print(report.render())
        for t in report.trades[-5:]:
            print(f"  {t.side} {t.entry_time} @ {t.entry:.2f} -> {t.result} @ {t.exit:.2f} ({t.pnl_r:+.1f}R)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Робот сигналов по золоту и нефти")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("signals", help="разовый прогон: показать текущие сигналы")
    bt = sub.add_parser("backtest", help="проверить стратегию на истории")
    bt.add_argument("--interval", default=settings.interval, choices=["15m", "30m", "1h", "4h", "1d"])
    bt.add_argument("--lookback", default="1y")
    bt.add_argument("--min-score", type=int, default=None)
    sub.add_parser("live", help="бесконечный цикл с уведомлениями")

    args = parser.parse_args()
    if args.command == "signals":
        cmd_signals()
    elif args.command == "backtest":
        cmd_backtest(args.interval, args.lookback, args.min_score)
    elif args.command == "live":
        run_live()


if __name__ == "__main__":
    main()
