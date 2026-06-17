#!/usr/bin/env python3
"""
Quick report on harvest progress.
Run at any time: python3 scripts/harvest_report.py
"""
import sqlite3, sys
from pathlib import Path

DB = Path(__file__).parent.parent / "storage" / "betsapi_harvest.db"

if not DB.exists():
    print("No harvest DB found yet. Run start_harvest.sh first.")
    sys.exit(0)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

def q(sql, *args):
    return conn.execute(sql, args).fetchone()[0]

print(f"\n{'='*55}")
print("BETSAPI HARVEST REPORT")
print(f"{'='*55}")

# API
api_calls = q("SELECT COUNT(*) FROM api_log")
api_errors = q("SELECT COUNT(*) FROM api_log WHERE error IS NOT NULL")
print(f"  API calls total:       {api_calls:>8,}")
print(f"  API errors:            {api_errors:>8,}")

# Events
print(f"\n  --- Events ---")
for tag in ("dota2", "cs2", "lol", "valorant", "esports"):
    ended   = q("SELECT COUNT(*) FROM raw_events WHERE sport_tag=? AND status='ended'",   tag)
    upcoming= q("SELECT COUNT(*) FROM raw_events WHERE sport_tag=? AND status='upcoming'",tag)
    if ended + upcoming > 0:
        print(f"  {tag.upper():<12} ended={ended:>7,}  upcoming={upcoming:>5,}")

# Odds summary
print(f"\n  --- Odds Summary ---")
total_rows = q("SELECT COUNT(*) FROM odds_summary")
unique_ev  = q("SELECT COUNT(DISTINCT event_id) FROM odds_summary")
unique_bm  = q("SELECT COUNT(DISTINCT bookmaker) FROM odds_summary")
print(f"  Total rows:            {total_rows:>8,}")
print(f"  Unique events:         {unique_ev:>8,}")
print(f"  Unique bookmakers:     {unique_bm:>8,}")

moved = q("""SELECT COUNT(*) FROM odds_summary
             WHERE open_home IS NOT NULL AND close_home IS NOT NULL
               AND ABS(open_home - close_home) > 0.001""")
print(f"  Lines that moved:      {moved:>8,}")

print(f"\n  Top bookmakers:")
bms = conn.execute(
    "SELECT bookmaker, COUNT(*) as c FROM odds_summary GROUP BY bookmaker ORDER BY c DESC LIMIT 15"
).fetchall()
for bm in bms:
    print(f"    {bm['bookmaker']:<28} {bm['c']:>7,}")

# Odds history
print(f"\n  --- Odds History (Movement Points) ---")
hist_pts = q("SELECT COUNT(*) FROM odds_history")
hist_ev  = q("SELECT COUNT(DISTINCT event_id) FROM odds_history")
print(f"  Total points:          {hist_pts:>8,}")
print(f"  Events covered:        {hist_ev:>8,}")

markets = [r[0] for r in conn.execute("SELECT DISTINCT market FROM odds_history").fetchall()]
print(f"  Markets:               {markets}")

# Live snapshots
live = q("SELECT COUNT(*) FROM live_snapshots") if q("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='live_snapshots'") else 0
live_ev = q("SELECT COUNT(DISTINCT event_id) FROM live_snapshots") if live else 0
print(f"\n  --- Live Snapshots ---")
print(f"  Total snapshots:       {live:>8,}")
print(f"  Unique events:         {live_ev:>8,}")

# Harvest progress
done_s = q("SELECT COUNT(*) FROM harvest_progress WHERE summary_done=1")
done_h = q("SELECT COUNT(*) FROM harvest_progress WHERE history_done=1")
print(f"\n  --- Progress ---")
print(f"  Events w/ summary:     {done_s:>8,}")
print(f"  Events w/ history:     {done_h:>8,}")

# DB size
size_mb = DB.stat().st_size / 1024 / 1024
print(f"\n  DB size:               {size_mb:>7.1f} MB")

# Meta
meta = {r[0]: r[1] for r in conn.execute("SELECT key,value FROM harvest_meta").fetchall()}
if meta.get("started_at"):
    print(f"  Started at:            {meta['started_at']}")
if meta.get("finished_at"):
    print(f"  Finished at:           {meta['finished_at']}")

print(f"{'='*55}\n")
conn.close()
