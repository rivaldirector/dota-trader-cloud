from dotenv import load_dotenv
from pydantic import BaseModel
import os

load_dotenv()

# Инструменты по умолчанию: фьючерс на золото и нефть WTI (тикеры Yahoo Finance).
DEFAULT_SYMBOLS = "GC=F,CL=F"

# Человекочитаемые имена для отчётов и сообщений.
SYMBOL_NAMES = {
    "GC=F": "Золото (COMEX)",
    "CL=F": "Нефть WTI",
    "BZ=F": "Нефть Brent",
    "SI=F": "Серебро (COMEX)",
    "XAUUSD=X": "Золото спот",
}

# Соответствие тикеров Yahoo -> Stooq (запасной источник, только дневные бары).
STOOQ_SYMBOLS = {
    "GC=F": "gc.f",
    "CL=F": "cl.f",
    "BZ=F": "cb.f",
    "SI=F": "si.f",
    "XAUUSD=X": "xauusd",
}


class CommoditySettings(BaseModel):
    symbols: list[str] = [s.strip() for s in os.getenv("COMMODITY_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
    interval: str = os.getenv("COMMODITY_INTERVAL", "1h")     # 1h / 4h / 1d
    lookback: str = os.getenv("COMMODITY_LOOKBACK", "6mo")    # глубина истории для расчёта индикаторов

    # Риск-менеджмент
    account_size: float = float(os.getenv("COMMODITY_ACCOUNT", "10000"))
    risk_pct: float = float(os.getenv("COMMODITY_RISK_PCT", "0.01"))       # риск на сделку, доля счёта
    atr_stop_mult: float = float(os.getenv("COMMODITY_ATR_STOP", "1.5"))   # стоп = N * ATR
    atr_target_mult: float = float(os.getenv("COMMODITY_ATR_TARGET", "3.0"))  # цель = N * ATR
    min_score: int = int(os.getenv("COMMODITY_MIN_SCORE", "3"))            # минимум очков подтверждения для сигнала

    # Live-режим
    poll_seconds: int = int(os.getenv("COMMODITY_POLL_SECONDS", "900"))
    state_path: str = os.getenv("COMMODITY_STATE_PATH", "storage/commodity_signals.json")

    # Telegram (необязательно)
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


settings = CommoditySettings()
