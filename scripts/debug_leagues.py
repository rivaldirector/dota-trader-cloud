#!/usr/bin/env python3
"""Показывает все лиги из BetsAPI sport_id=151 — чтобы понять что там есть."""
import os, time, requests
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
TOKEN = os.getenv("BETSAPI_TOKEN", "")
BASE  = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")

def api_get(path, params={}):
    time.sleep(2.1)
    try:
        r = requests.get(f"{BASE}{path}", params={"token": TOKEN, **params}, timeout=15)
        r.raise_for_status()
        d = r.json()
        return d if d.get("success") else None
    except Exception as e:
        print(f"[Ошибка] {e}")
        return None

leagues = defaultdict(int)

for endpoint in ["/v3/events/upcoming", "/v3/events/inplay"]:
    d = api_get(endpoint, {"sport_id": 151})
    if not d:
        continue
    for e in (d.get("results") or []):
        name = (e.get("league") or {}).get("name", "???")
        home = (e.get("home") or {}).get("name", "?")
        away = (e.get("away") or {}).get("name", "?")
        leagues[name] += 1

print(f"\nВсе лиги в sport_id=151 ({sum(leagues.values())} матчей):\n")
for name, count in sorted(leagues.items(), key=lambda x: -x[1]):
    print(f"  {count:>3}  {name}")
