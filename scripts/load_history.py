#!/usr/bin/env python3
"""
Загрузка 12 месяцев исторических матчей Dota 2 из PandaScore.

Что делает:
  - Скачивает все finished матчи за указанный период
  - Пропускает уже существующие в БД (по external_id)
  - Сохраняет в таблицы matches + raw_events
  - Показывает прогресс

Запуск:
    python3 scripts/load_history.py              # 12 месяцев
    python3 scripts/load_history.py --months 6   # 6 месяцев
    python3 scripts/load_history.py --months 24  # 24 месяца
    python3 scripts/load_history.py --dry-run    # только посчитать, не сохранять
"""

from __future__ import annotations

import sys, json, time, argparse, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests
from config import settings

TOKEN   = settings.pandascore_token
BASE    = settings.pandascore_base_url.rstrip("/")
DB_PATH = PROJECT_ROOT / settings.database_path
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
PER_PAGE = 100


def _now():
    return datetime.now(timezone.utc)


def _existing_ids(conn):
    rows = conn.execute("SELECT external_id FROM matches WHERE external_id IS NOT NULL").fetchall()
    return {str(r[0]) for r in rows}


def _extract_match(raw: dict) -> Optional[dict]:
    """Извлечь поля матча из PandaScore raw JSON."""
    opponents = raw.get("opponents", [])
    if len(opponents) < 2:
        return None

    t1 = opponents[0].get("opponent", {})
    t2 = opponents[1].get("opponent", {})
    t1_name = t1.get("name")
    t2_name = t2.get("name")
    if not t1_name or not t2_name:
        return None

    winner = raw.get("winner", {})
    winner_name = None
    if winner:
        winner_name = winner.get("name")
    # Fallback: results
    if not winner_name:
        results = raw.get("results", [])
        if len(results) == 2:
            scores = [(results[i].get("score", 0) or 0, i) for i in range(2)]
            best = max(scores, key=lambda x: x[0])
            if best[0] > 0:
                winner_name = [t1_name, t2_name][best[1]]

    league = raw.get("league", {}) or {}
    tournament = raw.get("tournament", {}) or {}

    return {
        "external_id":   str(raw["id"]),
        "source":        "pandascore",
        "name":          raw.get("name", f"{t1_name} vs {t2_name}"),
        "status":        raw.get("status", "finished"),
        "begin_at":      raw.get("begin_at"),
        "league_name":   league.get("name"),
        "team_1_name":   t1_name,
        "team_2_name":   t2_name,
        "winner_name":   winner_name,
        "raw_json":      json.dumps(raw, ensure_ascii=False),
    }


def _save_match(conn, m: dict):
    conn.execute("""
        INSERT INTO matches
            (external_id, source, name, status, begin_at, league_name,
             team_1_name, team_2_name, winner_name, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        m["external_id"], m["source"], m["name"], m["status"], m["begin_at"],
        m["league_name"], m["team_1_name"], m["team_2_name"],
        m["winner_name"], m["raw_json"],
    ))
    conn.execute("""
        INSERT INTO raw_events (source, event_type, external_id, payload_json)
        VALUES ('pandascore', 'match_history', ?, ?)
    """, (m["external_id"], m["raw_json"]))


def load_history(months: int = 12, dry_run: bool = False):
    now     = _now()
    from_dt = now - timedelta(days=int(months * 30.44))
    to_dt   = now - timedelta(days=1)  # вчера — только finished

    print(f"\n{'='*60}")
    print(f"  PandaScore History Loader")
    print(f"  Period: {from_dt:%Y-%m-%d} → {to_dt:%Y-%m-%d} ({months} months)")
    print(f"  Mode:   {'DRY RUN' if dry_run else 'SAVE TO DB'}")
    print(f"{'='*60}\n")

    # Подключаемся к БД
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existing = _existing_ids(conn)
    print(f"Existing matches in DB: {len(existing)}")

    # Считаем сколько страниц
    count_params = {
        "filter[videogame]": "dota-2",
        "filter[status]":    "finished",
        "range[begin_at]":   f"{from_dt:%Y-%m-%dT%H:%M:%SZ},{to_dt:%Y-%m-%dT%H:%M:%SZ}",
        "sort":              "begin_at",
        "page":              1,
        "per_page":          1,
    }
    r = requests.get(f"{BASE}/matches", headers=HEADERS, params=count_params, timeout=20)
    r.raise_for_status()
    total = int(r.headers.get("X-Total", 0))
    pages = (total + PER_PAGE - 1) // PER_PAGE
    print(f"Total matches on PandaScore: {total}")
    print(f"Pages to fetch (100/page):   {pages}")
    print(f"Estimated time:              ~{pages * 0.6:.0f} sec\n")

    if dry_run:
        print("DRY RUN — not saving anything.")
        conn.close()
        return

    inserted = 0
    skipped  = 0
    errors   = 0
    start_ts = time.time()

    for page in range(1, pages + 1):
        params = {
            "filter[videogame]": "dota-2",
            "filter[status]":    "finished",
            "range[begin_at]":   f"{from_dt:%Y-%m-%dT%H:%M:%SZ},{to_dt:%Y-%m-%dT%H:%M:%SZ}",
            "sort":              "begin_at",
            "page":              page,
            "per_page":          PER_PAGE,
        }

        try:
            r = requests.get(f"{BASE}/matches", headers=HEADERS,
                             params=params, timeout=30)
            r.raise_for_status()
            matches = r.json()
        except Exception as e:
            print(f"  Page {page} ERROR: {e} — retrying in 5s")
            time.sleep(5)
            try:
                r = requests.get(f"{BASE}/matches", headers=HEADERS,
                                 params=params, timeout=30)
                r.raise_for_status()
                matches = r.json()
            except Exception as e2:
                print(f"  Page {page} FAILED: {e2}")
                errors += 1
                continue

        if not matches:
            break

        page_new = 0
        for raw in matches:
            ext_id = str(raw.get("id", ""))
            if ext_id in existing:
                skipped += 1
                continue
            m = _extract_match(raw)
            if not m:
                errors += 1
                continue
            try:
                _save_match(conn, m)
                existing.add(ext_id)
                inserted += 1
                page_new += 1
            except Exception as e:
                errors += 1

        conn.commit()

        elapsed = time.time() - start_ts
        eta     = elapsed / page * (pages - page) if page > 0 else 0
        pct     = page / pages * 100
        rate    = inserted / elapsed if elapsed > 0 else 0
        print(f"  Page {page:3}/{pages} [{pct:4.0f}%] "
              f"+{page_new:3} new | total={inserted} skip={skipped} "
              f"| {rate:.1f}/s | ETA {eta:.0f}s")

        # Уважаем rate limit PandaScore
        time.sleep(0.4)

    conn.close()

    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"  Inserted: {inserted}")
    print(f"  Skipped (already existed): {skipped}")
    print(f"  Errors: {errors}")
    print(f"  Time: {time.time()-start_ts:.0f}s")
    print(f"{'='*60}")
    print(f"\nRun backtest to validate:")
    print(f"  python3 scripts/backtest_daily.py")


def main():
    parser = argparse.ArgumentParser(description="Load PandaScore history")
    parser.add_argument("--months",  type=int, default=12, help="Months of history (default 12)")
    parser.add_argument("--dry-run", action="store_true",  help="Count only, don't save")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: PANDASCORE_TOKEN not set in .env")
        sys.exit(1)

    load_history(months=args.months, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
