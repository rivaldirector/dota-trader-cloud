#!/usr/bin/env python3
"""
BetsAPI Live Odds Collector — adaptive intervals.

Rules:
  > 3 hours to start  →  poll every 30 minutes
  < 3 hours to start  →  poll every 10 minutes
  < 1 hour  to start  →  poll every 5 minutes
  Live (started)       →  poll every 5 minutes

Run in background:
    nohup python3 scripts/betsapi_live_collector.py >> logs/live_collector.log 2>&1 &

Stop:
    kill $(cat /tmp/betsapi_live.pid)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
SPORT_ID = 151

DB_PATH  = PROJECT_ROOT / "storage" / "betsapi_harvest.db"
LOG_DIR  = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
PID_FILE = Path("/tmp/betsapi_live.pid")

DOTA_KEYWORDS = ["dota", "dota 2", "dota2"]

# Интервалы в секундах
INTERVAL_FAR    = 30 * 60   # > 3h
INTERVAL_NEAR   = 10 * 60   # < 3h
INTERVAL_CLOSE  =  5 * 60   # < 1h или live
MIN_REQ_DELAY   = 1.05       # между API вызовами

SCHEMA_LIVE = """
CREATE TABLE IF NOT EXISTS live_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    league       TEXT,
    home_team    TEXT,
    away_team    TEXT,
    start_time   INTEGER,
    seconds_to_start INTEGER,
    bookmaker    TEXT NOT NULL,
    market       TEXT DEFAULT '151_1',
    home_odds    REAL,
    away_odds    REAL,
    open_home    REAL,
    open_away    REAL,
    raw_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ls_event ON live_snapshots(event_id);
CREATE INDEX IF NOT EXISTS idx_ls_time  ON live_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_ls_bm    ON live_snapshots(bookmaker);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_LIVE)
    conn.commit()
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts() -> int:
    return int(time.time())


def _safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class RateLimitedSession:
    def __init__(self, token: str):
        self.token   = token
        self._last   = 0.0
        self._total  = 0
        self._errors = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "DotaLiveCollector/1.0"})

    def get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.time() - self._last
        if elapsed < MIN_REQ_DELAY:
            time.sleep(MIN_REQ_DELAY - elapsed)

        p = {"token": self.token, **(params or {})}
        try:
            r = self.session.get(f"{BASE}{path}", params=p, timeout=20)
            self._last   = time.time()
            self._total += 1

            if r.status_code == 429:
                print(f"[429] rate limit — sleep 5 min", flush=True)
                time.sleep(300)
                return self.get(path, params)

            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                raise RuntimeError(f"API error: {data}")
            return data

        except Exception as e:
            self._errors += 1
            print(f"[API ERROR] {path}: {e}", flush=True)
            raise


def fetch_upcoming_dota2(api: RateLimitedSession) -> list[dict]:
    """Get all upcoming Dota 2 events."""
    results = []
    page = 1
    while True:
        data  = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
        items = data.get("results", [])
        if not items:
            break
        total = data.get("pager", {}).get("total", 0)
        for e in items:
            league = (e.get("league") or {}).get("name", "").lower()
            if any(k in league for k in DOTA_KEYWORDS):
                results.append(e)
        if page * 50 >= total:
            break
        page += 1
    return results


def fetch_odds_summary(api: RateLimitedSession, event_id: str) -> dict:
    return api.get("/v2/event/odds/summary", {"event_id": event_id})


def parse_summary_bms(data: dict) -> list[dict]:
    """Extract per-bookmaker open/close from summary."""
    bms = []
    results = data.get("results", {})
    for bm_name, bm_data in results.items():
        odds  = bm_data.get("odds", {})
        start = odds.get("start", {})
        end   = odds.get("end", {}) or start

        mk_s = start.get("151_1", {}) or start.get("1_1", {}) or {}
        mk_e = end.get("151_1", {})   or end.get("1_1", {})   or mk_s

        oh = _safe_float(mk_s.get("home_od") or mk_s.get("1"))
        oa = _safe_float(mk_s.get("away_od") or mk_s.get("2"))
        ch = _safe_float(mk_e.get("home_od") or mk_e.get("1"))
        ca = _safe_float(mk_e.get("away_od") or mk_e.get("2"))

        if oh or oa or ch or ca:
            bms.append({
                "bookmaker":  bm_name,
                "open_home":  oh,
                "open_away":  oa,
                "close_home": ch or oh,
                "close_away": ca or oa,
            })
    return bms


def collect_snapshot(conn: sqlite3.Connection, api: RateLimitedSession,
                     event: dict) -> int:
    """Collect current odds snapshot for one event. Returns rows inserted."""
    eid        = str(event.get("id", ""))
    league     = (event.get("league") or {}).get("name", "")
    home       = (event.get("home")   or {}).get("name", "")
    away       = (event.get("away")   or {}).get("name", "")
    start_time = event.get("time")

    try:
        data   = fetch_odds_summary(api, eid)
        bms    = parse_summary_bms(data)
        raw    = json.dumps(data, ensure_ascii=False)
        cap_at = now_iso()
        secs   = (int(start_time) - now_ts()) if start_time else None
        inserted = 0

        for bm in bms:
            # Current live odds = close odds (most recent)
            cur_home = bm["close_home"] or bm["open_home"]
            cur_away = bm["close_away"] or bm["open_away"]

            conn.execute(
                """INSERT INTO live_snapshots
                   (captured_at, event_id, league, home_team, away_team,
                    start_time, seconds_to_start, bookmaker, market,
                    home_odds, away_odds, open_home, open_away, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cap_at, eid, league, home, away,
                 start_time, secs, bm["bookmaker"], "151_1",
                 cur_home, cur_away,
                 bm["open_home"], bm["open_away"], raw)
            )
            inserted += 1

        conn.commit()
        return inserted

    except Exception as e:
        print(f"  [WARN] {home} vs {away} ({eid}): {e}", flush=True)
        return 0


def get_poll_interval(start_time: int | None) -> int:
    """Return polling interval in seconds based on time to event start."""
    if not start_time:
        return INTERVAL_CLOSE  # unknown → treat as close
    secs = int(start_time) - now_ts()
    if secs > 3 * 3600:
        return INTERVAL_FAR
    elif secs > 3600:
        return INTERVAL_NEAR
    else:
        return INTERVAL_CLOSE


def main_loop():
    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN not set in .env")
        sys.exit(1)

    # Write PID
    PID_FILE.write_text(str(os.getpid()))
    print(f"Live collector PID={os.getpid()} | DB={DB_PATH}", flush=True)

    conn = open_db()
    api  = RateLimitedSession(TOKEN)

    total_snapshots = 0
    cycles = 0

    while True:
        cycle_start = time.time()
        cycles += 1
        print(f"\n[{now_iso()}] === Cycle {cycles} ===", flush=True)

        try:
            events = fetch_upcoming_dota2(api)
            print(f"  Found {len(events)} upcoming Dota 2 events", flush=True)
        except Exception as e:
            print(f"  [ERROR] fetch upcoming: {e}", flush=True)
            time.sleep(60)
            continue

        if not events:
            # No events — sleep max interval and check again
            print(f"  No events, sleeping {INTERVAL_FAR//60} min", flush=True)
            time.sleep(INTERVAL_FAR)
            continue

        # Determine next cycle time based on the most urgent event
        min_interval = INTERVAL_FAR
        cycle_rows = 0

        for event in events:
            st = event.get("time")
            interval = get_poll_interval(st)
            min_interval = min(min_interval, interval)

            home = (event.get("home") or {}).get("name", "?")
            away = (event.get("away") or {}).get("name", "?")
            secs = (int(st) - now_ts()) if st else None
            lbl  = f"{secs//3600}h{(secs%3600)//60}m" if secs and secs > 0 else "LIVE"
            print(f"  [{lbl:<8}] {home} vs {away}", flush=True)

            rows = collect_snapshot(conn, api, event)
            cycle_rows    += rows
            total_snapshots += rows

        print(f"\n  Cycle {cycles} done: +{cycle_rows} rows | total={total_snapshots} | "
              f"api_calls={api._total} | next_in={min_interval//60}min", flush=True)

        # Sleep until next poll
        elapsed = time.time() - cycle_start
        sleep_s = max(0, min_interval - elapsed)
        print(f"  Sleeping {sleep_s:.0f}s...", flush=True)
        time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print(f"\n[!] Live collector stopped", flush=True)
        PID_FILE.unlink(missing_ok=True)
