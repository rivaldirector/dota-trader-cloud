#!/usr/bin/env python3
"""
fetch_pandascore_history_cloud.py — облачная версия scripts/fetch_pandascore_history.py
(Mac). Тянет ИСТОРИЧЕСКИЕ finished-матчи Dota2 из PandaScore за последние N
дней и upsert'ит в Supabase-таблицу elo_pandascore_history (НЕ трогает
betsapi_events).

Зачем: betsapi_events недообсчитан по части лиг/брэкетов (TI Quals, EPL —
см. локальный анализ check_pandascore_coverage.py). PandaScore покрывает их
полнее. Эта история используется ТОЛЬКО для более точного Elo team-rating —
коэффициентов тут нет и не будет, PandaScore не odds-провайдер.

Run:
    python3 scripts/fetch_pandascore_history_cloud.py            # 60 дней назад
    python3 scripts/fetch_pandascore_history_cloud.py --days 120

GitHub Actions: раз в 6-12ч (история обновляется медленно) — см.
pandascore_history_pipeline.yml. Требует секрет PANDASCORE_TOKEN.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
PANDASCORE_TOKEN = os.getenv("PANDASCORE_TOKEN", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

PS_BASE = "https://api.pandascore.co/dota2/matches"


def to_unix(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def fetch_finished_range(token: str, since_dt: datetime, until_dt: datetime) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    rng = f"{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{until_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    all_matches: list[dict] = []
    page = 1
    while page <= 50:
        params = {
            "filter[status]": "finished",
            "range[end_at]": rng,
            "per_page": "100",
            "page": str(page),
            "sort": "end_at",
        }
        try:
            r = requests.get(PS_BASE, headers=headers, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as ex:
            print(f"  [WARN] PandaScore page {page}: {ex}")
            break
        if not data:
            break
        all_matches.extend(data)
        print(f"   page {page}: +{len(data)} (всего {len(all_matches)})")
        if len(data) < 100:
            break
        page += 1
    return all_matches


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="Сколько дней истории назад тянуть")
    args = ap.parse_args()

    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)
    if not PANDASCORE_TOKEN:
        print("ERROR: missing PANDASCORE_TOKEN — добавь секрет в GitHub Actions")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=args.days)
    print(f"Тянем PandaScore finished Dota2-матчи: {since_dt} .. {now} ({args.days} дн.)")

    matches = fetch_finished_range(PANDASCORE_TOKEN, since_dt, now)
    print(f"\nВсего получено: {len(matches)} матчей")

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    skipped_no_st = 0
    for m in matches:
        opps = m.get("opponents", [])
        if len(opps) != 2:
            continue
        home = (opps[0].get("opponent") or {}).get("name", "?")
        away = (opps[1].get("opponent") or {}).get("name", "?")
        league = (m.get("league") or {}).get("name", "?")
        st = to_unix(m.get("begin_at") or m.get("scheduled_at"))
        if st is None:
            skipped_no_st += 1
            continue
        winner = (m.get("winner") or {}).get("name") if m.get("winner") else None
        ps_id = m.get("id")
        if ps_id is None:
            continue

        rows.append({
            "ps_id": ps_id,
            "home_team": home,
            "away_team": away,
            "league": f"DOTA2 - {league}",
            "start_time": st,
            "winner": winner,
            "status": "finished",
            "fetched_at": now_iso,
        })

    sb_upsert("elo_pandascore_history", rows, on_conflict="ps_id")
    no_winner = sum(1 for r in rows if r["winner"] is None)
    print(f"Upsert в elo_pandascore_history: {len(rows)} (без winner: {no_winner}, без start_time: {skipped_no_st})")


if __name__ == "__main__":
    main()
