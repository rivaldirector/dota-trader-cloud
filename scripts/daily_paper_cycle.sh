#!/bin/zsh -l
# ──────────────────────────────────────────────────────────────────────────
# daily_paper_cycle.sh — ПОСТОЯННЫЙ (continuous) цикл harvest + paper-trading.
#
# Имя файла осталось историческим, но это больше НЕ ежедневный запуск:
# launchd дёргает этот скрипт каждые ~10 минут (см. StartInterval в
# com.dotatrader.dailycycle.plist), чтобы сигналы/результаты появлялись
# в dashboard.html близко к реальному времени, а не раз в сутки.
#
# Тяжёлые шаги (5,6 — реальные вызовы PandaScore API) троттлятся ВНУТРИ
# самих скриптов (THROTTLE_MINUTES), так что частый запуск этого файла
# не означает частые реальные HTTP-вызовы — лишние прогоны просто
# мгновенно завершаются с "Пропуск: ... < N мин назад".
#
# Шаги:
#   1. betsapi_harvest.py --settle-only     → BetsAPI (мёртв с 17.06, но
#                                              оставлен — заработает сам,
#                                              когда токен переподключат)
#   2. settle_via_pandascore.py             → РЕАЛЬНЫЙ источник сеттлинга
#                                              сейчас (PandaScore, не зависит
#                                              от BetsAPI). Идемпотентен —
#                                              трогает только settled=0.
#   3. paper_trading.py --mode bet          → слепые ставки на новые матчи
#                                              (результаты НЕ смотрим)
#   4. paper_trading.py --mode settle       → доп. сеттлинг через raw_events
#                                              (на случай если BetsAPI оживёт)
#   5. paper_trading.py --mode report       → лидерборд + гейт Rule C (M05)
#   6. fetch_pandascore_schedule.py         → расписание (throttle 15 мин)
#   7. fetch_pandascore_history.py          → история для Elo (throttle 60 мин)
#   8. generate_dashboard.py                → пересборка dashboard.html
#                                              (горизонт предиктов: 72ч)
#
# Лог: logs/daily_cycle.log
# ──────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/daily_cycle.log"

{
  echo "===== $(date) ====="

  echo "[1/8] BetsAPI settle-only (ожидаемо no-op пока токен мёртв)..."
  python3 scripts/betsapi_harvest.py --settle-only

  echo "[2/8] Сеттлинг через PandaScore (рабочий источник)..."
  python3 scripts/settle_via_pandascore.py

  echo "[3/8] Placing new blind paper bets..."
  python3 scripts/paper_trading.py --mode bet

  echo "[4/8] Settling paper bets (raw_events fallback)..."
  python3 scripts/paper_trading.py --mode settle

  echo "[5/8] Report..."
  python3 scripts/paper_trading.py --mode report

  echo "[6/8] Добор расписания из PandaScore (throttle 15 мин)..."
  python3 scripts/fetch_pandascore_schedule.py

  echo "[7/8] Добор истории из PandaScore для Elo (throttle 60 мин)..."
  python3 scripts/fetch_pandascore_history.py --days 60

  echo "[8/8] Генерация dashboard.html (горизонт 72ч)..."
  python3 scripts/generate_dashboard.py

  echo "===== Done $(date) ====="
  echo ""
} >> "$LOG" 2>&1
