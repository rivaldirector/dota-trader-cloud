#!/usr/bin/env python3
"""
Проверка BetsAPI — sport_id=151 (E-sports / Dota 2).

Запуск:
    python3 scripts/probe_betsapi.py
"""
import sys, json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
import requests

TOKEN = settings.betsapi_token
BASE  = settings.betsapi_base_url
SPORT = 151  # E-sports

if not TOKEN:
    print("ERROR: BETSAPI_TOKEN не задан в .env")
    print("Добавь: BETSAPI_TOKEN=your_token_here")
    sys.exit(1)

print(f"Token: {TOKEN[:8]}...")
print(f"Base:  {BASE}")
print()

session = requests.Session()

def get(path, params=None):
    p = {"token": TOKEN, **(params or {})}
    r = session.get(f"{BASE}{path}", params=p, timeout=15)
    print(f"  [{r.status_code}] {r.url[:100]}")
    r.raise_for_status()
    return r.json()

# 1. Upcoming esports
print("=== Upcoming E-sports (sport_id=151) ===")
try:
    data = get("/v3/events/upcoming", {"sport_id": SPORT})
    events = data.get("results", [])
    print(f"Событий: {data.get('pager', {}).get('total', len(events))}")
    for e in events[:5]:
        home = e.get("home", {}).get("name", "?")
        away = e.get("away", {}).get("name", "?")
        league = e.get("league", {}).get("name", "?")
        t = e.get("time", "")
        print(f"  [{e.get('id')}] {home} vs {away} | {league} | time={t}")
except Exception as ex:
    print(f"  ERROR: {ex}")

# 2. Ended esports (исторические)
print("\n=== Ended E-sports (последние завершённые) ===")
try:
    data = get("/v3/events/ended", {"sport_id": SPORT})
    events = data.get("results", [])
    total = data.get("pager", {}).get("total", 0)
    print(f"Всего завершённых: {total}")
    for e in events[:3]:
        home = e.get("home", {}).get("name", "?")
        away = e.get("away", {}).get("name", "?")
        ss   = e.get("ss", "?")  # score
        print(f"  [{e.get('id')}] {home} vs {away} | score={ss}")
except Exception as ex:
    print(f"  ERROR: {ex}")

# 3. Odds для первого события
print("\n=== Odds первого завершённого матча ===")
try:
    data = get("/v3/events/ended", {"sport_id": SPORT})
    events = data.get("results", [])
    if events:
        eid = events[0]["id"]
        home = events[0].get("home", {}).get("name", "?")
        away = events[0].get("away", {}).get("name", "?")
        print(f"Матч: {home} vs {away} (id={eid})")

        # Odds summary — открытие + закрытие
        odds = get("/v2/event/odds/summary", {"event_id": eid})
        print(f"Odds summary keys: {list(odds.get('results', {}).keys())[:10]}")
        r = odds.get("results", {})
        # Показать bet365 если есть
        for source in ["Bet365", "bet365", "1XBet", "Pinnacle", "GGBet"]:
            if source in r or source.lower() in {k.lower() for k in r}:
                key = next(k for k in r if k.lower() == source.lower())
                print(f"\n  {key}: {json.dumps(r[key], ensure_ascii=False)[:300]}")

        # Полная история движения
        print(f"\n=== Full odds history (source=bet365) ===")
        full = get("/v2/event/odds", {"event_id": eid, "source": "bet365", "since_time": "0"})
        results = full.get("results", {})
        for market, entries in list(results.items())[:2]:
            print(f"  Market {market}: {len(entries.get('odds', {}))} bookmakers")
            for bm, vals in list(entries.get("odds", {}).items())[:2]:
                print(f"    {bm}: {str(vals)[:120]}")

except Exception as ex:
    print(f"  ERROR: {ex}")

# 4. Поиск Dota 2 специфично
print("\n=== Поиск 'dota' в лигах ===")
try:
    data = get("/v3/events/upcoming", {"sport_id": SPORT, "page": 1})
    events = data.get("results", [])
    dota = [e for e in events if "dota" in str(e.get("league", {}).get("name", "")).lower()]
    print(f"Dota 2 матчей в upcoming: {len(dota)} из {len(events)}")
    for e in dota[:5]:
        print(f"  {e.get('home',{}).get('name','?')} vs {e.get('away',{}).get('name','?')} | {e.get('league',{}).get('name')}")
except Exception as ex:
    print(f"  ERROR: {ex}")

print("\n✓ Probe complete")
print(f"  Следующий шаг: python3 scripts/collect_odds.py --probe")
