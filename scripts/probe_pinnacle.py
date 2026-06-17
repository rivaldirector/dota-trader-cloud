#!/usr/bin/env python3
"""
Проверка Pinnacle Odds API (pinnacle-odds-api.p.rapidapi.com).

Запуск:
    python3 scripts/probe_pinnacle.py
"""
import sys, json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

KEY = settings.rapidapi_key
if not KEY:
    print("ERROR: RAPIDAPI_KEY не задан в .env")
    sys.exit(1)

from adapters.pinnacle_rapidapi import PinnacleClient

print(f"Key: {KEY[:12]}...")
print()

client = PinnacleClient()

# 1. Все доступные спорты
print("=== Все спорты (GET /pinnacle/sports) ===")
try:
    sports = client.get_sports()
    print(f"Всего: {len(sports)}")
    for s in sports:
        print(f"  id={s.get('id') or s.get('sportId'):>5}  name={s.get('name') or s.get('sportName')}")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

# 2. Искать Dota 2 / esports
print()
dota_id = client.find_dota2_sport_id()
if dota_id:
    print(f"✓ Dota 2 найден: sport_id={dota_id}")
else:
    print("✗ Dota 2 не найден среди спортов")
    print("  Список выше — какой ID у esports?")
    sys.exit(0)

# 3. Лиги Dota 2
print(f"\n=== Лиги Dota 2 (sport_id={dota_id}) ===")
try:
    leagues = client.get_leagues(dota_id)
    print(f"Всего лиг: {len(leagues)}")
    for l in leagues[:15]:
        print(f"  id={l.get('id') or l.get('leagueId'):>8}  {l.get('name') or l.get('leagueName')}")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

if not leagues:
    print("Лиг нет — нет активных матчей прямо сейчас")
    sys.exit(0)

# 4. Матчи
league_ids = [l.get("id") or l.get("leagueId") for l in leagues[:5] if l.get("id") or l.get("leagueId")]
print(f"\n=== Matchups (первые 5 лиг) ===")
try:
    matchups = client.get_matchups(league_ids)
    print(f"Матчей: {len(matchups)}")
    for m in matchups[:5]:
        print(f"  {json.dumps(m, ensure_ascii=False)[:120]}")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

if not matchups:
    print("Матчей нет")
    sys.exit(0)

# 5. Odds первого матча
first = matchups[0]
event_id = first.get("id") or first.get("eventId") or first.get("matchupId")
print(f"\n=== Odds первого матча (id={event_id}) ===")
try:
    odds = client.get_odds(int(event_id))
    print(json.dumps(odds, indent=2, ensure_ascii=False)[:1000])
except Exception as e:
    print(f"ERROR: {e}")

print("\n✓ Probe complete. Если всё OK:")
print("  python3 scripts/collect_odds.py --once")
