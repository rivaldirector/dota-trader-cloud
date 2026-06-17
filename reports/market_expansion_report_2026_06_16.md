# Dota 2 Market Expansion Research
**Дата:** 16 июня 2026  
**Источник данных:** storage/betsapi_harvest.db (454 MB, 13,782 Dota2 матча)  
**Задача:** Найти второй рынок для edge-исследования после Match Winner  
**Заморожено:** Rule C, Elo, H2H — не трогать

---

## Главный вывод

В BetsAPI для Dota 2 доступны **ровно 3 рынка**: Match Winner, Map Handicap ±1.5, Total Maps 2.5.  
Correct Score, Map 1/2/3 Winner — **отсутствуют** как самостоятельные рынки.

**Первый кандидат для исследования: Map Handicap -1.5/+1.5 (151_2)**

---

## Задача 1 — Market Inventory

### Что реально есть в BetsAPI Dota2

| Код | Рынок | Поле |
|---|---|---|
| `151_1` | Match Winner | `home_od` / `away_od` |
| `151_2` | Map Handicap (-1.5/+1.5) | `home_od` / `away_od` + `handicap` |
| `151_3` | Total Maps 2.5 | `over_od` / `under_od` + `handicap` |

Больше ничего. Codes 151_4, 151_5 и прочие — **не существуют** в наших данных.

Все три рынка хранятся в одном `raw_json` в `odds_summary`.  
Мы парсили только `151_1`. Данные по `151_2` и `151_3` уже в БД — нужно только их распарсить.

**Внутри 151_2 также есть:** kill handicap (+10.5, +11.5 и т.д.) — внутриигровые линии, не рыночный интерес.  
**Внутри 151_3 также есть:** kill total (108.5–121.5) и total 4.5 (BO5 серии) — не Dota2 BO3 интерес.

---

## Задача 2 — Market Quality Ranking

### Таблица качества (все 13,782 матча)

| Метрика | Match Winner 151_1 | Handicap ±1.5 151_2 | Total Maps 2.5 151_3 |
|---|---|---|---|
| Events | 13,782 (100%) | 12,833 (93.1%) | 10,008 (72.6%) |
| Bookmakers | 17 | 17 | 17 |
| Pinnacle events | 6,102 (44.3%) | 5,675 (44.2%) | 5,626 (56.2%) |
| Open+Close events | 13,782 | 12,833 | 10,008 |
| OC + Pinnacle | 6,102 | 5,675 | 5,626 |
| Pinnacle pre-match close | 6,102 | **5,607** | **5,584** |
| % lines moved | 76.6% | 75.4% | 68.4% |
| Median movement | 0.140 | 0.167 | 0.070 |
| Avg movement (pre-match) | — | — | 0.180 |

> **Важно:** ~30% "end" записей содержат LIVE-коэфициенты (поле `ss` внутри odds ≠ null).  
> При фильтрации `ss=null` в "end" — данные чистые. Pre-match Pinnacle coverage не меняется: 5,607 и 5,584.

### Рейтинг рынков

**🟢 Grade A — исследовать сейчас:**

**Handicap ±1.5 (151_2)**
- 12,833 событий (93.1%), 5,607 Pinnacle pre-match
- Логически связан с Elo (elo_diff → вероятность свипа)
- Backtest полностью реализуем через `ss` из raw_events

**Total Maps 2.5 (151_3)**  
- 10,008 событий (72.6%), 5,584 Pinnacle pre-match
- Самое высокое Pinnacle покрытие относительно событий (56.2%)
- Другой угол: предсказываем не победителя, а "сколько карт"

**🔴 Grade C — нет данных / не трогать:**

- Correct Score (2:0 / 2:1) — нет самостоятельного рынка → CLV невозможен
- Map 1/2/3 Winner — отсутствует в BetsAPI для Dota2
- Live/line movement markets — Phase 5 сломана (парсер), 0 строк в odds_history

---

## Задача 3 — Correct Score Feasibility

**correct_score_market_available = NO**

В BetsAPI нет рынка типа "Score 2:0" или "Score 2:1".  
Коды 151_4+: отсутствуют в данных.

**Что есть вместо:** поле `raw_events.ss` содержит фактический счёт серии.

| Результат | Матчей | % |
|---|---|---|
| 2-0 (дом свип) | 4,572 | 37.5% |
| 0-2 (гость свип) | 3,414 | 28.0% |
| 2-1 (дом 3 карты) | 2,218 | 18.2% |
| 1-2 (гость 3 карты) | 1,997 | 16.4% |
| **BO3 итого** | **12,201** | — |

**Correct score через комбинацию:**

| Счёт | Эквивалент |
|---|---|
| 2:0 | Under 2.5 + home wins |
| 0:2 | Under 2.5 + away wins |
| 2:1 | Over 2.5 + home wins |
| 1:2 | Over 2.5 + away wins |

Это полезно как **метка для бэктеста**, но не как самостоятельный CLV-рынок.

**Вывод:** Correct score может быть дополнительным label в исследовании Handicap/Total. Как основной рынок — не доступен.

---

## Задача 4 — Handicap -1.5 Feasibility

**can_backtest = YES ✓**

### Покрытие

| Метрика | Значение |
|---|---|
| Pinnacle events | 5,675 |
| Pre-match Pinnacle close | 5,607 |
| Matchable с series score | 5,173 (91.2%) |
| Home team с -1.5 | 3,019 |
| Home team с +1.5 | 2,154 |
| % Pinnacle lines moved | 96.1% |
| Avg Pinnacle movement (pre-match) | 0.167 (median) |
| Avg Pinnacle open (home_od) | 2.038 |

### Логика бэктеста

```
SIGNAL:  наша Elo модель предсказывает высокую вероятность победы команды (elo_diff ≥ X)
MARKET:  берём линию на эту команду -1.5 (должна выиграть 2:0)
LABEL:   ss == '2-0' если home -1.5, ss == '0-2' если away -1.5 → COVERS
         любой другой результат → FAILS
CLV:     открытая линия > Pinnacle closing line → положительный CLV
```

### Связь с существующей моделью

- Elo diff ≥ 75 (текущий Rule C фильтр) → высокая доминанта → повышает вероятность свипа
- Та же Elo/H2H база, новый выход: не "кто победит", а "победит ли с сухим счётом"
- Гипотеза: рынок Handicap менее эффективен чем Match Winner, т.к. требует точного предсказания счёта, а не просто исхода

### Базовая частота по данным

Из 12,201 BO3 матчей:
- 65.5% закончились свипом (2:0 или 0:2) → Under 2.5 сторона
- При elo_diff ≥ 150: доля свипов выше (нужно считать)

---

## Задача 5 — Total Maps 2.5 Feasibility

**can_backtest = YES ✓**

### Покрытие

| Метрика | Значение |
|---|---|
| Pinnacle events | 5,626 |
| Pre-match Pinnacle close | 5,584 |
| Matchable с series score | 5,175 (92.0%) |
| Avg Pinnacle open Over 2.5 | 2.336 |
| Avg Pinnacle open Under 2.5 | 1.815 |
| % lines moved (pre-match) | 68.4% |
| Avg movement pre-match (Pinnacle) | 0.180 |
| Median movement | 0.070 |

### Базовая частота (в Pinnacle-matched сете)

| Исход | N | % |
|---|---|---|
| Over 2.5 (2:1 или 1:2) | 2,098 | 40.5% |
| Under 2.5 (2:0 или 0:2) | 3,077 | 59.5% |

Fair odds: Over ~2.47 / Under ~1.68

**Сравнение с Pinnacle средним open:**
- Pinnacle avg open Over 2.5: 2.336
- Fair value: ~2.47
- Gap: -0.13 (Pinnacle немного переоценивает Under / недооценивает Over — нужна детальная проверка)

### Логика бэктеста

```
SIGNAL:  elo_diff мал (близкие команды) → повышает Over 2.5 (будет 3 карты)
         elo_diff велик (доминирование) → повышает Under 2.5 (свип)
MARKET:  линия Over 2.5 или Under 2.5
LABEL:   ss in {'2-1','1-2'} → Over, ss in {'2-0','0-2'} → Under
CLV:     наш предиктивный коэф > Pinnacle close → CLV
```

---

## Задача 6 — Map Winner Feasibility

**can_backtest = NO ✗**

- Market codes найденные в BetsAPI: только `151_1`, `151_2`, `151_3`
- Map 1 Winner, Map 2 Winner, Map 3 Winner — **отсутствуют**
- Для получения этих рынков нужен другой план BetsAPI или отдельный endpoint
- Данные по результатам отдельных карт также отсутствуют в raw_events
- Вердикт: **не трогать** на текущем этапе

---

## Задача 7 — First Candidate Market

### Критерии выбора

| Критерий | Handicap ±1.5 | Total Maps 2.5 |
|---|---|---|
| n ≥ 1000 | ✓ 5,173 | ✓ 5,175 |
| Open+Close pre-match | ✓ | ✓ |
| Pinnacle coverage | ✓ 5,607 | ✓ 5,584 |
| Matchable с результатом | ✓ 91.2% | ✓ 92.0% |
| Логически связан с Elo | ✓✓ прямо | ✓ косвенно |
| Потенциально неэффективен | ✓✓ высокая вероятность | ✓ средняя |
| Покрытие матчей | ✓✓ 93.1% | ✓ 72.6% |

---

## Рекомендация: Map Handicap -1.5/+1.5 как первый кандидат

### Почему Handicap, не Total Maps

**Прямая связь с текущей моделью:**  
Elo diff у нас уже измеряет "насколько одна команда сильнее другой".  
Handicap -1.5 задаёт вопрос "достаточно ли они сильнее чтобы выиграть 2:0?"  
Total спрашивает "сколько карт?" — тот же вопрос, но чуть более абстрактный.

**Эффективность рынка:**  
Match winner — самый ликвидный рынок, sharp money туда идёт первым.  
Handicap -1.5 требует точного предсказания не просто победителя, а счёта серии.  
Теоретически: меньше sharp action → чуть менее эффективный рынок.

**Покрытие:**  
93.1% матчей имеют handicap против 72.6% для Total. Больше выборка → лучше статистика.

**Базовая асимметрия:**  
65.5% BO3 серий заканчиваются свипом. Если рынок это недооценивает — там edge.  
Elo + H2H хорошо предсказывают доминирование, а не просто победителя.

### Что НЕ так с Total Maps как первым кандидатом

Total Maps — отличный второй кандидат, но:
- Меньше покрытие (72.6%)
- Elo предсказывает winner, а sweep vs contest — другой вопрос  
- Лучше исследовать как дополнение после Handicap

### Итоговый выбор

```
ПЕРВЫЙ КАНДИДАТ:    Handicap ±1.5 (151_2)
ВТОРОЙ КАНДИДАТ:    Total Maps 2.5 (151_3)
НЕ ДОСТУПНО:        Correct Score, Map 1/2/3 Winner
ЖДЁТ PHASE 5:       Line Movement Markets
```

---

## Следующий шаг — что нужно сделать

1. **Распарсить 151_2 из raw_json** в новую таблицу `handicap_odds` (аналог `odds_summary`)
2. **Матчить с raw_events.ss** — получить датасет (match_id, open, close, result, covers)
3. **Фильтр ss=null в 'end'** — отсеивать live pricing от pre-match closing
4. **EDA**: распределение coverage/result по elo_diff сегментам
5. **CLV бэктест**: сравниваем наш сигнал (elo → sweep probability) с Pinnacle closing line

### Гипотеза для тестирования

```
Матчи с elo_diff ≥ 150:
  — рынок ставит favourite -1.5 ≈ 1.70–2.00
  — реальная частота свипа фаворита ≈ 55–65%
  — fair odds ≈ 1.54–1.82
  — если open line > fair → мы купили дешевле рынка → CLV+

Матчи с elo_diff < 50 (близкие команды):
  — рынок неопределён по -1.5
  — реальная частота свипа низкая (~30–40%)
  — фаворит -1.5 может быть ДОРОГИМ → CLV-
```

### Важные ограничения

- **Не строить ML** на этом этапе
- **Не менять Rule C** — это отдельная research-ветка
- **Результат исследования** — только feasibility + EDA + CLV статистика
- **Paper trading Handicap** начинать только после 30 Rule C сигналов пройдут через frozen систему

---

## Техническая справка

### Как извлечь Handicap данные из БД

```python
# Парсим 151_2 из raw_json
raw = json.loads(row['raw_json'])
for bm_name, bm_data in raw['results'].items():
    od = bm_data.get('odds', {}) or {}
    start = od.get('start') or {}
    end   = od.get('end') or {}
    
    s2 = start.get('151_2')
    e2 = end.get('151_2')
    
    if isinstance(s2, dict) and isinstance(e2, dict):
        hcp = s2.get('handicap')          # '-1.5' или '+1.5'
        open_home  = float(s2.get('home_od', 0))
        close_home = float(e2.get('home_od', 0))
        end_ss     = e2.get('ss')         # None = pre-match close ✓, else = live ✗

# Matching с результатом:
ss = json.loads(re_row['raw_json']).get('ss')  # '2-0', '2-1', etc.
if hcp == '-1.5':
    covers = (ss == '2-0')   # home wins 2:0 → -1.5 covers
elif hcp == '+1.5':
    covers = (ss != '0-2')   # away doesn't lose 2:0 → +1.5 covers
```

### Структура данных (резюме)

```
betsapi_harvest.db:
  raw_events:   sport_tag, ss (series score), raw_json
  odds_summary: market='151_1' только (151_2/151_3 внутри raw_json)
  
Нужно создать:
  handicap_summary: event_id, bookmaker, hcp_value, open_home, close_home,
                    end_is_prematch, series_result, covers
```
