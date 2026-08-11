"""Live-цикл робота: опрашивает рынок, генерирует сигналы, шлёт уведомления.

Дедупликация: по каждому инструменту помним время бара и направление последнего
отправленного сигнала (storage/commodity_signals.json), чтобы при перезапуске
и на каждом тике не слать одно и то же.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import settings
from .data import fetch_ohlcv
from .indicators import add_indicators
from .notifier import send
from .strategy import evaluate


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — отсутствие/битость файла состояния не критична
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def scan_once(state: dict | None = None) -> list:
    """Один проход по всем инструментам. Возвращает список новых сигналов."""
    signals = []
    for symbol in settings.symbols:
        try:
            df = fetch_ohlcv(symbol, interval=settings.interval, lookback=settings.lookback)
            sig = evaluate(symbol, add_indicators(df))
        except Exception as err:  # noqa: BLE001 — один инструмент не должен ронять остальные
            print(f"[bot] {symbol}: ошибка данных/расчёта: {err}", flush=True)
            continue
        if sig is None:
            print(f"[bot] {symbol}: сигнала нет", flush=True)
            continue
        key = f"{sig.symbol}:{sig.side}:{sig.time}"
        if state is not None and state.get(sig.symbol) == key:
            print(f"[bot] {symbol}: сигнал уже отправлялся ({key})", flush=True)
            continue
        signals.append(sig)
        if state is not None:
            state[sig.symbol] = key
    return signals


def run_live() -> None:
    state_path = Path(settings.state_path)
    state = _load_state(state_path)
    send(
        "🤖 Робот по золоту и нефти запущен.\n"
        f"Инструменты: {', '.join(settings.symbols)}; таймфрейм {settings.interval}; "
        f"опрос каждые {settings.poll_seconds // 60} мин."
    )
    while True:
        for sig in scan_once(state):
            send(sig.render())
        _save_state(state_path, state)
        time.sleep(settings.poll_seconds)
