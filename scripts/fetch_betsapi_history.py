#!/usr/bin/env python3
"""
Исторический backfill odds из BetsAPI — все esports (sport_id=151).

Что делает:
  1. Скачивает завершённые esports события (CS:GO, Dota 2, LoL, Valorant, ...)
  2. Для каждого матча получает opening/closing odds
  3. Сохраняет в odds_snapshots с полем league_name (фильтруй потом)
  4. Матчит с нашей БД по имени команды (fuzzy) — для Dota 2 матчей

Фильтрация при анализе:
  SELECT * FROM odds_snapshots WHERE league_name LIKE '%dota%'
  SELECT * FROM odds_snapshots WHERE league_name LIKE '%counter%'

Запуск:
    python3 scripts/fetch_betsapi_history.py              # 100 страниц
    python3 scripts/fetch_betsapi_history.py --pages 50   # быстрый тест
    python3 scripts/fetch_betsapi_history.py --dry-run    # только считать
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


def _already_collected(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT match_external_id FROM odds_snapshots "
        "WHERE source='betsapi' AND match_external_id IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def _add_league_column_if_missing(conn: sqlite3.Connection):
    """Добавляем league_name если старая БД без него."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_snapshots)").fetchall()}
    if "league_name" not in cols:
        conn.execute("ALTER TABLE odds_snapshots ADD COLUMN league_name TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_odds_league ON odds_snapshots(league_name)")
        conn.commit()
        print("  [DB] Добавлена колонка league_name")


def run(max_pages: int = 100, dry_run: bool = False):
    client = BetsAPIClient()
    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    _add_league_column_if_missing(conn)

    already = _already_collected(conn)
    print(f"\n{'='*60}")
    print(f"  BetsAPI Historical Odds Backfill — ALL Esports")
    print(f"  Max pages: {max_pages} (~{max_pages*50} ended events scanned)")
    print(f"  Mode: {'DRY RUN' if dry_run else 'SAVE TO DB'}")
    print(f"  Already in DB: {len(already)} event IDs")
    print(f"{'='*60}\n")

    page = 1
    total_events_scanned = 0
    total_skipped  = 0
    total_inserted = 0
    total_no_odds  = 0
    league_counts: dict[str, int] = {}
    start_ts = time.time()

    while page <= max_pages:
        try:
            data  = client.get_ended(page)
            items = data.get("results", [])
            total_events = data.get("pager", {}).get("total", 0)
        except Exception as e:
            print(f"  Page {page} ERROR: {e} — retry in 5s")
            time.sleep(5)
            try:
                data  = client.get_ended(page)
                items = data.get("results", [])
                total_events = data.get("pager", {}).get("total", 0)
            except Exception as e2:
                print(f"  Page {page} FAILED: {e2}")
                page += 1
                continue

        if not items:
            break

        total_events_scanned += len(items)

        for event in items:
            eid    = str(event.get("id", ""))
            home   = event.get("home", {}).get("name", "")
            away   = event.get("away", {}).get("name", "")
            league = event.get("league", {}).get("name", "")

            # Статистика по играм
            game_key = league.split(" ")[0].lower() if league else "unknown"
            league_counts[game_key] = league_counts.get(game_key, 0) + 1

            if eid in already:
                total_skipped += 1
                continue

            if dry_run:
                total_inserted += 1  # count only
                continue

            # Получить odds
            try:
                summary = client.get_odds_summary(eid)
                bms = _extract_moneyline(summary)
            except Exception as e:
                total_no_odds += 1
                continue

            if not bms:
                total_no_odds += 1
                continue

            # Матч к нашей БД (только для Dota 2)
            is_dota = "dota" in league.lower()
            if is_dota:
                ext_id, match_name = _match_to_db(
                    conn, home, away, str(event.get("time", ""))
                )
            else:
                ext_id = None
                match_name = f"{home} vs {away}"

            # Captured_at = время матча (ретроспективно)
            event_ts = event.get("time", "")
            try:
                captured_at = datetime.fromtimestamp(
                    int(event_ts), tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            for bm in bms:
                # Opening odds
                _insert_snapshot(
                    conn, captured_at + "_open", event, bm,
                    bm["open_home"], bm["open_away"],
                    ext_id, match_name,
                    {"type": "open", "event": event, "bm": bm},
                    league_name=league,
                )
                # Closing odds (если отличаются)
                if bm["close_home"] != bm["open_home"]:
                    _insert_snapshot(
                        conn, captured_at + "_close", event, bm,
                        bm["close_home"], bm["close_away"],
                        ext_id, match_name,
                        {"type": "close", "event": event, "bm": bm},
                        league_name=league,
                    )
                total_inserted += 1

            already.add(eid)

        conn.commit()
        time.sleep(2)  # доп. пауза между страницами

        elapsed = time.time() - start_ts
        eta = elapsed / page * (min(max_pages, total_events // 50) - page) if page > 0 else 0
        pct = page / min(max_pages, total_events // 50 + 1) * 100
        print(f"  Page {page:4} [{pct:4.0f}%] "
              f"scanned={total_events_scanned:5} inserted={total_inserted:5} "
              f"skip={total_skipped:4} no_odds={total_no_odds:3} "
              f"| ETA {eta:.0f}s")

        if page * 50 >= total_events:
            break
        page += 1

    conn.close()

    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"  Events scanned:        {total_events_scanned}")
    print(f"  Snapshots inserted:    {total_inserted}")
    print(f"  Skipped (duplicate):   {total_skipped}")
    print(f"  No odds available:     {total_no_odds}")
    print(f"  Time: {time.time()-start_ts:.0f}s")
    print(f"\n  Топ лиг по событиям:")
    for lg, cnt in sorted(league_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"    {lg:20} {cnt}")
    print(f"{'='*60}")

    if not dry_run and total_inserted > 0:
        print(f"\nТеперь строим таблицу edge:")
        print(f"  python3 scripts/edge_report.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages",   type=int, default=100,
                        help="Страниц ended событий (50 событий/стр)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только считать, не сохранять")
    args = parser.parse_args()

    if not settings.betsapi_token:
        print("ERROR: BETSAPI_TOKEN not set in .env")
        sys.exit(1)

    run(max_pages=args.pages, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
