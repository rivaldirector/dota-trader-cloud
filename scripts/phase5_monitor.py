#!/usr/bin/env python3
"""
Phase 5 Monitor — смотрит прогресс всех шардов в реальном времени.
Запускай в отдельном терминале. Обновляется каждые 30 секунд.

Usage:
    python3 scripts/phase5_monitor.py
    python3 scripts/phase5_monitor.py --once   # одноразово
"""
import sqlite3, time, sys, os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent

SHARDS = [
    ("Dota ALL",  ROOT / "storage" / "phase5_dota_all.db"),
    ("CS2",       ROOT / "storage" / "phase5_cs2.db"),
    ("LoL/Val",   ROOT / "storage" / "phase5_lol_val.db"),
]
LIVE_DB = ROOT / "storage" / "live_tracking.db"
MAIN_DB = ROOT / "storage" / "betsapi_harvest.db"

def query(db_path, sql, params=()):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        r = conn.execute(sql, params).fetchall()
        conn.close()
        return r
    except Exception:
        return []

def scalar(db_path, sql, params=(), default=0):
    r = query(db_path, sql, params)
    return r[0][0] if r and r[0][0] is not None else default

def print_report():
    os.system("clear")
    now = datetime.now().strftime("%H:%M:%S")
    print(f"{'='*70}")
    print(f"  Phase 5 Monitor — {now}  (Ctrl+C to stop)")
    print(f"{'='*70}")

    total_rows = 0
    total_events = 0

    for name, path in SHARDS:
        if not path.exists():
            print(f"\n  [{name}] — не запущен")
            continue

        rows       = scalar(path, "SELECT COUNT(*) FROM odds_history")
        events     = scalar(path, "SELECT COUNT(DISTINCT event_id) FROM odds_history")
        prematch   = scalar(path, "SELECT COUNT(*) FROM odds_history WHERE ss IS NULL")
        live       = scalar(path, "SELECT COUNT(*) FROM odds_history WHERE ss IS NOT NULL")
        done       = scalar(path, "SELECT COUNT(*) FROM worker_progress WHERE done=1")
        empty      = scalar(path, "SELECT COUNT(*) FROM worker_progress WHERE done=-2")
        errors     = scalar(path, "SELECT COUNT(*) FROM worker_progress WHERE done=-1")

        # Last event updated
        last = query(path, """
            SELECT wp.event_id, wp.done_at FROM worker_progress wp
            ORDER BY wp.done_at DESC LIMIT 1
        """)
        last_str = last[0]["done_at"][-8:] if last else "—"

        total_rows   += rows
        total_events += events

        print(f"\n  [{name}]  {path.name}")
        print(f"    rows:  {rows:>8,}  |  events: {events:>5}  |  pre-match: {prematch:>6}  live: {live:>6}")
        print(f"    done:  {done:>5}  empty: {empty:>5}  errors: {errors:>4}  |  last: {last_str}")

        # Market breakdown
        mkts = query(path, "SELECT market, COUNT(*) as c FROM odds_history GROUP BY market")
        mkt_str = "  ".join(f"{m['market']}:{m['c']:,}" for m in mkts)
        if mkt_str:
            print(f"    markets: {mkt_str}")

    print(f"\n{'─'*70}")
    print(f"  TOTAL  rows: {total_rows:,}  |  events: {total_events:,}")

    # Main DB status
    main_done = scalar(MAIN_DB, "SELECT COUNT(*) FROM harvest_progress WHERE history_done=1")
    main_total = scalar(MAIN_DB, "SELECT COUNT(*) FROM harvest_progress WHERE summary_done=1")
    print(f"  Main DB Phase 5 progress: {main_done}/{main_total} ({main_done/max(main_total,1)*100:.1f}%)")

    # Live tracking
    if LIVE_DB.exists():
        live_snaps = scalar(LIVE_DB, "SELECT COUNT(*) FROM live_snapshots")
        polls = scalar(LIVE_DB, "SELECT COUNT(*) FROM poll_log")
        last_poll = query(LIVE_DB, "SELECT polled_at FROM poll_log ORDER BY id DESC LIMIT 1")
        lp = last_poll[0][0][-8:] if last_poll else "—"
        print(f"  Live Tracking: {live_snaps:,} snapshots | {polls} polls | last: {lp}")

    print(f"{'='*70}\n")

once = "--once" in sys.argv

if once:
    print_report()
else:
    try:
        while True:
            print_report()
            time.sleep(30)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
