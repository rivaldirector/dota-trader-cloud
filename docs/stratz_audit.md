# STRATZ API Аудит

**Дата:** 2026-06-15  
**Назначение:** model features (НЕ odds source)

---

## Что такое STRATZ

Dota 2 stats platform. Берёт данные напрямую из Steam API (Valve официальный поток).
GraphQL API + REST API. Бесплатный токен через stratz.com.

API: `https://api.stratz.com/graphql`  
Docs: `https://api.stratz.com/graphiql` (интерактивный explorer)

---

## Доступные данные

### Match-уровень
| Поле | Описание |
|------|----------|
| `id` | Match ID (совместим с OpenDota) |
| `didRadiantWin` | Результат |
| `durationSeconds` | Длительность |
| `gameMode` | Game mode (AP, CM, etc.) |
| `lobbyType` | Тип лобби (tournament, ranked, etc.) |
| `league.id / league.name` | Лига |
| `tournamentId` | Турнир |
| `radiantTeam / direTeam` | Команды (id, name) |
| `radiantKills / direKills` | Общие киллы |
| `pickBans` | **Полный драфт** (см. ниже) |
| `players` | **Игроки** (см. ниже) |

### Драфт (pickBans) ← ключевой источник
```graphql
pickBans {
  isPick      # true=pick, false=ban
  heroId      # ID героя
  isRadiant   # сторона
  order       # порядок 0-23
  bannedHeroId # если ban
}
```
**Это полный CM-драфт.** Порядок пиков/банов, герои, стороны — всё есть.

### Игроки (players)
```graphql
players {
  heroId
  isRadiant
  kills / deaths / assists
  netWorth / goldPerMinute / experiencePerMinute
  heroDamage / towerDamage / heroHealing
  level
  item0Id ... item5Id  # итемы на конец матча
  backpack0Id ... backpack2Id
  steamAccount { id name seasonRank }  # MMR/ранг (если не скрыт)
}
```

### Hero stats (агрегированные)
- Win rates по патчу, bracket, позиции
- Counter/synergy матрицы
- Доступны через отдельные запросы

---

## Покрытие

| Категория | Покрытие |
|-----------|----------|
| Все публичные матчи | ✓ (через Steam API) |
| Tournament / pro матчи | ✓ |
| Ranked матчи | ✓ (если не приватные) |
| Исторические данные | 7+ лет (с ~2017) |
| Picks/bans в CM | ✓ (если матч публичный) |
| Приватные steam профили | ✗ (ранг скрыт) |

**Оценка coverage pro-матчей:** ~90%+ (официальные турниры через Valve — все)

---

## Rate limits (бесплатный tier)

| Параметр | Значение |
|----------|----------|
| Запросы/мин | ~300 |
| Запросов/месяц (без токена) | 10,000 |
| Запросов/месяц (с free токеном) | ~100,000+ |
| Токен | Бесплатно на stratz.com |

---

## Сложность интеграции

**Низкая.** GraphQL — гибкий, документированный, работает из коробки.

```python
import requests

TOKEN = "your_stratz_token"
QUERY = """
{
  match(id: 7938990591) {
    id
    didRadiantWin
    radiantTeam { name }
    direTeam { name }
    pickBans { isPick heroId isRadiant order }
    players { heroId isRadiant kills deaths assists netWorth }
  }
}
"""

r = requests.post(
    "https://api.stratz.com/graphql",
    json={"query": QUERY},
    headers={"Authorization": f"Bearer {TOKEN}"},
)
data = r.json()["data"]["match"]
```

---

## Model Features из STRATZ

### Драфт-фичи (новые признаки)
| Feature | Описание |
|---------|----------|
| `hero_winrate_radiant` | Средний WR всех пиков radiant |
| `hero_winrate_dire` | Средний WR всех пиков dire |
| `draft_advantage` | Разница WR в пользу radiant |
| `counter_score` | Насколько dire контрит radiant (матрица) |
| `cm_ban_value` | Стоимость забаненных сильных героев |

### Игроки (если данные доступны)
| Feature | Описание |
|---------|----------|
| `avg_rank_radiant` | Средний ранг команды |
| `avg_rank_dire` | Средний ранг команды |
| `rank_diff` | Разница рангов |

### Исторические фичи (обогащение матчей)
- Добавить picks/bans к уже собранным матчам PandaScore
- Обогащение через match_id (Steam/OpenDota ID)

---

## Проблемы

1. **PandaScore ID ≠ Steam Match ID** — нужен маппинг через external_references
2. **Приватные профили** — часть ranked матчей без ранга игроков
3. **Лаг данных** — ~10 мин после окончания матча до появления в STRATZ

---

## Рекомендация

**Приоритет STRATZ:** после Elo/PandaScore baseline.

**Этапы:**
1. Получить STRATZ токен (5 мин, бесплатно)
2. Написать `adapters/stratz.py` с GraphQL клиентом
3. Обогатить исторические матчи picks/bans (через Steam IDs из PandaScore)
4. Добавить `draft_advantage` как feature в модель
5. Сравнить accuracy с/без драфт-фич

**Ожидаемый эффект на модель:** +2-5% accuracy (драфт — сильный предиктор исхода)

---

## Следующий шаг

```bash
# 1. Получить токен: https://stratz.com/api
# 2. Добавить в .env:
STRATZ_TOKEN=your_token_here

# 3. Проверить:
python3 -c "
import requests
TOKEN = 'your_token'
r = requests.post('https://api.stratz.com/graphql',
    json={'query': '{match(id:7938990591){id didRadiantWin}}'},
    headers={'Authorization': f'Bearer {TOKEN}'})
print(r.json())
"
```
