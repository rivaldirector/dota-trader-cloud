#!/usr/bin/env python3
"""
Загрузка исторических Dota 2 odds по league_id — без глобального лимита страниц.

Вместо /v3/events/ended?sport_id=151 (лимит ~100 страниц)
используем /v3/events/ended?league_id=XXXXX для каждой лиги отдельно.
У каждой лиги независимая пагинация → можем уйти на 500+ страниц суммарно.

Запуск:
    PYTHONPATH=. python3 scripts/fetch_dota2_by_league.py
    PYTHONPATH=. python3 scripts/fetch_dota2_by_league.py --pages 200
    PYTHONPATH=. python3 scripts/fetch_dota2_by_league.py --dry-run
"""
from __future__ import annotations

import sys, json, time, argparse, sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from adapters.betsapi import (
    BetsAPIClient, _match_to_db,
    _extract_moneyline, _insert_snapshot,
)

DB_PATH = PROJECT_ROOT / settings.database_path

# Все известные Dota 2 league_id (из raw_json наших снапшотов)
# Добавляй новые если BetsAPI вернёт новые лиги
DOTA2_LEAGUES = {
    "13922":  "DOTA2",
    "37551":  "DOTA2 - 1win Series",
    "39038":  "DOTA2 - BLAST Slam",
    "42066":  "DOTA2 - CCT Series SA",
    "42755":  "DOTA2 - CCT South America Series 2",
    "20453":  "DOTA2 - DreamLeague",
    "41900":  "DOTA2 - DreamLeague Div 2",
    "38383":  "DOTA2 - DreamLeague Quals - CN",
    "38382":  "DOTA2 - DreamLeague Quals - EEU",
    "38403":  "DOTA2 - DreamLeague Quals - NA",
    "38404":  "DOTA2 - DreamLeague Quals - SA",
    "38384":  "DOTA2 - DreamLeague Quals - SEA",
    "38386":  "DOTA2 - DreamLeague Quals - WEU",
    "39712":  "DOTA2 - EPL World Series SEA",
    "42833":  "DOTA2 - ESL Challenger China",
    "40629":  "DOTA2 - EWC Quals EU East",
    "40631":  "DOTA2 - EWC Quals MESWA",
    "40657":  "DOTA2 - EWC Quals SA",
    "40764":  "DOTA2 - Esports World Cup",
    "35796":  "DOTA2 - European Pro League",
    "37005":  "DOTA2 - PGL Wallachia",
}


def _already_collected(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT match_external_id FROM odds_snapshots "
        "WHERE source='betsapi' AND match_external_id IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def _add_league_column_if_missing(conn: sqlite3.Connection):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_snapshots)").fetchall()}
    if "league_name" not in cols:
        conn.execute("ALTER TABLE odds_snapshots ADD COLUMN league_name TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_odds_league ON odds_snapshots(league_name)")
        conn.commit()


def fetch_league(client: BetsAPIClient, conn: sqlite3.Connection,
                 league_id: str, league_name: str,
                 already: set, max_pages: int, dry_run: bool) -> dict:
    """Скачать все завершённые события для одной лиги."""
    inserted = 0
    skipped  = 0
    no_odds  = 0
    page     = 1

    while page <= max_pages:
        try:
            data = client._get("/v3/events/ended", {
                "sport_id": "151",
                "league_id": league_id,
                "page": page,
            })
        except Exception as e:
            errmsg = str(e)
            if "PARAM_INVALID" in errmsg or "invalid" in errmsg.lower():
                # Достигли конца истории лиги
                break
            print(f"    [{league_name}] page {page} ERROR: {e} — пропускаем")
            page += 1
            continue

        items = data.get("results", [])
        total = data.get("pager", {}).get("total", 0)

        if not items:
            break

        for event in items:
            eid  = str(event.get("id", ""))
            home = event.get("home", {}).get("name", "")
            away = event.get("away", {}).get("name", "")

            if eid in already:
                skipped += 1
                continue

            if dry_run:
                inserted += 1
                continue

            try:
                summary = client.get_odds_summary(eid)
                bms = _extract_moneyline(summary)
            except Exception:
                no_odds += 1
                continue

            if not bms:
                no_odds += 1
                continue

            # Матчинг с PandaScore
            ext_id, match_name = _match_to_db(
                conn, home, away, str(event.get("time", ""))
            )

            event_ts = event.get("time", "")
            try:
                captured_at = datetime.fromtimestamp(
                    int(event_ts), tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            for bm in bms:
                _insert_snapshot(
                    conn, captured_at + "_open", event, bm,
                    bm["open_home"], bm["open_away"],
                    ext_id, match_name,
                    {"type": "open", "event": event, "bm": bm},
                    league_name=league_name,
                )
                if bm["close_home"] != bm["open_home"]:
                    _insert_snapshot(
                        conn, captured_at + "_close", event, bm,
                        bm["close_home"], bm["close_away"],
                        ext_id, match_name,
                        {"type": "close", "event": event, "bm": bm},
                        league_name=league_name,
                    )
                inserted += 1

            already.add(eid)

        conn.commit()

        pct = min(page / max(total // 50, 1) * 100, 100)
        print(f"    [{league_name}] page {page:3}  "
              f"total_events={total}  inserted={inserted}  skip={skipped}  "
              f"no_odds={no_odds}  [{pct:.0f}%]", flush=True)

        time.sleep(1.5)  # rate limit

        if page * 50 >= total:
            break
        page += 1

    return {"inserted": inserted, "skipped": skipped, "no_odds": no_odds, "pages": page}


def run(max_pages_per_league: int = 500, dry_run: bool = False,
        league_filter: str | None = None):
    client = BetsAPIClient()
    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _add_league_column_if_missing(conn)

    already = _already_collected(conn)
    print(f"\n{'='*65}")
    print(f"  Dota 2 by-league backfill")
    print(f"  Лиг: {len(DOTA2_LEAGUES)}  max_pages/лига: {max_pages_per_league}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'SAVE TO DB'}")
    print(f"  Уже в БД: {len(already)} event IDs")
    print(f"{'='*65}\n")

    start_ts = time.time()
    total_ins = 0
    total_skip = 0
    total_no_odds = 0

    leagues = DOTA2_LEAGUES.items()
    if league_filter:
        leagues = [(lid, name) for lid, name in leagues
                   if league_filter.lower() in name.lower()]

    for league_id, league_name in leagues:
        print(f"\n── {league_name} (id={league_id}) ──")
        stats = fetch_league(client, conn, league_id, league_name,
                             already, max_pages_per_league, dry_run)
        total_ins      += stats["inserted"]
        total_skip     += stats["skipped"]
        total_no_odds  += stats["no_odds"]
        print(f"   → inserted={stats['inserted']}  skipped={stats['skipped']}  "
              f"no_odds={stats['no_odds']}  pages={stats['pages']}")

    conn.close()
    elapsed = time.time() - start_ts

    print(f"\n{'='*65}")
    print(f"  DONE")
    print(f"  Snapshots inserted:  {total_ins}")
    print(f"  Skipped (dup):       {total_skip}")
    print(f"  No odds:             {total_no_odds}")
    print(f"  Time: {elapsed:.0f}s")
    print(f"{'='*65}")

    if not dry_run and total_ins > 0:
        print(f"\nПовтори анализ:")
        print(f"  PYTHONPATH=. python3 scripts/edge_report.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages",   type=int, default=500,
                        help="Макс. страниц на лигу (default: 500)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--league",  type=str, default=None,
                        help="Фильтр по названию лиги (partial match)")
    args = parser.parse_args()

    if not settings.betsapi_token:
        print("ERROR: BETSAPI_TOKEN not set in .env")
        sys.exit(1)

    run(max_pages_per_league=args.pages, dry_run=args.dry_run,
        league_filter=args.league)


if __name__ == "__main__":
    main()
