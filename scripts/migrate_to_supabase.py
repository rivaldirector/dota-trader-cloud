#!/usr/bin/env python3
"""
Migrate local betsapi_harvest.db → Supabase PostgreSQL.

Migrates:
  - raw_events      (all Dota2 ended + upcoming)
  - odds_summary    (all rows)
  - live_snapshots  (all rows)

Run ONCE from project root:
    cd ~/Downloads/dota_trader_v2
    python3 scripts/migrate_to_supabase.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

URL = os.getenv("SUPABASE_URL", "")
KEY = os.getenv("SUPABASE_ANON_KEY", "")
HARVEST_DB = ROOT / "storage" / "betsapi_harvest.db"

HEADERS = {
    "apikey":        KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}

BATCH = 500   # rows per request
DELAY = 0.3   # seconds between requests


def sb_upsert(table: str, rows: list[dict]) -> None:
    r = requests.post(
        f"{URL}/rest/v1/{table}",
        headers=HEADERS,
        json=rows,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"[{table}] {r.status_code}: {r.text[:200]}")


def migrate_table(conn: sqlite3.Connection, sql: str, table: str,
                  row_fn, label: str) -> int:
    rows_all = conn.execute(sql).fetchall()
    total = len(rows_all)
    print(f"\n[{label}] {total:,} rows → {table}")

    inserted = 0
    for i in range(0, total, BATCH):
        batch = [row_fn(r) for r in rows_all[i:i+BATCH]]
        try:
            sb_upsert(table, batch)
            inserted += len(batch)
            pct = inserted / total * 100
            print(f"  {inserted:>7,} / {total:,} ({pct:.1f}%)", end="\r", flush=True)
            time.sleep(DELAY)
        except Exception as e:
            print(f"\n  ERROR at batch {i}: {e}")
            # retry once
            time.sleep(2)
            try:
                sb_upsert(table, batch)
                inserted += len(batch)
            except Exception as e2:
                print(f"  SKIP batch {i}: {e2}")

    print(f"  Done: {inserted:,} rows inserted/updated")
    return inserted


def main():
    if not HARVEST_DB.exists():
        print(f"ERROR: {HARVEST_DB} not found")
        sys.exit(1)
    if not URL or not KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_ANON_KEY missing in .env")
        sys.exit(1)

    conn = sqlite3.connect(HARVEST_DB)
    conn.row_factory = sqlite3.Row

    print("=" * 60)
    print("Dota Trader — SQLite → Supabase Migration")
    print("=" * 60)

    # ── 1. betsapi_events ────────────────────────────────────────
    migrate_table(
        conn,
        sql="""
            SELECT event_id, league, home_team, away_team,
                   start_time, status, score, winner, raw_json, fetched_at
            FROM raw_events
            WHERE sport_tag = 'dota2'
            ORDER BY start_time
        """,
        table="betsapi_events",
        row_fn=lambda r: {
            "event_id":   r["event_id"],
            "league":     r["league"],
            "home_team":  r["home_team"],
            "away_team":  r["away_team"],
            "start_time": r["start_time"],
            "status":     r["status"],
            "score":      r["score"],
            "winner":     r["winner"],
            "raw_json":   r["raw_json"],
            "fetched_at": r["fetched_at"],
        },
        label="1/2 betsapi_events",
    )

    # ── 2. betsapi_odds ──────────────────────────────────────────
    migrate_table(
        conn,
        sql="""
            SELECT os.event_id, os.bookmaker, os.market,
                   os.open_home, os.open_away,
                   os.close_home, os.close_away,
                   os.fetched_at
            FROM odds_summary os
            JOIN raw_events re ON os.event_id = re.event_id
            WHERE re.sport_tag = 'dota2'
            ORDER BY os.event_id, os.bookmaker
        """,
        table="betsapi_odds",
        row_fn=lambda r: {
            "event_id":   r["event_id"],
            "bookmaker":  r["bookmaker"],
            "market":     r["market"] or "151_1",
            "open_home":  r["open_home"],
            "open_away":  r["open_away"],
            "close_home": r["close_home"],
            "close_away": r["close_away"],
            "fetched_at": r["fetched_at"],
        },
        label="2/2 betsapi_odds",
    )

    conn.close()

    print("\n" + "=" * 60)
    print("Migration complete!")
    print("Next: run supabase_harvest.py for remaining BetsAPI days")
    print("=" * 60)


if __name__ == "__main__":
    main()
