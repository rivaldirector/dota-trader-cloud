"""Торговый робот по сырьевым рынкам: золото (XAU) и нефть (WTI/Brent).

Модули:
  data       — загрузка котировок (Yahoo Finance, запасной источник Stooq)
  indicators — технические индикаторы (EMA, RSI, MACD, ATR, Bollinger)
  strategy   — генератор сигналов BUY/SELL с уровнями входа, стопа и целей
  backtest   — проверка стратегии на истории
  notifier   — уведомления в Telegram
  bot        — live-цикл: опрос рынка и рассылка сигналов
"""

__all__ = ["data", "indicators", "strategy", "backtest", "notifier", "bot"]
