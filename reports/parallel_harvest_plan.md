# Phase 5 Harvest — ТЗ (финальная версия)
**Дата:** 16 июня 2026

---

## Ограничение скорости

BetsAPI: **1800 req/hour PER ACCOUNT** (не per process).
Параллельные процессы не увеличивают скорость, только делят лимит.

**Бюджет:**
| Процесс | req/hour | Интервал |
|---|---|---|
| Terminal 1 — Dota Phase 5 | ~1333 (89%) | 2.7s |
| Terminal 2 — Live Poller | ~32 (2%) | 900s между запусками |
| Terminal 3 — Monitor | 0 | — |
| **Итого** | **~1365** | **< 1500 ✓** |

---

## Архитектура — 3 терминала

```
Terminal 1 — Dota Phase 5 (основной сбор, 89% лимита)
Terminal 2 — Live Poller (upcoming матчи, 2% лимита)
Terminal 3 — Monitor (SQL только, без API)
```

**CS2 / LoL / Valorant:** только после Dota Phase 5 ≥ 80% готов.

---

## Шаг 0 — Pre-flight dry-run

```bash
cd ~/Downloads/dota_trader_v2

python3 scripts/phase5_worker.py \
    --date-from 2020-01-01 --date-to 2026-12-31 \
    --shard-db storage/phase5_dota_all.db \
    --interval 2.7 \
    --dry-run --limit 5
```

**Проверить в выводе:**
- [ ] `→ N snapshots` (N > 0 хотя бы для части матчей)
- [ ] Нет 403 / DatabaseError
- [ ] Три рынка: 151_1, 151_2, 151_3

---

## Terminal 1 — Dota Phase 5 (основной)

```bash
cd ~/Downloads/dota_trader_v2
python3 scripts/phase5_worker.py \
    --date-from 2020-01-01 --date-to 2026-12-31 \
    --shard-db storage/phase5_dota_all.db \
    --interval 2.7 \
    --sport dota2
```

**ETA:** ~15,000 событий × 2.7s ÷ 3600 ≈ **11.3 часов** до 100%

**Resume** (после Ctrl+C): та же команда — worker_progress помнит где остановился.

**Retry ошибок:**
```bash
sqlite3 storage/phase5_dota_all.db "UPDATE worker_progress SET done=0 WHERE done=-1;"
```

---

## Terminal 2 — Live Poller (upcoming матчи)

```bash
cd ~/Downloads/dota_trader_v2
# Тест (один запуск):
python3 scripts/live_poller.py --once

# Постоянный режим (опрос каждые 15 мин):
python3 scripts/live_poller.py --interval 900
```

Пишет в `storage/live_tracking.db` (отдельно от основной БД).
~32 req/hour — безопасно рядом с Terminal 1.

---

## Terminal 3 — Monitor (только SQL, без API)

```bash
cd ~/Downloads/dota_trader_v2
python3 scripts/phase5_monitor.py
```

Одноразовый отчёт:
```bash
python3 scripts/phase5_monitor.py --once
```

---

## Ручной мониторинг — SQL команды

### Общий прогресс Phase 5
```bash
sqlite3 storage/phase5_dota_all.db "
SELECT
  done, COUNT(*) as cnt
FROM worker_progress GROUP BY done;
-- done=1: успешно, done=-2: пустые, done=-1: ошибки, done=0: не обработано"
```

### Строки в odds_history
```bash
sqlite3 storage/phase5_dota_all.db "
SELECT
  COUNT(*) as rows,
  COUNT(DISTINCT event_id) as events,
  SUM(CASE WHEN ss IS NULL THEN 1 ELSE 0 END) as prematch,
  SUM(CASE WHEN ss IS NOT NULL THEN 1 ELSE 0 END) as live
FROM odds_history;"
```

### По рынкам
```bash
sqlite3 storage/phase5_dota_all.db "
SELECT market, COUNT(*) as rows FROM odds_history GROUP BY market;"
```

### Скорость (строк за последний час)
```bash
sqlite3 storage/phase5_dota_all.db "
SELECT COUNT(*) as rows_last_hour FROM odds_history
WHERE fetched_at > datetime('now', '-1 hour');"
```

### Последние 5 обработанных событий
```bash
sqlite3 storage/phase5_dota_all.db "
SELECT event_id, done, pts_count, done_at
FROM worker_progress ORDER BY done_at DESC LIMIT 5;"
```

### Процент готовности (vs main DB)
```bash
sqlite3 storage/betsapi_harvest.db "
SELECT
  COUNT(*) as total,
  SUM(history_done) as done,
  ROUND(SUM(history_done)*100.0/COUNT(*), 1) as pct
FROM harvest_progress WHERE summary_done=1;"
```

---

## Когда запускать CS2 / LoL

1. Проверь % готовности Dota (команда выше) → нужно ≥ 80%
2. Останови или снизь Terminal 1 до `--interval 5.4` (потеряешь скорость вдвое)
3. Запусти CS2:
```bash
python3 scripts/phase5_worker.py \
    --date-from 2022-01-01 --date-to 2026-12-31 \
    --shard-db storage/phase5_cs2.db \
    --interval 5.4 \
    --sport cs2
```
(2 воркера × 1/5.4 = 1333 req/hour суммарно — те же 89% лимита)

---

## Объединение шардов в main DB

После завершения Phase 5 (phase5_dota_all.db → в betsapi_harvest.db):
```bash
python3 scripts/merge_phase5_shards.py
```
*(скрипт будет написан после завершения сбора)*

---

## Порядок запуска

```
1. Terminal 3: запусти monitor → убедись что читает файлы
2. Terminal 1: pre-flight dry-run --limit 5 → если ОК
3. Terminal 1: запусти основной Dota Phase 5 (--interval 2.7)
4. Terminal 2: запусти live_poller.py (опционально)
5. Следи за 429 в Terminal 1. Если появились → увеличь interval до 3.5s
6. После Dota ≥ 80% → решай по CS2/LoL
```

---

## SQLite безопасность

| Ситуация | Безопасно? |
|---|---|
| Terminal 1 пишет в phase5_dota_all.db | ✅ |
| Terminal 2 пишет в live_tracking.db | ✅ (отдельная БД) |
| Terminal 3 читает обе (read-only) | ✅ |
| Два процесса пишут в одну DB | ❌ Избегать |
