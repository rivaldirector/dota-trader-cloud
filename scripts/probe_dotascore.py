#!/usr/bin/env python3
"""
DotaScore API reverse engineering probe.
Найдены правильные эндпоинты через OpenAPI + health/upstreams.

Запуск:
    python3 scripts/probe_dotascore.py
"""
import sys, json, requests
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from config import settings

KEY  = settings.dotascore_api_key   # dks_8Bvjde06jXSkkb3p2I55Jr7o
BASE = "https://api.dotascore.live"

# Четыре варианта авторизации — пробуем все
AUTH_VARIANTS = [
    ("Bearer",          {"Authorization": f"Bearer {KEY}"}),
    ("X-Api-Key",       {"X-Api-Key": KEY}),
    ("X-DotaScore-Key", {"X-DotaScore-Key": KEY}),
    ("query ?api_key",  {}),   # ключ в query param
    ("query ?key",      {}),   # ключ в query param
]

ENDPOINTS = [
    "/v1/matches",
    "/v1/matches/upcoming",
    "/v1/matches/live",
    "/v1/odds",
    "/v1/odds/upcoming",
    "/v1/teams",
    "/v1/leagues",
    "/v1/players",
]

def probe():
    print(f"Key: {KEY[:12]}...")
    print(f"Base: {BASE}\n")

    # 1. Проверить что API жив
    r = requests.get(f"{BASE}/health", timeout=10)
    print(f"Health: {r.json()}\n")

    # 2. Перебираем auth варианты на /v1/matches
    print("=== AUTH DETECTION (/v1/matches) ===")
    working_auth = None
    for name, headers in AUTH_VARIANTS:
        params = {"limit": 1}
        if name == "query ?api_key":
            params["api_key"] = KEY
        elif name == "query ?key":
            params["key"] = KEY

        try:
            r = requests.get(f"{BASE}/v1/matches", headers=headers,
                             params=params, timeout=10)
            body = r.text[:200]
            print(f"  [{name:20}] {r.status_code} | {body}")
            if r.status_code == 200:
                working_auth = (name, headers, params.get("api_key") or params.get("key"))
                print(f"  ✓ FOUND WORKING AUTH: {name}")
                break
        except Exception as e:
            print(f"  [{name:20}] ERROR: {e}")

    if not working_auth:
        print("\n  ✗ No working auth found with existing key")
        print("  → Key may be invalid or expired")
        print("  → Try: curl -H 'Authorization: Bearer <key>' https://api.dotascore.live/v1/matches")
        return

    # 3. Если нашли auth — тестируем все эндпоинты
    auth_name, auth_headers, auth_qparam = working_auth
    print(f"\n=== ALL ENDPOINTS (auth={auth_name}) ===")
    for endpoint in ENDPOINTS:
        params = {"limit": 3}
        if auth_qparam:
            params["api_key" if "api_key" in auth_name else "key"] = KEY
        try:
            r = requests.get(f"{BASE}{endpoint}", headers=auth_headers,
                             params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    print(f"  ✓ {endpoint}: list[{len(data)}]", end="")
                    if data:
                        print(f" keys={list(data[0].keys())[:8]}")
                    else:
                        print(" (empty)")
                elif isinstance(data, dict):
                    print(f"  ✓ {endpoint}: dict keys={list(data.keys())[:8]}")
            else:
                print(f"  ✗ {endpoint}: {r.status_code}")
        except Exception as e:
            print(f"  ✗ {endpoint}: {e}")

    # 4. Детально смотрим /v1/odds
    print(f"\n=== ODDS DETAIL ===")
    params = {"limit": 5}
    if auth_qparam:
        params[auth_qparam] = KEY
    r = requests.get(f"{BASE}/v1/odds", headers=auth_headers,
                     params=params, timeout=10)
    if r.status_code == 200:
        data = r.json()
        print(json.dumps(data[:2] if isinstance(data, list) else data, indent=2)[:2000])
    else:
        print(f"Status: {r.status_code} | {r.text[:300]}")

    # 5. Pinnacle через RapidAPI (найдено в health/upstreams)
    print(f"\n=== PINNACLE via RapidAPI (прямой доступ) ===")
    print("  URL: pinnacle-betting-odds.p.rapidapi.com/kit/v1/markets")
    print("  sport_id=10 (Dota 2 на Pinnacle)")
    print("  Нужен RapidAPI ключ — отдельный от DotaScore")
    print("  Регистрация: https://rapidapi.com/theoddsapi/api/pinnacle-betting-odds")

if __name__ == "__main__":
    probe()
