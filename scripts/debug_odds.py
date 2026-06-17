#!/usr/bin/env python3
"""Debug: проверить что BetsAPI возвращает для конкретных event_id"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
from daily_strategy_run import api_get

EVENT_IDS = ['12053825', '12046912']

for eid in EVENT_IDS:
    print(f"\n{'='*50}")
    print(f"event_id: {eid}")

    # Статус матча
    d2 = api_get('/v2/event/view', {'event_id': eid})
    if d2:
        ev = (d2.get('results') or [{}])[0]
        print(f"  time_status: {ev.get('time_status')}  ss: {ev.get('ss')}")
        print(f"  home: {ev.get('home',{}).get('name')}  away: {ev.get('away',{}).get('name')}")
        print(f"  league: {ev.get('league',{}).get('name')}")
    else:
        print("  /v2/event/view -> None")

    # Котировки
    d = api_get('/v2/event/odds/summary', {'event_id': eid})
    if not d:
        print("  /v2/event/odds/summary -> None (API вернул success=0)")
        continue
    r = d.get('results') or {}
    print(f"  букмекеров в odds/summary: {len(r)}")
    if r:
        for bm, bd in list(r.items())[:3]:
            times = (bd.get('odds') or {}).get('end') or (bd.get('odds') or {}).get('start') or {}
            m1 = times.get('151_1') or {}
            print(f"    {bm}: 151_1 home={m1.get('home_od')} away={m1.get('away_od')}")
    else:
        # Показать сырой ответ
        print("  RAW:", json.dumps(d)[:300])
