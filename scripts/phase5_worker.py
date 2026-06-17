#!/usr/bin/env python3
"""
Phase 5 Sharded Worker — параллельный сбор odds_history по диапазонам дат.

Читает события из основной DB (READ-ONLY).
Пишет в изолированную шард-DB — безопасен для параллельного запуска.

Rate limit: 1800 req/hour TOTAL на аккаунт.
  1 воркер  → --interval 2.0   (1800/hour, ПОЛНЫЙ лимит)
  2 воркера → --interval 4.0 каждый (900 × 2 = 1800/hour)
  При 429: авто-cooldown 60→120→300→600s, затем retry — без потери прогресса.

Usage:
    python3 scripts/phase5_worker.py \\
        --date-from 2025-01-01 --date-to 2026-12-31 \\
        --shard-db storage/phase5_dota_2025_2026.db \\
        --interval 6.5 --sport dota2

    # dry-run (не пишет в БД):
    python3 scripts/phase5_worker.py \\
        --date-from 2025-01-01 --date-to 2026-12-31 \\
        --shard-db storage/phase5_dota_2025_2026.db \\
        --interval 6.5 --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

TOKEN   = os.getenv("BETSAPI_TOKEN", "")
BASE    = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
MAIN_DB = ROOT / "storage" / "betsapi_harvest.db"

SHARD_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS odds_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    market      TEXT NOT NULL,
    snapshot_id TEXT,
    home_od     REAL,
    away_od     REAL,
    over_od     REAL,
    under_od    REAL,
    handicap    TEXT,
    ss          TEXT,
    add_time    INTEGER,
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_oh_event  ON odds_history(event_id);
CREATE INDEX IF NOT EXISTS idx_oh_market ON odds_history(market);

CREATE TABLE IF NOT EXISTS worker_progress (
    event_id   TEXT PRIMARY KEY,
    done       INTEGER DEFAULT 0,   -- 1=success, -1=error, -2=empty
    pts_count  INTEGER DEFAULT 0,
    done_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_wp_done ON worker_progress(done);

CREATE TABLE IF NOT EXISTS worker_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_shard(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SHARD_SCHEMA)
    conn.commit()
    return conn


def open_main_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_float(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def fetch_odds_history(session: requests.Session, event_id: str,
                       last_req: list, interval: float) -> dict:
    """Делает запрос с соблюдением интервала. При 429 — авто-cooldown с retry."""
    COOLDOWN_STEPS = [60, 120, 300, 600]  # секунд ожидания на 1-й, 2-й, 3-й, 4-й 429

    for attempt, cooldown in enumerate([0] + COOLDOWN_STEPS):
        if cooldown:
            print(f"\n  [429] Rate limit — cooldown {cooldown}s (попытка {attempt}/{len(COOLDOWN_STEPS)})...",
                  flush=True)
            time.sleep(cooldown)

        elapsed = time.time() - last_req[0]
        if elapsed < interval:
            time.sleep(interval - elapsed)

        url = f"{BASE}/v2/event/odds"
        r = session.get(url,
                        params={"token": TOKEN, "event_id": event_id, "since_time": "0"},
                        timeout=20)
        last_req[0] = time.time()

        if r.status_code == 429:
            if attempt == len(COOLDOWN_STEPS):
                raise RuntimeError("429 persistent — все retry исчерпаны")
            continue  # уходим на следующий cooldown

        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"API error: {data}")
        return data

    raise RuntimeError("fetch_odds_history: недостижимый код")


def parse_history(data: dict) -> list[dict]:
    rows = []
    results = data.get("results", {})
    if not isinstance(results, dict):
        return rows
    odds = results.get("odds", {})
    if not isinstance(odds, dict):
        return rows
    for market_key, snapshots in odds.items():
        if not isinstance(snapshots, list):
            continue
        for snap in snapshots:
            if not isinstance(snap, dict):
                continue
            rows.append({
                "market":      market_key,
                "snapshot_id": snap.get("id"),
                "home_od":     _safe_float(snap.get("home_od")),
                "away_od":     _safe_float(snap.get("away_od")),
                "over_od":     _safe_float(snap.get("over_od")),
                "under_od":    _safe_float(snap.get("under_od")),
                "handicap":    snap.get("handicap"),
                "ss":          snap.get("ss"),
                "add_time":    snap.get("add_time"),
                "raw_json":    json.dumps(snap, ensure_ascii=False),
            })
    return rows


def write_rows(shard: sqlite3.Connection, event_id: str, rows: list[dict]):
    for pt in rows:
        shard.execute(
            """INSERT INTO odds_history
               (event_id,market,snapshot_id,home_od,away_od,
                over_od,under_od,handicap,ss,add_time,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, pt["market"], pt["snapshot_id"],
             pt["home_od"], pt["away_od"],
             pt["over_od"], pt["under_od"],
             pt["handicap"], pt["ss"],
             pt["add_time"], pt["raw_json"])
        )
    status = 1 if rows else -2  # -2 = empty response (no data, not error)
    shard.execute(
        """INSERT OR REPLACE INTO worker_progress(event_id,done,pts_count,done_at)
           VALUES(?,?,?,?)""",
        (event_id, status, len(rows), now_iso())
    )
    shard.commit()


def main():
    parser = argparse.ArgumentParser(description="Phase 5 Sharded Worker")
    parser.add_argument("--date-from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to",   required=True, help="YYYY-MM-DD")
    parser.add_argument("--shard-db",  required=True, help="Путь к шард-БД")
    parser.add_argument("--sport",     default="dota2",
                        help="dota2 / cs2 / lol / valorant (default: dota2)")
    parser.add_argument("--interval",  type=float, default=2.0,
                        help="Секунд между запросами (default: 2.0 = 1800 req/hour = полный лимит)")
    parser.add_argument("--limit",     type=int,   default=None,
                        help="Обработать только N событий (для теста)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Делает API-запросы, НЕ пишет в БД")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN not set in .env"); sys.exit(1)
    if not MAIN_DB.exists():
        print(f"ERROR: Main DB not found: {MAIN_DB}"); sys.exit(1)

    shard_path = Path(args.shard_db)
    shard = open_shard(shard_path)
    main_conn = open_main_readonly()

    # Сохраняем метаданные воркера
    shard.execute("INSERT OR REPLACE INTO worker_meta VALUES('sport',?)", (args.sport,))
    shard.execute("INSERT OR REPLACE INTO worker_meta VALUES('date_from',?)", (args.date_from,))
    shard.execute("INSERT OR REPLACE INTO worker_meta VALUES('date_to',?)", (args.date_to,))
    shard.execute("INSERT OR REPLACE INTO worker_meta VALUES('started_at',?)", (now_iso(),))
    shard.commit()

    # Конвертируем даты в Unix timestamp
    ts_from = int(datetime.strptime(args.date_from, "%Y-%m-%d").timestamp())
    ts_to   = int(datetime.strptime(args.date_to,   "%Y-%m-%d").timestamp()) + 86400

    # Получаем события из основной БД
    all_events = main_conn.execute("""
        SELECT re.event_id, re.home_team, re.away_team, re.start_time, re.league
        FROM raw_events re
        JOIN harvest_progress hp ON re.event_id = hp.event_id
        WHERE re.sport_tag = ?
          AND re.status = 'ended'
          AND hp.summary_done = 1
          AND re.start_time BETWEEN ? AND ?
        ORDER BY re.start_time DESC
    """, (args.sport, ts_from, ts_to)).fetchall()

    # Фильтруем уже обработанные этим воркером
    done_ids = {r[0] for r in shard.execute(
        "SELECT event_id FROM worker_progress WHERE done != 0"
    ).fetchall()}

    events = [e for e in all_events if e["event_id"] not in done_ids]
    if args.limit:
        events = events[:args.limit]

    total_in_range = len(all_events)
    already_done   = len(done_ids)
    remaining      = len(events)

    req_per_hour = 3600 / args.interval
    eta_h = remaining * args.interval / 3600

    print(f"\n{'='*60}")
    print(f"Phase 5 Worker [{args.sport.upper()}] {args.date_from} → {args.date_to}")
    print(f"Shard DB:  {shard_path}")
    print(f"Interval:  {args.interval}s ({req_per_hour:.0f} req/hour)")
    print(f"Range:     {total_in_range} events total")
    print(f"Done:      {already_done} already processed")
    print(f"Remaining: {remaining} | ETA: {eta_h:.1f}h")
    if args.dry_run:
        print("MODE:      DRY RUN (не пишет в БД)")
    print(f"{'='*60}\n")

    if remaining == 0:
        print("✓ All done in this date range!")
        shard.execute("INSERT OR REPLACE INTO worker_meta VALUES('finished_at',?)", (now_iso(),))
        shard.commit()
        return

    session = requests.Session()
    session.headers.update({"User-Agent": "DotaWorker/1.0"})
    last_req = [0.0]

    inserted_total = 0
    empty_count    = 0
    error_count    = 0

    for i, row in enumerate(events):
        eid   = row["event_id"]
        label = f"{row['home_team']} vs {row['away_team']}"

        try:
            data = fetch_odds_history(session, eid, last_req, args.interval)
            pts  = parse_history(data)

            status_str = f"→ {len(pts)} snapshots"
            if len(pts) == 0:
                empty_count += 1
                status_str = "→ 0 snapshots (empty)"

            if args.dry_run:
                print(f"  [{i+1}/{remaining}] {label} {status_str} [DRY RUN]", flush=True)
            else:
                write_rows(shard, eid, pts)
                inserted_total += len(pts)
                print(f"  [{i+1}/{remaining}] {label} {status_str}", flush=True)

            if (i + 1) % 50 == 0:
                done_now = already_done + i + 1
                pct = done_now / total_in_range * 100
                remain_h = (remaining - i - 1) * args.interval / 3600
                print(f"\n  [{pct:.1f}%] {done_now}/{total_in_range} | "
                      f"inserted={inserted_total} empty={empty_count} err={error_count} | "
                      f"ETA={remain_h:.1f}h\n", flush=True)

        except KeyboardInterrupt:
            print("\n[!] Interrupted — прогресс сохранён. Запусти снова для продолжения.")
            break

        except Exception as e:
            error_count += 1
            print(f"  [ERR] {eid} {label}: {e}", flush=True)
            if not args.dry_run:
                shard.execute(
                    "INSERT OR REPLACE INTO worker_progress(event_id,done,done_at) VALUES(?,-1,?)",
                    (eid, now_iso())
                )
                shard.commit()

    # Финальный отчёт
    total_in_shard = shard.execute("SELECT COUNT(*) FROM odds_history").fetchone()[0]
    done_in_shard  = shard.execute("SELECT COUNT(*) FROM worker_progress WHERE done=1").fetchone()[0]
    empty_in_shard = shard.execute("SELECT COUNT(*) FROM worker_progress WHERE done=-2").fetchone()[0]
    err_in_shard   = shard.execute("SELECT COUNT(*) FROM worker_progress WHERE done=-1").fetchone()[0]

    print(f"\n{'='*60}")
    print(f"WORKER REPORT [{args.sport.upper()}] {args.date_from}→{args.date_to}")
    print(f"  odds_history rows:  {total_in_shard}")
    print(f"  Events done (data): {done_in_shard}")
    print(f"  Events empty:       {empty_in_shard}")
    print(f"  Events errors:      {err_in_shard}")
    print(f"  Remaining:          {total_in_range - done_in_shard - empty_in_shard}")
    print(f"{'='*60}")

    shard.execute("INSERT OR REPLACE INTO worker_meta VALUES('finished_at',?)", (now_iso(),))
    shard.commit()
    shard.close()
    main_conn.close()


if __name__ == "__main__":
    main()
