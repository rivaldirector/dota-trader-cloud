#!/usr/bin/env python3
"""
Live/Upcoming Poller — редкий polling текущих матчей и коэффициентов.
Работает параллельно с Phase 5, использует <2% общего rate limit.

Интервал: 1 запуск каждые 15 минут (~8 API-запросов/запуск = ~32 req/hour).
Пишет в storage/live_tracking.db (отдельно от betsapi_harvest.db).

Usage:
    python3 scripts/live_poller.py              # бесконечный цикл
    python3 scripts/live_poller.py --once       # один раз и выход
    python3 scripts/live_poller.py --interval 900  # интервал в секундах (default 900=15min)
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
SPORT_ID = 151  # E-sports
DB_PATH  = ROOT / "storage" / "live_tracking.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS live_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    home_team   TEXT,
    away_team   TEXT,
    league      TEXT,
    start_time  INTEGER,
    status      TEXT,
    market      TEXT,
    home_od     REAL,
    away_od     REAL,
    over_od     REAL,
    under_od    REAL,
    handicap    TEXT,
    ss          TEXT,
    snap_time   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ls_event ON live_snapshots(event_id);
CREATE INDEX IF NOT EXISTS idx_ls_time  ON live_snapshots(snap_time);

CREATE TABLE IF NOT EXISTS poll_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at   TEXT,
    events_found INTEGER,
    snapshots_saved INTEGER,
    api_calls   INTEGER,
    error       TEXT
);
"""

REQ_GAP = 2.1  # минимум 2.1s между запросами внутри одного поллинга


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(session: requests.Session, path: str, params: dict, last: list) -> dict | None:
    elapsed = time.time() - last[0]
    if elapsed < REQ_GAP:
        time.sleep(REQ_GAP - elapsed)
    try:
        r = session.get(f"{BASE}{path}", params={"token": TOKEN, **params}, timeout=15)
        last[0] = time.time()
        if r.status_code == 429:
            print(f"  [429] Rate limit — пропускаем этот запрос", flush=True)
            return None
        r.raise_for_status()
        data = r.json()
        return data if data.get("success") else None
    except Exception as e:
        print(f"  [ERR] {path}: {e}", flush=True)
        return None


def safe_float(v):
    try:
        return float(v) if v else None
    except (ValueError, TypeError):
        return None


def poll_once(db: sqlite3.Connection) -> tuple[int, int, int]:
    """Возвращает (events_found, snapshots_saved, api_calls)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "LivePoller/1.0"})
    last = [0.0]
    api_calls = 0
    snapshots = 0

    # 1. Получаем upcoming события
    data = get(session, "/v3/events/upcoming", {"sport_id": SPORT_ID, "page": 1}, last)
    api_calls += 1
    if not data:
        return 0, 0, api_calls

    events = data.get("results", [])
    # Фильтр: только Dota 2
    dota_events = [
        e for e in events
        if any(k in e.get("league", {}).get("name", "").lower()
               for k in ["dota", "dota 2", "dota2"])
    ]

    print(f"  Upcoming: {len(events)} total, {len(dota_events)} Dota 2", flush=True)

    for evt in dota_events[:8]:  # максимум 8 событий за раз
        eid       = evt.get("id", "")
        home      = evt.get("home", {}).get("name", "")
        away      = evt.get("away", {}).get("name", "")
        league    = evt.get("league", {}).get("name", "")
        start_ts  = evt.get("time", 0)
        status    = "upcoming"

        # 2. Получаем текущие коэффициенты
        odds_data = get(session, "/v2/event/odds/summary", {"event_id": eid}, last)
        api_calls += 1
        if not odds_data:
            continue

        results = odds_data.get("results", {})
        for bm_name, bm_data in results.items():
            if bm_name not in ("PinnacleSports", "Bet365", "GGBet"):
                continue  # берём только топ букмекеров для live tracking
            od = bm_data.get("odds", {}) or {}
            end = od.get("end") or od.get("start") or {}
            if not end:
                continue

            for market_key in ("151_1", "151_2", "151_3"):
                m = end.get(market_key)
                if not isinstance(m, dict):
                    continue
                db.execute(
                    """INSERT INTO live_snapshots
                       (event_id,home_team,away_team,league,start_time,status,
                        market,home_od,away_od,over_od,under_od,handicap,ss,raw_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, home, away, league, start_ts, status,
                     market_key,
                     safe_float(m.get("home_od")),
                     safe_float(m.get("away_od")),
                     safe_float(m.get("over_od")),
                     safe_float(m.get("under_od")),
                     m.get("handicap"),
                     m.get("ss"),
                     json.dumps(m, ensure_ascii=False))
                )
                snapshots += 1

    db.commit()
    return len(dota_events), snapshots, api_calls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",     action="store_true", help="Один запуск и выход")
    parser.add_argument("--interval", type=int, default=900,
                        help="Секунд между запусками (default: 900 = 15 мин)")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN not set"); sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()

    print(f"Live Poller — interval={args.interval}s (~{3600//args.interval} runs/hour)")
    print(f"DB: {DB_PATH}")
    print(f"Rate budget: ~{3600//args.interval * 8} req/hour (safe alongside Phase 5)\n")

    while True:
        ts = now_iso()
        print(f"[{ts}] Polling...", flush=True)
        try:
            events, snaps, calls = poll_once(db)
            db.execute(
                "INSERT INTO poll_log(polled_at,events_found,snapshots_saved,api_calls) VALUES(?,?,?,?)",
                (ts, events, snaps, calls)
            )
            db.commit()
            print(f"  → {events} events, {snaps} snapshots, {calls} API calls", flush=True)
        except Exception as e:
            db.execute("INSERT INTO poll_log(polled_at,error) VALUES(?,?)", (ts, str(e)))
            db.commit()
            print(f"  [ERR] {e}", flush=True)

        if args.once:
            break

        next_run = args.interval
        print(f"  Следующий запуск через {next_run//60} мин. "
              f"(Ctrl+C для остановки)\n", flush=True)
        time.sleep(next_run)


if __name__ == "__main__":
    main()
