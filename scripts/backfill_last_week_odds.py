#!/usr/bin/env python3
"""
Targeted backfill: fetch odds for events from the last 7 days that are
already in betsapi_events (ended, with winner) but missing from
betsapi_odds. Unlike harvest_odds_multisport.py (which only covers
cs2/lol/valorant and walks the whole historical window), this covers
ALL disciplines (dota2, cs2, lol, valorant, r6) but only the last 7
days — needed to get an honest signal count for "last week".

Run in background:
    nohup python3 scripts/backfill_last_week_odds.py > /tmp/backfill.log 2>&1 &
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, os.path.dirname(__file__))
from daily_pipeline_lib import check_betsapi_alive, sb_set_pipeline_status

BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_ANON_KEY", "")
REQ_INTERVAL  = 2.0
BATCH_LIMIT   = int(os.getenv("BATCH_LIMIT", "0")) or None  # cap events fetched this run (0 = no cap)

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}


def sb_get(table, qs):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
                      headers={**SB_HEADERS, "Prefer": "return=representation"}, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows):
    if not rows:
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, json=rows, timeout=30)
    if r.status_code not in (200, 201):
        print(f"  [SB ERROR] {table}: {r.status_code} {r.text[:200]}", flush=True)


def safe_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_odds(event_id, data):
    rows = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bm_name, bm_data in (data.get("results") or {}).items():
        odds = bm_data.get("odds") or {}
        start = odds.get("start") or {}
        end = odds.get("end") or start
        def get_mk(d): return d.get("151_1") or d.get("1_1") or {}
        mk_s, mk_e = get_mk(start), get_mk(end)
        if not mk_e:
            mk_e = mk_s
        oh = safe_float(mk_s.get("home_od") or mk_s.get("1"))
        oa = safe_float(mk_s.get("away_od") or mk_s.get("2"))
        ch = safe_float(mk_e.get("home_od") or mk_e.get("1")) or oh
        ca = safe_float(mk_e.get("away_od") or mk_e.get("2")) or oa
        if (oh or ch) and (oa or ca):
            rows.append({"event_id": event_id, "bookmaker": bm_name, "market": "151_1",
                         "open_home": oh, "open_away": oa, "close_home": ch, "close_away": ca,
                         "fetched_at": now})
    return rows


def main():
    if not all([BETSAPI_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing env vars", flush=True); sys.exit(1)

    alive, why = check_betsapi_alive(BETSAPI_TOKEN)
    if not alive:
        print(f"ERROR: BetsAPI недоступен ({why}) — backfill пропущен.", flush=True)
        sb_set_pipeline_status("backfill_last_week_odds", False, f"BetsAPI недоступен: {why}")
        sys.exit(1)

    print(f"[{datetime.now()}] Fetching last-7-days ended events...", flush=True)
    events = sb_get("betsapi_events",
                     "status=eq.ended&winner=neq.&"
                     f"start_time=gte.{int(time.time())-7*86400}&start_time=lt.{int(time.time())}&"
                     "select=event_id,sport_tag,start_time&limit=5000")
    print(f"  total ended events in window: {len(events)}", flush=True)

    event_ids = [e["event_id"] for e in events]
    existing = set()
    for i in range(0, len(event_ids), 100):
        chunk = event_ids[i:i+100]
        ids_str = ",".join(f'"{e}"' for e in chunk)
        rows = sb_get("betsapi_odds", f"select=event_id&event_id=in.({ids_str})&limit=1000")
        existing.update(r["event_id"] for r in rows)

    to_fetch = [e for e in events if e["event_id"] not in existing]
    print(f"  already have odds: {len(existing)}  |  need to fetch: {len(to_fetch)}", flush=True)
    if BATCH_LIMIT:
        to_fetch = to_fetch[:BATCH_LIMIT]
        print(f"  (this run capped at {BATCH_LIMIT})", flush=True)

    session = requests.Session()
    last = 0.0
    ok, fail = 0, 0
    buf = []
    for i, e in enumerate(to_fetch):
        eid = e["event_id"]
        elapsed = time.time() - last
        if elapsed < REQ_INTERVAL:
            time.sleep(REQ_INTERVAL - elapsed)
        try:
            r = session.get("https://api.b365api.com/v2/event/odds/summary",
                             params={"token": BETSAPI_TOKEN, "event_id": eid}, timeout=20)
            last = time.time()
            if r.status_code in (401, 403):
                print(f"  [AUTH] HTTP {r.status_code} on {eid} — токен умер посередине прогона.", flush=True)
                sb_set_pipeline_status("backfill_last_week_odds", False,
                                        f"токен умер на {i+1}/{len(to_fetch)} (ok={ok} fail={fail})")
                if buf:
                    sb_upsert("betsapi_odds", buf)
                sys.exit(1)
            if r.status_code == 429:
                print("  [429] sleeping 60s", flush=True)
                time.sleep(60)
                continue
            data = r.json()
            if data.get("success"):
                rows = parse_odds(eid, data)
                if rows:
                    buf.extend(rows)
                    ok += 1
                else:
                    fail += 1
            else:
                fail += 1
        except Exception as ex:
            print(f"  [WARN] {eid}: {ex}", flush=True)
            fail += 1

        # Defensive abort: if the canary passed but the token dies mid-run
        # (e.g. expires while the job is running), don't silently burn the
        # rest of the timeout failing on every single event.
        if i + 1 == 10 and ok == 0 and fail >= 10:
            print("  [ABORT] первые 10 запросов все провалились — похоже, API недоступен.", flush=True)
            sb_set_pipeline_status("backfill_last_week_odds", False,
                                    "первые 10 запросов провалились — вероятен сбой API")
            sys.exit(1)

        if len(buf) >= 200:
            sb_upsert("betsapi_odds", buf)
            buf = []

        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(to_fetch)}] ok={ok} fail={fail}", flush=True)

    if buf:
        sb_upsert("betsapi_odds", buf)

    print(f"[{datetime.now()}] DONE. ok={ok} fail={fail}", flush=True)
    sb_set_pipeline_status("backfill_last_week_odds", True, f"ok={ok} fail={fail} (всего к фетчу: {len(to_fetch)})")


if __name__ == "__main__":
    main()
