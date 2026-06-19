#!/usr/bin/env python3
"""
Supabase Harvest — пишет прямо в Supabase, без локального SQLite.
Для оставшихся дней BetsAPI и GitHub Actions.

Фазы:
  1. Upcoming events + their odds (предстоящие матчи)
  2. Ended events day-by-day from last fetched date (исторические)
  3. Odds summary for ended events without odds
  4. Live snapshots (текущие котировки)

Run:
    cd ~/Downloads/dota_trader_v2
    python3 scripts/supabase_harvest.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_ANON_KEY", "")

if not all([BETSAPI_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Missing env vars. Check .env")
    sys.exit(1)

BETS_BASE   = "https://api.b365api.com"
SPORT_ID    = 151       # esports
DOTA_LEAGUE_KEYWORDS = ["dota", "dota 2", "dota2"]

REQ_INTERVAL = 1.1      # seconds between BetsAPI calls
START_DATE   = date(2022, 1, 1)

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}


# ── BetsAPI client ────────────────────────────────────────────────────────────

class BetsAPI:
    def __init__(self, token: str):
        self.token   = token
        self._last   = 0.0
        self._total  = 0
        self.session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.time() - self._last
        if elapsed < REQ_INTERVAL:
            time.sleep(REQ_INTERVAL - elapsed)

        p = {"token": self.token, **(params or {})}
        for attempt in range(3):
            try:
                r = self.session.get(f"{BETS_BASE}{path}", params=p, timeout=20)
                self._last  = time.time()
                self._total += 1

                if r.status_code == 429:
                    print(f"  [429] rate limit — sleep 5m", flush=True)
                    time.sleep(300)
                    continue

                r.raise_for_status()
                data = r.json()
                if not data.get("success"):
                    raise RuntimeError(f"API error: {data.get('error', data)}")
                return data

            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  [WARN] {path} attempt {attempt+1}: {e}", flush=True)
                time.sleep(5)

        raise RuntimeError("Max retries exceeded")


# ── Supabase helpers ──────────────────────────────────────────────────────────

CONFLICT_COLS = {
    "betsapi_events":  "event_id",
    "betsapi_odds":    "event_id,bookmaker,market",
    "raw_events":      "event_id",
    "upcoming_events": "event_id",
    "odds_summary":    "event_id,bookmaker,market",
}

def sb_upsert(table: str, rows: list[dict], batch: int = 500) -> int:
    inserted = 0
    conflict = CONFLICT_COLS.get(table, "")
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if conflict:
        url += f"?on_conflict={conflict}"
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        r = requests.post(url, headers=SB_HEADERS, json=chunk, timeout=30)
        if r.status_code not in (200, 201):
            print(f"  [SB ERROR] {table}: {r.status_code} {r.text[:200]}")
        else:
            inserted += len(chunk)
        time.sleep(0.1)
    return inserted


def sb_get(table: str, params: str) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{params}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def is_dota2(event: dict) -> bool:
    league = (event.get("league") or {}).get("name", "").lower()
    return any(k in league for k in DOTA_LEAGUE_KEYWORDS)


# ── Parse helpers ─────────────────────────────────────────────────────────────

def parse_event(e: dict, status: str) -> dict:
    scores = e.get("scores") or {}
    score_str = json.dumps(scores) if scores else None
    winner = None
    if scores:
        home_s = scores.get("2", {}).get("home", 0)
        away_s = scores.get("2", {}).get("away", 0)
        try:
            if int(home_s) > int(away_s):
                winner = "1"
            elif int(away_s) > int(home_s):
                winner = "2"
        except (TypeError, ValueError):
            pass

    return {
        "event_id":   str(e.get("id", "")),
        "sport_id":   SPORT_ID,
        "sport_tag":  "dota2",
        "league":     (e.get("league") or {}).get("name", ""),
        "home_team":  (e.get("home") or {}).get("name", ""),
        "away_team":  (e.get("away") or {}).get("name", ""),
        "start_time": e.get("time"),
        "status":     status,
        "score":      score_str,
        "winner":     winner,
        "raw_json":   json.dumps(e, ensure_ascii=False),
        "fetched_at": now_iso(),
    }


def parse_odds_summary(event_id: str, data: dict) -> list[dict]:
    rows = []
    results = data.get("results", {})
    for bm_name, bm_data in results.items():
        odds  = bm_data.get("odds", {})
        start = odds.get("start", {}) or {}
        end   = odds.get("end", {}) or start

        mk_s = start.get("151_1", {}) or start.get("1_1", {}) or {}
        mk_e = end.get("151_1", {})   or end.get("1_1", {}) or mk_s

        oh = _safe_float(mk_s.get("home_od") or mk_s.get("1"))
        oa = _safe_float(mk_s.get("away_od") or mk_s.get("2"))
        ch = _safe_float(mk_e.get("home_od") or mk_e.get("1"))
        ca = _safe_float(mk_e.get("away_od") or mk_e.get("2"))

        if oh or oa or ch or ca:
            rows.append({
                "event_id":   event_id,
                "bookmaker":  bm_name,
                "market":     "151_1",
                "open_home":  oh,
                "open_away":  oa,
                "close_home": ch or oh,
                "close_away": ca or oa,
                "raw_json":   json.dumps(data, ensure_ascii=False)[:2000],
                "fetched_at": now_iso(),
            })
    return rows


# ── Phase 1: Upcoming ─────────────────────────────────────────────────────────

def phase1_upcoming(api: BetsAPI) -> None:
    print("\n[Phase 1] Upcoming Dota2 events + odds")
    events = []
    page = 1
    while True:
        data  = api.get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})
        items = data.get("results", [])
        if not items:
            break
        total = data.get("pager", {}).get("total", 0)
        for e in items:
            if is_dota2(e):
                events.append(e)
        if page * 50 >= total:
            break
        page += 1

    print(f"  Found {len(events)} upcoming Dota2 events")
    if not events:
        return

    # Save to upcoming_events
    upcoming_rows = [{
        "event_id":   str(e.get("id", "")),
        "sport_tag":  "dota2",
        "league":     (e.get("league") or {}).get("name", ""),
        "home_team":  (e.get("home") or {}).get("name", ""),
        "away_team":  (e.get("away") or {}).get("name", ""),
        "start_time": e.get("time"),
        "raw_json":   json.dumps(e, ensure_ascii=False),
        "fetched_at": now_iso(),
    } for e in events]
    sb_upsert("upcoming_events", upcoming_rows)

    # Also save to raw_events
    event_rows = [parse_event(e, "upcoming") for e in events]
    sb_upsert("raw_events", event_rows)

    # Fetch odds for each upcoming event
    odds_rows = []
    for i, e in enumerate(events):
        eid  = str(e.get("id", ""))
        home = (e.get("home") or {}).get("name", "?")
        away = (e.get("away") or {}).get("name", "?")
        try:
            data = api.get("/v2/event/odds/summary", {"event_id": eid})
            rows = parse_odds_summary(eid, data)
            odds_rows.extend(rows)
            print(f"  [{i+1}/{len(events)}] {home} vs {away}: {len(rows)} bookmakers", flush=True)
        except Exception as ex:
            print(f"  [WARN] {home} vs {away}: {ex}", flush=True)

    if odds_rows:
        sb_upsert("odds_summary", odds_rows)
        print(f"  Saved {len(odds_rows)} odds rows")


# ── Phase 2: Ended events (day-based) ────────────────────────────────────────

def phase2_ended(api: BetsAPI) -> None:
    print("\n[Phase 2] Ended Dota2 events (day-based from last checkpoint)")

    # Find last fetched date from Supabase
    try:
        rows = sb_get("raw_events",
                      "sport_tag=eq.dota2&status=eq.ended"
                      "&order=start_time.desc&limit=1&select=start_time")
        if rows and rows[0].get("start_time"):
            last_ts   = int(rows[0]["start_time"])
            last_date = datetime.fromtimestamp(last_ts).date()
            start     = last_date - timedelta(days=3)  # overlap buffer
        else:
            start = START_DATE
    except Exception:
        start = START_DATE

    today = date.today()
    print(f"  Fetching from {start} to {today}")

    current = start
    total_saved = 0

    while current <= today:
        day_str = current.strftime("%Y%m%d")
        page    = 1
        day_events = []

        while True:
            try:
                data  = api.get("/v3/events/ended",
                                {"sport_id": SPORT_ID, "day": day_str, "page": page})
                items = data.get("results", [])
                if not items:
                    break
                total = data.get("pager", {}).get("total", 0)

                for e in items:
                    if is_dota2(e):
                        day_events.append(e)

                if page * 50 >= total or page >= 100:
                    break
                page += 1

            except Exception as ex:
                print(f"  [WARN] {day_str} page {page}: {ex}", flush=True)
                break

        if day_events:
            rows = [parse_event(e, "ended") for e in day_events]
            sb_upsert("raw_events", rows)
            total_saved += len(day_events)
            print(f"  {current}: {len(day_events)} events (total saved: {total_saved})", flush=True)

        current += timedelta(days=1)

    print(f"  Phase 2 done: {total_saved} ended events saved")


# ── Phase 3: Odds summary for events missing odds ─────────────────────────────

def phase3_odds_summary(api: BetsAPI, max_events: int = 5000) -> None:
    print("\n[Phase 3] Odds summary for events without odds")

    # Events in raw_events but not in odds_summary
    try:
        all_events = sb_get("raw_events",
                            "sport_tag=eq.dota2&status=eq.ended"
                            "&select=event_id&order=start_time.desc"
                            f"&limit={max_events}")
        have_odds_rows = sb_get("odds_summary",
                                "select=event_id&limit=50000")
        have_odds = {r["event_id"] for r in have_odds_rows}
        need_odds = [r["event_id"] for r in all_events if r["event_id"] not in have_odds]
    except Exception as ex:
        print(f"  [ERROR] checking existing odds: {ex}")
        return

    print(f"  Events needing odds: {len(need_odds)}")

    for i, eid in enumerate(need_odds):
        try:
            data = api.get("/v2/event/odds/summary", {"event_id": eid})
            rows = parse_odds_summary(eid, data)
            if rows:
                sb_upsert("odds_summary", rows)
            if (i + 1) % 100 == 0:
                print(f"  Progress: {i+1}/{len(need_odds)}", flush=True)
        except Exception as ex:
            print(f"  [WARN] {eid}: {ex}", flush=True)

    print(f"  Phase 3 done")


# ── Phase 4: Live snapshots ───────────────────────────────────────────────────

def phase4_live_snapshot(api: BetsAPI) -> None:
    print("\n[Phase 4] Live snapshot of upcoming odds")

    try:
        upcoming = sb_get("upcoming_events",
                          f"start_time=gt.{int(time.time())}"
                          "&order=start_time.asc&limit=50")
    except Exception as ex:
        print(f"  [ERROR] {ex}")
        return

    print(f"  {len(upcoming)} upcoming events")
    snap_rows = []
    cap_at = now_iso()
    now_ts = int(time.time())

    for e in upcoming:
        eid = e["event_id"]
        try:
            data = api.get("/v2/event/odds/summary", {"event_id": eid})
            bms  = parse_odds_summary(eid, data)
            for bm in bms:
                secs = (int(e["start_time"]) - now_ts) if e.get("start_time") else None
                snap_rows.append({
                    "captured_at":      cap_at,
                    "event_id":         eid,
                    "league":           e.get("league", ""),
                    "home_team":        e.get("home_team", ""),
                    "away_team":        e.get("away_team", ""),
                    "start_time":       e.get("start_time"),
                    "seconds_to_start": secs,
                    "bookmaker":        bm["bookmaker"],
                    "market":           "151_1",
                    "home_odds":        bm["close_home"],
                    "away_odds":        bm["close_away"],
                    "open_home":        bm["open_home"],
                    "open_away":        bm["open_away"],
                    "raw_json":         bm["raw_json"],
                })
        except Exception as ex:
            print(f"  [WARN] {eid}: {ex}", flush=True)

    if snap_rows:
        sb_upsert("live_snapshots", snap_rows)
        print(f"  Saved {len(snap_rows)} live snapshot rows")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    phase = os.getenv("HARVEST_PHASE", "all").lower()

    print("=" * 60)
    print(f"Supabase Harvest — phase={phase}")
    print(f"Started: {now_iso()}")
    print("=" * 60)

    api = BetsAPI(BETSAPI_TOKEN)

    if phase in ("all", "upcoming"):
        phase1_upcoming(api)

    if phase in ("all", "ended"):
        phase2_ended(api)
        phase3_odds_summary(api)

    if phase in ("all", "live"):
        phase4_live_snapshot(api)

    print("\n" + "=" * 60)
    print(f"Done. API calls: {api._total} | phase: {phase}")
    print(f"Finished: {now_iso()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
