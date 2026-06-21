#!/usr/bin/env python3
"""
Dota Live Watch — снэпшот текущих live pro-матчей Dota2 (длительность, килы)
через бесплатный публичный эндпоинт OpenDota. Не требует BetsAPI/токена —
отдельный источник, чисто игровая статистика (не коэффициенты).

OpenDota /api/live отдаёт ВСЕ текущие live-игры (включая обычные пабы).
Фильтруем по league_id != 0 — это и есть pro/tournament матчи.

Run:
    python3 scripts/dota_live_watch.py

GitHub Actions: каждые 15 минут (короткий cron, отдельный от daily_pipeline).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

OPENDOTA_LIVE_URL = "https://api.opendota.com/api/live"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers=SB_HEADERS, json=rows, timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] upsert {table}: {r.status_code} {r.text[:200]}")


def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    try:
        r = requests.get(OPENDOTA_LIVE_URL, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as ex:
        print(f"ERROR: OpenDota live недоступен: {ex}")
        sys.exit(1)

    pro_matches = [m for m in data if m.get("league_id", 0)]
    print(f"Всего live игр: {len(data)}  |  pro (league_id!=0): {len(pro_matches)}")

    now = now_iso()
    rows = []
    for m in pro_matches:
        rows.append({
            "match_id": str(m.get("match_id")),
            "league_id": m.get("league_id"),
            "team_radiant": m.get("team_name_radiant") or None,
            "team_dire": m.get("team_name_dire") or None,
            "radiant_score": m.get("radiant_score"),
            "dire_score": m.get("dire_score"),
            "game_time_sec": m.get("game_time"),
            "delay_sec": m.get("delay"),
            "last_seen_at": now,
        })
        mins, secs = divmod(int(m.get("game_time") or 0), 60)
        print(f"  [{m.get('league_id')}] {m.get('team_name_radiant') or '?'} vs "
              f"{m.get('team_name_dire') or '?'}  {mins:02d}:{secs:02d}  "
              f"килы {m.get('radiant_score')}:{m.get('dire_score')}")

    sb_upsert("dota_live_matches", rows, on_conflict="match_id")
    print(f"\nЗаписано в dota_live_matches: {len(rows)}")


if __name__ == "__main__":
    main()
