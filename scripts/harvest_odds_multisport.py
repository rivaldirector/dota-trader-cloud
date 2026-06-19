#!/usr/bin/env python3
"""
Collect odds for CS2 / LoL / Valorant events from BetsAPI.
Reads event_ids from local SQLite, fetches odds/summary, saves to Supabase.

Run:
    cd ~/Downloads/dota_trader_v2
    python3 scripts/harvest_odds_multisport.py

Takes ~20-40 hours to collect all odds. Can be stopped and resumed.
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

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_ANON_KEY", "")
HARVEST_DB    = ROOT / "storage" / "betsapi_harvest.db"

if not all([BETSAPI_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Missing env vars"); sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

SPORTS = ["valorant", "lol", "cs2"]    # which disciplines to collect
SPORT_PRIORITY = "valorant"            # do this one first — smallest, currently
                                        # 0% covered, fastest path to a clean
                                        # full dataset for a 4th discipline
REQ_INTERVAL  = 2.0                    # API limit = 1800 req/hour = exactly 1 every 2.0s.
                                        # No extra buffer needed: the interval timer measures
                                        # elapsed since the *previous* request returned, and
                                        # parsing/Supabase-upsert work between requests already
                                        # eats real wall time on top of the sleep — so actual
                                        # achieved rate stays at/under 1800/h even at this setting.
BATCH_SIZE    = 300                    # upsert batch size to Supabase
START_FROM_YEAR = 2022                 # fallback floor, see MAX_AGE_DAYS below
MAX_AGE_DAYS  = 450                     # only fetch events from last N days —
                                        # this is what the backtest/strategy actually
                                        # uses; fetching back to 2022 wastes most
                                        # calls on data that will never be analyzed

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}

CONFLICT_COLS = {
    "betsapi_events": "event_id",
    "betsapi_odds":   "event_id,bookmaker,market",
}


# ── Supabase ──────────────────────────────────────────────────────────────────

def sb_upsert(table: str, rows: list[dict]) -> bool:
    if not rows:
        return True
    conflict = CONFLICT_COLS.get(table, "")
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if conflict:
        url += f"?on_conflict={conflict}"
    r = requests.post(url, headers=SB_HEADERS, json=rows, timeout=30)
    if r.status_code not in (200, 201):
        print(f"\n  [SB ERROR] {table}: {r.status_code} {r.text[:150]}")
        return False
    return True

def sb_get_existing_odds(event_ids: list[str]) -> set[str]:
    """Return set of event_ids already in betsapi_odds."""
    if not event_ids:
        return set()
    # Query in chunks of 100
    existing = set()
    for i in range(0, len(event_ids), 100):
        chunk = event_ids[i:i+100]
        ids_str = ",".join(f'"{e}"' for e in chunk)
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/betsapi_odds?select=event_id&event_id=in.({ids_str})&limit=1000",
            headers={**SB_HEADERS, "Prefer": "return=representation"},
            timeout=20,
        )
        if r.status_code == 200:
            existing.update(row["event_id"] for row in r.json())
    return existing


# ── BetsAPI ───────────────────────────────────────────────────────────────────
# Confirmed limit: 1800 req/hour = 1 every 2.0s. REQ_INTERVAL already sits right
# at that line (+0.05s buffer), so MIN_INTERVAL == REQ_INTERVAL — the "speedup"
# logic below is now a no-op safety net (interval never drops below the known
# cap), and only the 429 backoff actually does anything if we still get throttled
# (e.g. limit is shared across other tools using the same token).

MIN_INTERVAL       = REQ_INTERVAL
MAX_INTERVAL       = 3.0
SPEEDUP_EVERY       = 25     # successes before trying a faster interval (inert at floor)
SPEEDUP_STEP        = 0.05
SLOWDOWN_STEP       = 0.25
INITIAL_429_BACKOFF = 15     # seconds, doubles on repeated 429s
MAX_429_BACKOFF     = 180

class BetsAPI:
    def __init__(self, token: str):
        self.token = token
        self._last = 0.0
        self.calls = 0
        self.interval = REQ_INTERVAL
        self.streak = 0
        self.session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict | None:
        elapsed = time.time() - self._last
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        p = {"token": self.token, **(params or {})}
        backoff = INITIAL_429_BACKOFF
        for attempt in range(5):
            try:
                r = self.session.get(
                    f"https://api.b365api.com{path}", params=p, timeout=20
                )
                self._last = time.time()
                self.calls += 1
                if r.status_code == 429:
                    self.interval = min(MAX_INTERVAL, self.interval + SLOWDOWN_STEP)
                    self.streak = 0
                    print(f"\n  [429] rate limit — backoff {backoff}s, "
                          f"interval now {self.interval:.2f}s")
                    time.sleep(backoff)
                    backoff = min(MAX_429_BACKOFF, backoff * 2)
                    continue
                r.raise_for_status()
                data = r.json()
                if not data.get("success"):
                    return None
                self.streak += 1
                if self.streak >= SPEEDUP_EVERY and self.interval > MIN_INTERVAL:
                    self.interval = round(max(MIN_INTERVAL, self.interval - SPEEDUP_STEP), 2)
                    self.streak = 0
                    print(f"  [speedup] interval now {self.interval:.2f}s", flush=True)
                return data
            except Exception as e:
                if attempt == 4:
                    return None
                time.sleep(3)
        return None


def connect_with_retry(db_path, retries: int = 5, delay: float = 2.0) -> sqlite3.Connection:
    """Open the SQLite connection with a busy_timeout, retrying on transient
    'disk I/O error' / 'database is locked' from other scripts hitting the
    same file (paper_trading.py, live_poller.py, phase5_worker.py, etc.)."""
    last_err = None
    for attempt in range(retries):
        try:
            c = sqlite3.connect(db_path, timeout=30)
            c.execute("PRAGMA busy_timeout = 30000")
            return c
        except sqlite3.OperationalError as e:
            last_err = e
            print(f"  [DB] connect failed ({e}), retry {attempt+1}/{retries}...")
            time.sleep(delay)
    raise last_err


def query_with_retry(conn: sqlite3.Connection, db_path, sql: str, params=(),
                      retries: int = 5, delay: float = 2.0):
    """Run a SELECT with retry + reconnect on transient I/O errors. Returns
    (rows, conn) since a reconnect may replace the connection object."""
    last_err = None
    for attempt in range(retries):
        try:
            return conn.execute(sql, params).fetchall(), conn
        except sqlite3.OperationalError as e:
            last_err = e
            print(f"  [DB] query failed ({e}), retry {attempt+1}/{retries}...")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(delay)
            conn = connect_with_retry(db_path)
    raise last_err


# ── Odds parser ───────────────────────────────────────────────────────────────

def safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

def parse_odds(event_id: str, data: dict) -> list[dict]:
    rows = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bm_name, bm_data in (data.get("results") or {}).items():
        odds  = bm_data.get("odds") or {}
        start = odds.get("start") or {}
        end   = odds.get("end") or start
        def get_mk(d): return d.get("151_1") or d.get("1_1") or {}
        mk_s = get_mk(start)
        mk_e = get_mk(end) or mk_s
        oh = safe_float(mk_s.get("home_od") or mk_s.get("1"))
        oa = safe_float(mk_s.get("away_od") or mk_s.get("2"))
        ch = safe_float(mk_e.get("home_od") or mk_e.get("1")) or oh
        ca = safe_float(mk_e.get("away_od") or mk_e.get("2")) or oa
        if (oh or ch) and (oa or ca):
            rows.append({
                "event_id":   event_id,
                "bookmaker":  bm_name,
                "market":     "151_1",
                "open_home":  oh, "open_away":  oa,
                "close_home": ch, "close_away": ca,
                "fetched_at": now,
            })
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    conn = connect_with_retry(HARVEST_DB)
    conn.row_factory = sqlite3.Row
    api  = BetsAPI(BETSAPI_TOKEN)

    print("=" * 60)
    print("Multi-sport Odds Harvest")
    print(f"Sports: {', '.join(SPORTS)}")
    print("=" * 60)

    # Process sports in priority order
    ordered = [SPORT_PRIORITY] + [s for s in SPORTS if s != SPORT_PRIORITY]

    for sport in ordered:
        print(f"\n{'='*60}")
        print(f"SPORT: {sport.upper()}")

        # Get all ended events with winner from SQLite
        floor_ts  = int(datetime(START_FROM_YEAR, 1, 1).timestamp())
        recent_ts = int(time.time()) - MAX_AGE_DAYS * 86400
        since_ts  = max(floor_ts, recent_ts)   # whichever is more recent
        events, conn = query_with_retry(conn, HARVEST_DB, """
            SELECT event_id, league, home_team, away_team,
                   start_time, status, score, winner, raw_json, fetched_at
            FROM raw_events
            WHERE sport_tag = ? AND status = 'ended'
              AND winner IS NOT NULL AND winner != ''
              AND start_time >= ?
            ORDER BY start_time DESC
        """, (sport, since_ts))
        conn.row_factory = sqlite3.Row

        print(f"  Events with winner: {len(events):,}")
        if not events:
            continue

        event_ids = [e["event_id"] for e in events]

        # Find which already have odds in Supabase
        print(f"  Checking existing odds in Supabase...")
        existing = sb_get_existing_odds(event_ids)
        to_fetch = [e for e in events if e["event_id"] not in existing]
        print(f"  Already have odds: {len(existing):,}")
        print(f"  Need to fetch:     {len(to_fetch):,}")

        if not to_fetch:
            print("  All done for this sport!")
            continue

        # First: migrate events to betsapi_events
        print(f"  Migrating events to Supabase...")
        event_rows = [{
            "event_id":   e["event_id"],
            "league":     e["league"],
            "home_team":  e["home_team"],
            "away_team":  e["away_team"],
            "start_time": e["start_time"],
            "status":     e["status"],
            "score":      e["score"],
            "winner":     e["winner"],
            "raw_json":   e["raw_json"],
            "fetched_at": e["fetched_at"],
            "sport_tag":  sport,
        } for e in to_fetch]

        for i in range(0, len(event_rows), BATCH_SIZE):
            sb_upsert("betsapi_events", event_rows[i:i+BATCH_SIZE])
        print(f"  Events migrated: {len(event_rows):,}")

        # Fetch odds for each event
        print(f"  Fetching odds from BetsAPI...")
        odds_buffer = []
        success = 0
        skip = 0
        start_time = time.time()

        for i, event in enumerate(to_fetch):
            eid = event["event_id"]
            data = api.get("/v2/event/odds/summary", {"event_id": eid})
            if data:
                rows = parse_odds(eid, data)
                if rows:
                    odds_buffer.extend(rows)
                    success += 1
                else:
                    skip += 1
            else:
                skip += 1

            # Flush buffer
            if len(odds_buffer) >= BATCH_SIZE:
                sb_upsert("betsapi_odds", odds_buffer)
                odds_buffer = []

            # Progress
            if (i + 1) % 50 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed
                remaining = (len(to_fetch) - i - 1) / rate
                eta_h = remaining / 3600
                print(f"  [{i+1:>5}/{len(to_fetch):,}] "
                      f"ok={success} skip={skip} "
                      f"ETA: {eta_h:.1f}h "
                      f"API calls: {api.calls:,}", flush=True)

        # Flush remaining
        if odds_buffer:
            sb_upsert("betsapi_odds", odds_buffer)

        print(f"\n  {sport.upper()} done: {success:,} events with odds, {skip:,} skipped")

    conn.close()
    print(f"\n{'='*60}")
    print(f"All done! Total API calls: {api.calls:,}")
    print("=" * 60)


if __name__ == "__main__":
    main()
