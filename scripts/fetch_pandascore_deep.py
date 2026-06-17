#!/usr/bin/env python3
"""
Расширенный сбор исторических матчей Dota 2 из PandaScore.
Пишет в ОТДЕЛЬНУЮ БД: storage/dota_research.sqlite3

Цель: максимум H2H истории для всех пар команд.
Запуск:
    PYTHONPATH=. python3 scripts/fetch_pandascore_deep.py
    PYTHONPATH=. python3 scripts/fetch_pandascore_deep.py --pages 200
"""
from __future__ import annotations

import sys, time, sqlite3, json, argparse, requests
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

DB_PATH  = PROJECT_ROOT / "storage" / "dota_research.sqlite3"
BASE_URL = "https://api.pandascore.co"
HEADERS  = {"Authorization": f"Bearer {settings.pandascore_token}"}
PAGE_SIZE = 100


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id   TEXT UNIQUE,
    begin_at      TEXT,
    league_name   TEXT,
    serie_name    TEXT,
    tournament_name TEXT,
    team_1_id     TEXT,
    team_1_name   TEXT,
    team_2_id     TEXT,
    team_2_name   TEXT,
    winner_id     TEXT,
    winner_name   TEXT,
    number_of_games INTEGER,
    status        TEXT,
    raw_json      TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(team_1_name, team_2_name);
CREATE INDEX IF NOT EXISTS idx_matches_begin ON matches(begin_at);
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league_name);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT,
    page       INTEGER,
    inserted   INTEGER,
    skipped    INTEGER
);
"""


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def fetch_page(page: int) -> list[dict]:
    r = requests.get(
        f"{BASE_URL}/dota2/matches/past",
        headers=HEADERS,
        params={
            "sort":     "-begin_at",
            "page[size]": PAGE_SIZE,
            "page[number]": page,
            "filter[status]": "finished",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def insert_match(conn: sqlite3.Connection, m: dict) -> bool:
    ext_id = str(m.get("id", ""))
    if not ext_id:
        return False

    opponents = m.get("opponents", [])
    t1 = opponents[0].get("opponent", {}) if len(opponents) > 0 else {}
    t2 = opponents[1].get("opponent", {}) if len(opponents) > 1 else {}
    winner = m.get("winner", {}) or {}

    try:
        conn.execute("""
            INSERT OR IGNORE INTO matches
              (external_id, begin_at, league_name, serie_name, tournament_name,
               team_1_id, team_1_name, team_2_id, team_2_name,
               winner_id, winner_name, number_of_games, status, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ext_id,
            m.get("begin_at"),
            m.get("league", {}).get("name"),
            m.get("serie", {}).get("full_name"),
            m.get("tournament", {}).get("name"),
            str(t1.get("id", "")),
            t1.get("name"),
            str(t2.get("id", "")),
            t2.get("name"),
            str(winner.get("id", "")),
            winner.get("name"),
            m.get("number_of_games"),
            m.get("status"),
            json.dumps(m, ensure_ascii=False),
        ))
        return conn.total_changes > 0
    except Exception as e:
        print(f"    INSERT error: {e}")
        return False


def already_fetched(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT external_id FROM matches").fetchall()}


def run(max_pages: int = 500):
    print(f"\n{'='*60}")
    print(f"  PandaScore Deep Fetch → {DB_PATH.name}")
    print(f"  max_pages: {max_pages}  page_size: {PAGE_SIZE}")
    print(f"{'='*60}\n")

    conn     = init_db()
    existing = already_fetched(conn)
    print(f"  Уже в БД: {len(existing)} матчей\n")

    total_ins = 0
    total_skip = 0
    start = time.time()

    for page in range(1, max_pages + 1):
        try:
            items = fetch_page(page)
        except requests.HTTPError as e:
            if e.response.status_code == 422:
                print(f"  page {page}: конец данных (422) — стоп")
                break
            print(f"  page {page} HTTP {e.response.status_code}: {e} — ждём 10s")
            time.sleep(10)
            continue
        except Exception as e:
            print(f"  page {page} ERROR: {e} — ждём 5s")
            time.sleep(5)
            continue

        if not items:
            print(f"  page {page}: пустой ответ — стоп")
            break

        ins = skip = 0
        for m in items:
            if str(m.get("id","")) in existing:
                skip += 1
                continue
            if insert_match(conn, m):
                ins += 1
                existing.add(str(m["id"]))
            else:
                skip += 1

        conn.execute(
            "INSERT INTO fetch_log(fetched_at, page, inserted, skipped) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), page, ins, skip)
        )
        conn.commit()
        total_ins  += ins
        total_skip += skip

        elapsed = time.time() - start
        rate = total_ins / elapsed * 60 if elapsed > 0 else 0
        oldest = min((m.get("begin_at","") or "") for m in items if m.get("begin_at"))

        print(f"  page {page:>4}  ins={ins:>3}  skip={skip:>3}  "
              f"total={total_ins:>5}  oldest={oldest[:10]}  "
              f"[{rate:.0f} mat/min]", flush=True)

        # PandaScore rate limit: ~1 req/s на free, ~2 req/s на paid
        time.sleep(1.1)

    conn.close()
    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"  Вставлено:  {total_ins}")
    print(f"  Пропущено:  {total_skip}")
    print(f"  Время:      {elapsed:.0f}s")
    print(f"  БД:         {DB_PATH}")
    print(f"{'='*60}")
    print(f"\nДля анализа H2H запусти:")
    print(f"  PYTHONPATH=. python3 scripts/analyze_h2h.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=500)
    args = parser.parse_args()

    if not settings.pandascore_token:
        print("ERROR: PANDASCORE_TOKEN not set in .env")
        sys.exit(1)

    run(max_pages=args.pages)


if __name__ == "__main__":
    main()
