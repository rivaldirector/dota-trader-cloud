#!/usr/bin/env python3
"""
Market data readiness report.
Показывает состояние накопленных odds и рыночных данных.

Запуск:
    python3 scripts/market_report.py
"""

import sys
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

DB_PATH = PROJECT_ROOT / settings.database_path


def _now():
    return datetime.now(timezone.utc)


def market_report(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("\n" + "=" * 60)
    print("  MARKET DATA REPORT")
    print(f"  {_now():%Y-%m-%d %H:%M:%S UTC}")
    print("=" * 60)

    # ── Общая статистика ──────────────────────────────────────────────────────
    total_snaps = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    print(f"\n[1] SNAPSHOTS TOTAL: {total_snaps}")

    if total_snaps == 0:
        print("\n  ⚠  Snapshots table is empty.")
        print("  Run:  python3 scripts/collect_odds.py --probe")
        print("  Then: python3 scripts/collect_odds.py")
        _print_projections(0)
        conn.close()
        return

    # ── По источникам ─────────────────────────────────────────────────────────
    print("\n[2] BY SOURCE / BOOKMAKER:")
    rows = conn.execute(
        "SELECT source, bookmaker, COUNT(*) as n "
        "FROM odds_snapshots GROUP BY source, bookmaker ORDER BY n DESC"
    ).fetchall()
    bm_set = set()
    for r in rows:
        print(f"   {r['source']:15} / {r['bookmaker']:20} : {r['n']:6} snapshots")
        bm_set.add(r["bookmaker"])
    print(f"   Unique bookmakers: {len(bm_set)}")

    # ── По матчам ─────────────────────────────────────────────────────────────
    print("\n[3] UNIQUE MATCHES WITH ODDS:")
    match_counts = conn.execute(
        "SELECT match_name, COUNT(*) as snaps, "
        "MIN(captured_at) as first_seen, MAX(captured_at) as last_seen "
        "FROM odds_snapshots GROUP BY match_name ORDER BY first_seen DESC"
    ).fetchall()
    n_matches = len(match_counts)
    print(f"   Total: {n_matches} matches")
    for r in match_counts[:10]:
        span = "?"
        try:
            t1 = datetime.fromisoformat(r["first_seen"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_seen"].replace("Z", "+00:00"))
            span = f"{int((t2-t1).total_seconds()//60)}min tracked"
        except Exception:
            pass
        print(f"   {r['match_name'][:40]:40} | {r['snaps']:3} snaps | {span}")

    # ── Временной охват ───────────────────────────────────────────────────────
    print("\n[4] TIME RANGE:")
    trange = conn.execute(
        "SELECT MIN(captured_at) as first, MAX(captured_at) as last FROM odds_snapshots"
    ).fetchone()
    print(f"   First snapshot: {trange['first']}")
    print(f"   Last snapshot:  {trange['last']}")
    try:
        t1 = datetime.fromisoformat(trange["first"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(trange["last"].replace("Z", "+00:00"))
        days = (t2 - t1).days
        rate = total_snaps / max(days, 1)
        print(f"   Days collecting: {days}")
        print(f"   Rate: ~{rate:.0f} snapshots/day")
        _print_projections(rate)
    except Exception:
        _print_projections(0)

    # ── Opening / Closing availability ────────────────────────────────────────
    print("\n[5] OPENING / CLOSING ODDS AVAILABILITY:")
    per_match = defaultdict(list)
    for r in match_counts:
        name = r["match_name"]
        snaps = conn.execute(
            "SELECT captured_at, team_1_odds, team_2_odds, match_start_at "
            "FROM odds_snapshots WHERE match_name=? ORDER BY captured_at",
            (name,)
        ).fetchall()
        per_match[name] = [dict(s) for s in snaps]

    has_opening  = sum(1 for v in per_match.values() if len(v) >= 1)
    has_closing  = 0  # closing = last snap before match start
    has_movement = sum(1 for v in per_match.values() if len(v) >= 3)

    for name, snaps in per_match.items():
        if not snaps:
            continue
        start_at = snaps[0].get("match_start_at")
        if start_at:
            try:
                start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                # Find last snap before start
                pre_start = [s for s in snaps
                             if datetime.fromisoformat(s["captured_at"].replace("Z","+00:00")) < start_dt]
                if pre_start:
                    has_closing += 1
            except Exception:
                pass

    print(f"   Matches with opening line:       {has_opening}/{n_matches}")
    print(f"   Matches with closing line:        {has_closing}/{n_matches} (last snap before start)")
    print(f"   Matches with line movement (≥3 snaps): {has_movement}/{n_matches}")

    # ── Overround stats ───────────────────────────────────────────────────────
    print("\n[6] MARKET QUALITY (overround):")
    ors = conn.execute(
        "SELECT overround FROM odds_snapshots WHERE overround IS NOT NULL AND overround > 0"
    ).fetchall()
    if ors:
        vals = [r["overround"] for r in ors]
        avg_or = sum(vals) / len(vals)
        print(f"   Avg overround: {avg_or:.4f} ({(avg_or-1)*100:.2f}% vig)")
        print(f"   Min overround: {min(vals):.4f}")
        print(f"   Max overround: {max(vals):.4f}")
        # Pinnacle typically ~1.02, sharp books ~1.03-1.05, soft books ~1.10+
        if avg_or < 1.04:
            print(f"   Quality: SHARP (Pinnacle-tier)")
        elif avg_or < 1.07:
            print(f"   Quality: MEDIUM")
        else:
            print(f"   Quality: SOFT (high vig)")

    # ── Market comparison readiness ───────────────────────────────────────────
    print("\n[7] MARKET COMPARISON READINESS:")
    # Match odds_snapshots to finished matches
    finished_with_odds = conn.execute(
        """SELECT COUNT(DISTINCT o.match_external_id) FROM odds_snapshots o
           JOIN matches m ON m.external_id = o.match_external_id
           WHERE m.status = 'finished'"""
    ).fetchone()[0]
    print(f"   Finished matches with odds in DB: {finished_with_odds}")
    if finished_with_odds >= 30:
        print("   ✓ Ready for first market comparison analysis")
    elif finished_with_odds >= 10:
        print("   ~ Partial: accumulate more data")
    else:
        print("   ✗ Not yet: keep collecting")

    # Sample of the market comparison table
    if finished_with_odds > 0:
        print("\n[8] SAMPLE: model_prob vs market_prob (first 5 matches):")
        sample = conn.execute(
            """SELECT o.match_name, o.team_1_odds, o.team_1_implied_prob,
                      m.winner_name, m.team_1_name
               FROM odds_snapshots o
               JOIN matches m ON m.external_id = o.match_external_id
               WHERE m.status = 'finished' AND o.team_1_implied_prob IS NOT NULL
               GROUP BY o.match_name
               LIMIT 5"""
        ).fetchall()
        print(f"   {'match':35} | mkt_imp1 | actual")
        for r in sample:
            actual = "WIN" if r["winner_name"] == r["team_1_name"] else "LOSS"
            print(f"   {r['match_name'][:35]:35} | {r['team_1_implied_prob']:.1%}   | {actual}")

    conn.close()


def _print_projections(rate_per_day: float):
    print("\n[PROJECTIONS]")
    if rate_per_day == 0:
        # Estimate from typical The Odds API: ~10 events/day, 3 bookmakers = 30 rows/collection
        # 48 collections/day (every 30 min) = 1440 rows/day but most events short-lived
        # Realistic: ~150-300 unique snapshots/day
        rate_per_day = 200
        print(f"   (Estimate based on typical odds API volume: ~{rate_per_day} rows/day)")

    for days, label in [(30, "30 days"), (90, "90 days"), (180, "180 days")]:
        snaps = int(rate_per_day * days)
        # ~10 matches/day, ~3 bookmakers, ~5 snapshots/match avg
        est_matches = int(days * 10)  # ~10 Dota 2 matches/day globally
        size_kb = snaps * 0.5  # ~500 bytes per row with raw_json
        print(f"   {label}: ~{snaps:,} snapshots | ~{est_matches} matches | ~{size_kb/1024:.1f} MB")


if __name__ == "__main__":
    market_report(DB_PATH)
