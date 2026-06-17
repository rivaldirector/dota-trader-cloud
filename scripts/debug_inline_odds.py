#!/usr/bin/env python3
"""Проверить: есть ли котировки встроены в upcoming-событие или через другой endpoint"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
from daily_strategy_run import api_get, SPORT_ID

TARGET_IDS = {'12053825', '12046912'}

print("=== 1. /v3/events/upcoming с odds=1 ===")
d = api_get("/v3/events/upcoming", {"sport_id": SPORT_ID, "odds": 1, "page": 1})
for ev in (d.get("results") or []):
    eid = str(ev.get("id",""))
    if eid in TARGET_IDS:
        home = ev.get("home",{}).get("name","?")
        away = ev.get("away",{}).get("name","?")
        print(f"\n{home} vs {away}  (id={eid})")
        # Посмотреть все поля с odds/bet
        for k, v in ev.items():
            if "odd" in k.lower() or "bet" in k.lower() or k in ("1_1","1_2","odds"):
                print(f"  {k}: {v}")

print("\n=== 2. /v3/events/odds для этих event_id ===")
for eid in TARGET_IDS:
    d2 = api_get("/v3/events/odds", {"event_id": eid})
    print(f"\nevent_id={eid}: {json.dumps(d2)[:300] if d2 else 'None'}")

print("\n=== 3. Полный raw JSON для одного матча (upcoming, page 1-5) ===")
found = False
for page in range(1, 6):
    d = api_get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
    for ev in (d.get("results") or []):
        if str(ev.get("id","")) in TARGET_IDS and not found:
            print(json.dumps(ev, indent=2)[:1500])
            found = True
            break
    if found:
        break
if not found:
    print("Матчи не найдены в upcoming (возможно уже завершены)")
