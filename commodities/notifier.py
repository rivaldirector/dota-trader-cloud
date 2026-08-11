"""Уведомления о сигналах: консоль всегда, Telegram — если заданы токен и chat_id."""

from __future__ import annotations

import requests

from .config import settings

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str) -> bool:
    """Печатает сообщение и, при настроенном Telegram, отправляет его в чат."""
    print(text, flush=True)
    if not settings.telegram_token or not settings.telegram_chat_id:
        return False
    try:
        resp = requests.post(
            TELEGRAM_URL.format(token=settings.telegram_token),
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as err:  # noqa: BLE001 — сбой доставки не должен ронять цикл
        print(f"[notifier] Telegram недоступен: {err}", flush=True)
        return False
