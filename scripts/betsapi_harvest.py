#!/usr/bin/env python3
"""
BetsAPI Maximum Data Harvest — 24-hour session.

Phases (in order of value):
  1. Upcoming events + odds (fast, ~100 req)
  2. All ended event pages → save raw (dota2 + all esports meta)
  3. odds_summary for each Dota 2 event (open + close, all bookmakers)
  4. odds_history for each Dota 2 event (movement points)
  5. CS2 / LoL / Valorant raw events (if budget allows)

Run:
    cd /path/to/dota_trader_v2
    python3 scripts/betsapi_harvest.py

The script is resumable: it skips already-processed event_ids.
All raw API responses are stored in betsapi_harvest.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
SPORT_ID = 151  # E-sports

DB_PATH  = PROJECT_ROOT / "storage" / "betsapi_harvest.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Лимит: 1800 req/hour → 1 req каждые 2с. Ставим 2.1s для безопасности.
REQ_INTERVAL = 2.1    # секунд между запросами (Phases 1-4)
PAGE_SIZE    = 50

# Phase 5 (odds_history) более агрессивно потребляет дневной лимит.
# 6.0s = 600 req/hour = 7,200 req/12h → ~7200 матчей за ночь.
# С дневным капом скрипт останавливается и при следующем запуске продолжает.
PHASE5_EXTRA_SLEEP = 0.0   # доп. задержка сверх REQ_INTERVAL (0 = использовать базовый 2.1s = ~1700 req/h)
PHASE5_DAILY_CAP   = 6000  # максимум вызовов odds_history за одну сессию

DOTA_KEYWORDS  = ["dota", "dota 2", "dota2"]
CS2_KEYWORDS   = ["cs2", "counter-strike", "csgo", "cs:go", "cs go"]
LOL_KEYWORDS   = ["league of legends", "lol", " lol "]
VAL_KEYWORDS   = ["valorant"]


# ── Database ──────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS raw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT    UNIQUE NOT NULL,
    sport_tag   TEXT,        -- dota2 / cs2 / lol / valorant / esports
    league      TEXT,
    home_team   TEXT,
    away_team   TEXT,
    start_time  INTEGER,
    status      TEXT,        -- ended / upcoming
    score       TEXT,
    winner      TEXT,
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_re_sport  ON raw_events(sport_tag);
CREATE INDEX IF NOT EXISTS idx_re_status ON raw_events(status);

CREATE TABLE IF NOT EXISTS odds_summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    bookmaker   TEXT NOT NULL,
    market      TEXT DEFAULT '151_1',
    open_home   REAL,
    open_away   REAL,
    close_home  REAL,
    close_away  REAL,
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(event_id, bookmaker)
);
CREATE INDEX IF NOT EXISTS idx_os_event ON odds_summary(event_id);
CREATE INDEX IF NOT EXISTS idx_os_bm    ON odds_summary(bookmaker);

CREATE TABLE IF NOT EXISTS odds_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    market      TEXT NOT NULL,
    snapshot_id TEXT,
    home_od     REAL,
    away_od     REAL,
    over_od     REAL,
    under_od    REAL,
    handicap    TEXT,
    ss          TEXT,
    add_time    INTEGER,
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_oh_event ON odds_history(event_id);
CREATE INDEX IF NOT EXISTS idx_oh_market ON odds_history(market);

CREATE TABLE IF NOT EXISTS harvest_progress (
    event_id          TEXT PRIMARY KEY,
    summary_done      INTEGER DEFAULT 0,
    history_done      INTEGER DEFAULT 0,
    summary_bm_count  INTEGER DEFAULT 0,
    history_pts_count INTEGER DEFAULT 0,
    updated_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS api_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint     TEXT,
    params_json  TEXT,
    status_code  INTEGER,
    duration_ms  INTEGER,
    error        TEXT,
    called_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS harvest_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    # Миграция: если таблица odds_history старой схемы (есть колонка bookmaker) — пересоздаём.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_history)").fetchall()}
    if "bookmaker" in cols:
        print("[DB] Migrating odds_history to new schema (no bookmaker, adds handicap/ss/over_od)...")
        conn.execute("DROP TABLE IF EXISTS odds_history")
        conn.commit()
        conn.close()
        # Переоткрываем после DROP чтобы соединение было чистым
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS odds_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT NOT NULL,
                market      TEXT NOT NULL,
                snapshot_id TEXT,
                home_od     REAL,
                away_od     REAL,
                over_od     REAL,
                under_od    REAL,
                handicap    TEXT,
                ss          TEXT,
                add_time    INTEGER,
                raw_json    TEXT NOT NULL,
                fetched_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_oh_event  ON odds_history(event_id);
            CREATE INDEX IF NOT EXISTS idx_oh_market ON odds_history(market);
        """)
        conn.commit()
        print("[DB] Migration complete.")
    return conn


# ── API client ────────────────────────────────────────────────────────────────
class BetsAPI:
    def __init__(self, token: str, conn: sqlite3.Connection):
        self.token   = token
        self.conn    = conn
        self._last   = 0.0
        self._total  = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "DotaHarvest/1.0"})

    def _get(self, path: str, params: dict | None = None,
             retry: int = 3) -> dict:
        # rate limit
        elapsed = time.time() - self._last
        if elapsed < REQ_INTERVAL:
            time.sleep(REQ_INTERVAL - elapsed)

        p = {"token": self.token, **(params or {})}
        t0 = time.time()
        err_msg = None
        status  = 0
        try:
            r = self.session.get(f"{BASE}{path}", params=p, timeout=20)
            status = r.status_code
            dur_ms = int((time.time() - t0) * 1000)

            if r.status_code == 429:
                wait = 120
                print(f"  [429] rate limit — wait {wait}s (retry={retry})", flush=True)
                time.sleep(wait)
                if retry > 0:
                    return self._get(path, params, retry=retry - 1)
                raise RuntimeError("BetsAPI 429 after retries")

            r.raise_for_status()
            data = r.json()

            if not data.get("success"):
                raise RuntimeError(f"API error: {data}")

            self._total += 1
            self._last   = time.time()
            self._log(path, params, status, dur_ms, None)
            return data

        except Exception as e:
            dur_ms = int((time.time() - t0) * 1000)
            err_msg = str(e)
            self._log(path, params, status, dur_ms, err_msg)
            if retry > 0 and "429" not in err_msg:
                time.sleep(3)
                return self._get(path, params, retry=retry - 1)
            raise

    def _log(self, path, params, status, dur_ms, error):
        try:
            self.conn.execute(
                "INSERT INTO api_log(endpoint,params_json,status_code,duration_ms,error)"
                " VALUES(?,?,?,?,?)",
                (path, json.dumps(params, ensure_ascii=False), status, dur_ms, error)
            )
            self.conn.commit()
        except Exception:
            pass

    def ended(self, page: int = 1, day: str | None = None) -> dict:
        params: dict = {"sport_id": SPORT_ID, "page": page}
        if day:
            params["day"] = day  # YYYYMMDD — обходит лимит 100 страниц
        return self._get("/v3/events/ended", params)

    def upcoming(self, page: int = 1) -> dict:
        return self._get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})

    def odds_summary(self, event_id: str) -> dict:
        return self._get("/v2/event/odds/summary", {"event_id": event_id})

    def odds_history(self, event_id: str) -> dict:
        """Get full odds history — no source filter = all bookmakers."""
        return self._get("/v2/event/odds", {
            "event_id":   event_id,
            "since_time": "0",
        })


# ── Helpers ───────────────────────────────────────────────────────────────────
def tag_sport(league: str) -> str:
    ll = league.lower()
    if any(k in ll for k in DOTA_KEYWORDS):  return "dota2"
    if any(k in ll for k in CS2_KEYWORDS):   return "cs2"
    if any(k in ll for k in LOL_KEYWORDS):   return "lol"
    if any(k in ll for k in VAL_KEYWORDS):   return "valorant"
    return "esports"


def parse_event(e: dict, status: str) -> dict:
    league = (e.get("league") or {}).get("name", "")
    home   = (e.get("home")   or {}).get("name", "")
    away   = (e.get("away")   or {}).get("name", "")
    ss     = e.get("ss") or e.get("score", "")
    winner = None
    if ss and "-" in str(ss):
        parts = str(ss).split("-")
        try:
            h, a = int(parts[0].strip()), int(parts[1].strip())
            winner = home if h > a else (away if a > h else "draw")
        except Exception:
            pass
    return {
        "event_id":   str(e.get("id", "")),
        "sport_tag":  tag_sport(league),
        "league":     league,
        "home_team":  home,
        "away_team":  away,
        "start_time": e.get("time"),
        "status":     status,
        "score":      ss,
        "winner":     winner,
        "raw_json":   json.dumps(e, ensure_ascii=False),
    }


def _safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Phase runners ─────────────────────────────────────────────────────────────

def phase1_upcoming(api: BetsAPI, conn: sqlite3.Connection) -> int:
    """Download all upcoming events."""
    print("\n[Phase 1] Upcoming events...", flush=True)
    inserted = 0
    page = 1
    while True:
        data  = api.upcoming(page)
        items = data.get("results", [])
        if not items:
            break
        total = data.get("pager", {}).get("total", 0)

        for e in items:
            rec = parse_event(e, "upcoming")
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO raw_events
                       (event_id,sport_tag,league,home_team,away_team,
                        start_time,status,score,winner,raw_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (rec["event_id"], rec["sport_tag"], rec["league"],
                     rec["home_team"], rec["away_team"], rec["start_time"],
                     rec["status"], rec["score"], rec["winner"], rec["raw_json"])
                )
                inserted += 1
            except Exception:
                pass
        conn.commit()

        dota = sum(1 for e in items if tag_sport((e.get("league") or {}).get("name","")) == "dota2")
        print(f"  page {page}/{(total+PAGE_SIZE-1)//PAGE_SIZE} — {len(items)} events ({dota} Dota 2)", flush=True)

        if page * PAGE_SIZE >= total or not items:
            break
        page += 1

    print(f"  → {inserted} upcoming events saved", flush=True)
    return inserted


def phase2_upcoming_odds(api: BetsAPI, conn: sqlite3.Connection) -> int:
    """Download odds_summary for ALL upcoming Dota 2 events."""
    print("\n[Phase 2] Upcoming Dota 2 odds...", flush=True)
    rows = conn.execute(
        "SELECT event_id, home_team, away_team FROM raw_events"
        " WHERE status='upcoming' AND sport_tag='dota2'"
    ).fetchall()

    inserted = 0
    for i, row in enumerate(rows):
        eid = row["event_id"]
        try:
            data = api.odds_summary(eid)
            raw  = json.dumps(data, ensure_ascii=False)
            bms  = _parse_summary(data, eid, raw)
            for bm in bms:
                conn.execute(
                    """INSERT OR REPLACE INTO odds_summary
                       (event_id,bookmaker,market,open_home,open_away,
                        close_home,close_away,raw_json)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (eid, bm["bookmaker"], "151_1",
                     bm["open_home"], bm["open_away"],
                     bm["close_home"], bm["close_away"], raw)
                )
                inserted += 1
            conn.commit()
            if (i+1) % 10 == 0:
                print(f"  {i+1}/{len(rows)} — {row['home_team']} vs {row['away_team']} "
                      f"({len(bms)} bookmakers)", flush=True)
        except Exception as ex:
            print(f"  [WARN] odds_summary {eid}: {ex}", flush=True)

    print(f"  → {inserted} upcoming odds rows", flush=True)
    return inserted


def phase3_ended_events(api: BetsAPI, conn: sqlite3.Connection) -> int:
    """
    Download ALL ended events by iterating day-by-day (YYYYMMDD).

    BetsAPI hard-limits pagination to 100 pages per query (~5000 events).
    The workaround: add ?day=YYYYMMDD — each day is independently pageable.
    We go from today back to 2015-01-01 (~4000 days × up to 100 pages).
    """
    from datetime import date, timedelta

    print(f"\n[Phase 3] Ended events by day (2015→today)...", flush=True)

    # Resume: find which days we've already completed
    done_days = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT value FROM harvest_meta WHERE key LIKE 'day_done_%'"
        ).fetchall()
    )

    # Date range: today → 2015-01-01
    today      = date.today()
    start_date = date(2022, 1, 1)
    all_days   = []
    d = today
    while d >= start_date:
        all_days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)

    remaining = [d for d in all_days if d not in done_days]
    print(f"  {len(all_days)} days total, {len(done_days)} already done, "
          f"{len(remaining)} remaining", flush=True)

    inserted  = 0
    days_done = 0

    for day_str in remaining:
        page = 1
        day_inserted = 0
        day_dota = 0

        while page <= 100:  # API hard limit per day
            try:
                data  = api.ended(page=page, day=day_str)
                items = data.get("results", [])
                if not items:
                    break
                total = data.get("pager", {}).get("total", 0)

                for e in items:
                    rec = parse_event(e, "ended")
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO raw_events
                               (event_id,sport_tag,league,home_team,away_team,
                                start_time,status,score,winner,raw_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (rec["event_id"], rec["sport_tag"], rec["league"],
                             rec["home_team"], rec["away_team"], rec["start_time"],
                             rec["status"], rec["score"], rec["winner"], rec["raw_json"])
                        )
                        day_inserted += 1
                        if rec["sport_tag"] == "dota2":
                            day_dota += 1
                    except Exception:
                        pass

                if page * PAGE_SIZE >= total:
                    break
                page += 1

            except KeyboardInterrupt:
                conn.commit()
                print("  Interrupted — progress saved.")
                return inserted
            except Exception as ex:
                print(f"  [WARN] {day_str} page {page}: {ex}", flush=True)
                break

        conn.commit()
        # Mark day as done
        conn.execute(
            "INSERT OR REPLACE INTO harvest_meta(key,value) VALUES(?,?)",
            (f"day_done_{day_str}", day_str)
        )
        conn.commit()

        inserted  += day_inserted
        days_done += 1

        if days_done % 10 == 0 or day_dota > 0:
            total_events = conn.execute(
                "SELECT COUNT(*) as c FROM raw_events WHERE status='ended'"
            ).fetchone()["c"]
            print(f"  {day_str} — {day_inserted} events ({day_dota} Dota2) | "
                  f"total_db={total_events} | api={api._total} | "
                  f"days={days_done}/{len(remaining)}", flush=True)

    print(f"  → {inserted} ended events saved across {days_done} days", flush=True)
    return inserted


def _parse_summary(data: dict, event_id: str, raw: str) -> list[dict]:
    """Extract per-bookmaker open/close from /v2/event/odds/summary response."""
    rows = []
    results = data.get("results", {})
    for bm_name, bm_data in results.items():
        odds  = bm_data.get("odds", {})
        start = odds.get("start", {})
        end   = odds.get("end", {}) or start

        # Try market 151_1
        for mk_key in ["151_1", "1_1", "1"]:
            mk_s = start.get(mk_key, {})
            mk_e = end.get(mk_key, {}) or mk_s
            if mk_s:
                break

        if not mk_s:
            continue

        oh = _safe_float(mk_s.get("home_od") or mk_s.get("home_od") or mk_s.get("1"))
        oa = _safe_float(mk_s.get("away_od") or mk_s.get("away_od") or mk_s.get("2"))
        ch = _safe_float(mk_e.get("home_od") or mk_e.get("home_od") or mk_e.get("1"))
        ca = _safe_float(mk_e.get("away_od") or mk_e.get("away_od") or mk_e.get("2"))

        if not oh and not oa:
            continue

        rows.append({
            "bookmaker":  bm_name,
            "open_home":  oh,
            "open_away":  oa,
            "close_home": ch or oh,
            "close_away": ca or oa,
        })
    return rows


def _parse_history(data: dict, event_id: str) -> list[dict]:
    """
    Extract snapshots from /v2/event/odds response.

    Actual API structure (confirmed 2026-06-16):
    {
      "results": {
        "stats": {"matching_dir": 1, "odds_update": {}},
        "odds": {
          "151_1": [{"id":"...","home_od":"2.625","away_od":"1.444","ss":null,"add_time":"..."}],
          "151_2": [{"id":"...","home_od":"1.444","away_od":"2.625","handicap":"+1.5","ss":null,"add_time":"..."}],
          "151_3": []
        }
      }
    }
    No bookmaker field — data is aggregate (single source per event).
    """
    rows = []
    results = data.get("results", {})
    if not isinstance(results, dict):
        return rows

    odds = results.get("odds", {})
    if not isinstance(odds, dict):
        return rows

    for market_key, snapshots in odds.items():
        if not isinstance(snapshots, list):
            continue
        for snap in snapshots:
            if not isinstance(snap, dict):
                continue
            rows.append({
                "market":      market_key,
                "snapshot_id": snap.get("id"),
                "home_od":     _safe_float(snap.get("home_od")),
                "away_od":     _safe_float(snap.get("away_od")),
                "over_od":     _safe_float(snap.get("over_od")),
                "under_od":    _safe_float(snap.get("under_od")),
                "handicap":    snap.get("handicap"),
                "ss":          snap.get("ss"),
                "add_time":    snap.get("add_time"),
                "raw_json":    json.dumps(snap, ensure_ascii=False),
            })
    return rows


def phase4_dota2_odds_summary(api: BetsAPI, conn: sqlite3.Connection) -> int:
    """Download odds_summary for ALL historical Dota 2 events."""
    print("\n[Phase 4] Dota 2 historical odds_summary...", flush=True)

    # All Dota 2 ended events not yet processed
    rows = conn.execute(
        """SELECT re.event_id, re.home_team, re.away_team
           FROM raw_events re
           LEFT JOIN harvest_progress hp ON re.event_id = hp.event_id
           WHERE re.sport_tag='dota2' AND re.status='ended'
             AND (hp.summary_done IS NULL OR hp.summary_done = 0)
           ORDER BY re.start_time DESC"""
    ).fetchall()

    total = len(rows)
    print(f"  {total} Dota 2 events to process for odds_summary", flush=True)

    inserted = 0
    for i, row in enumerate(rows):
        eid = row["event_id"]
        try:
            data = api.odds_summary(eid)
            raw  = json.dumps(data, ensure_ascii=False)
            bms  = _parse_summary(data, eid, raw)

            for bm in bms:
                conn.execute(
                    """INSERT OR REPLACE INTO odds_summary
                       (event_id,bookmaker,market,open_home,open_away,
                        close_home,close_away,raw_json)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (eid, bm["bookmaker"], "151_1",
                     bm["open_home"], bm["open_away"],
                     bm["close_home"], bm["close_away"], raw)
                )
                inserted += 1

            conn.execute(
                """INSERT OR REPLACE INTO harvest_progress
                   (event_id, summary_done, summary_bm_count, updated_at)
                   VALUES(?, 1, ?, ?)
                   ON CONFLICT(event_id) DO UPDATE SET
                     summary_done=1, summary_bm_count=?, updated_at=?""",
                (eid, len(bms), now_iso(), len(bms), now_iso())
            )
            conn.commit()

            if (i + 1) % 50 == 0:
                pct = (i+1) / total * 100
                print(f"  [{pct:.1f}%] {i+1}/{total} | "
                      f"{row['home_team']} vs {row['away_team']} "
                      f"({len(bms)} bm) | total_rows={inserted} | "
                      f"api_calls={api._total}", flush=True)

        except KeyboardInterrupt:
            print("  Interrupted — progress saved.")
            break
        except Exception as ex:
            print(f"  [WARN] odds_summary {eid}: {ex}", flush=True)
            conn.execute(
                """INSERT OR IGNORE INTO harvest_progress(event_id) VALUES(?)""", (eid,)
            )
            conn.commit()

    print(f"  → {inserted} odds_summary rows inserted", flush=True)
    return inserted


def phase5_dota2_odds_history(api: BetsAPI, conn: sqlite3.Connection,
                               max_events: int = 15000,
                               daily_cap: int | None = None,
                               limit: int | None = None,
                               dry_run: bool = False) -> int:
    """Download full odds movement history for Dota 2 events.

    dry_run=True: делает API-вызовы и парсит, но НЕ пишет в БД.
    limit=N: обработать только N событий (для теста парсера).
    """
    cap = daily_cap if daily_cap is not None else PHASE5_DAILY_CAP
    fetch_limit = limit if limit is not None else max_events
    print(f"\n[Phase 5] Dota 2 odds history (up to {fetch_limit} events, "
          f"session cap={cap}, interval={REQ_INTERVAL + PHASE5_EXTRA_SLEEP:.1f}s"
          f"{', DRY RUN' if dry_run else ''})...",
          flush=True)

    rows = conn.execute(
        """SELECT re.event_id, re.home_team, re.away_team
           FROM raw_events re
           JOIN harvest_progress hp ON re.event_id = hp.event_id
           WHERE re.sport_tag='dota2' AND re.status='ended'
             AND hp.summary_done = 1
             AND (hp.history_done IS NULL OR hp.history_done = 0)
           ORDER BY re.start_time DESC
           LIMIT ?""",
        (fetch_limit,)
    ).fetchall()

    total = len(rows)
    done_already = conn.execute(
        "SELECT COUNT(*) FROM harvest_progress WHERE history_done=1"
    ).fetchone()[0]
    print(f"  {total} events remaining | {done_already} already done", flush=True)
    if total == 0:
        print("  [Phase 5] All done!", flush=True)
        return 0

    inserted     = 0
    session_calls = 0  # track calls this session for cap

    for i, row in enumerate(rows):
        if session_calls >= cap:
            print(f"\n  [Phase 5] Daily cap reached ({cap} calls). "
                  f"Stopping — re-run to continue.", flush=True)
            break

        eid = row["event_id"]
        try:
            data   = api.odds_history(eid)
            points = _parse_history(data, eid)

            print(f"  [{i+1}] {row['home_team']} vs {row['away_team']} "
                  f"→ {len(points)} snapshots"
                  f"{' [DRY RUN, not saved]' if dry_run else ''}", flush=True)

            if not dry_run:
                for pt in points:
                    conn.execute(
                        """INSERT INTO odds_history
                           (event_id,market,snapshot_id,home_od,away_od,
                            over_od,under_od,handicap,ss,add_time,raw_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (eid, pt["market"], pt["snapshot_id"],
                         pt["home_od"], pt["away_od"],
                         pt["over_od"], pt["under_od"],
                         pt["handicap"], pt["ss"],
                         pt["add_time"], pt["raw_json"])
                    )
                    inserted += 1

                conn.execute(
                    """UPDATE harvest_progress
                       SET history_done=1, history_pts_count=?, updated_at=?
                       WHERE event_id=?""",
                    (len(points), now_iso(), eid)
                )
                conn.commit()

            session_calls += 1

            # Extra throttle beyond REQ_INTERVAL to stay within daily limit
            time.sleep(PHASE5_EXTRA_SLEEP)

            if (i + 1) % 50 == 0 or session_calls % 100 == 0:
                pct = (i+1) / total * 100
                remaining = total - (i + 1)
                eta_h = remaining * (REQ_INTERVAL + PHASE5_EXTRA_SLEEP) / 3600
                print(f"  [{pct:.1f}%] {i+1}/{total} remaining={remaining} "
                      f"({row['home_team']} vs {row['away_team']}, {len(points)} pts) "
                      f"| session={session_calls}/{cap} | ETA={eta_h:.1f}h", flush=True)

        except KeyboardInterrupt:
            print("  Interrupted — progress saved.")
            break
        except Exception as ex:
            print(f"  [WARN] odds_history {eid}: {ex}", flush=True)
            conn.execute(
                "UPDATE harvest_progress SET history_done=-1 WHERE event_id=?", (eid,)
            )
            conn.commit()

    done_now = conn.execute(
        "SELECT COUNT(*) FROM harvest_progress WHERE history_done=1"
    ).fetchone()[0]
    total_events = conn.execute(
        "SELECT COUNT(*) FROM harvest_progress WHERE summary_done=1"
    ).fetchone()[0]
    print(f"  → {inserted} movement points inserted this session", flush=True)
    print(f"  → History progress: {done_now}/{total_events} "
          f"({done_now/total_events*100:.1f}%)", flush=True)
    if done_now < total_events:
        print(f"  → Re-run script to continue ({total_events - done_now} remaining)", flush=True)
    return inserted


def phase6_other_sports(api: BetsAPI, conn: sqlite3.Connection,
                         budget_req: int = 3000) -> dict:
    """
    Download raw events for CS2 / LoL / Valorant by iterating days.
    Reuses already-fetched days from phase3 — no extra requests needed
    since phase3 already saved all esports. Just count what we have.
    """
    print(f"\n[Phase 6] Other esports raw (from existing DB)...", flush=True)
    counts = {}
    for tag in ("cs2", "lol", "valorant", "esports"):
        c = conn.execute(
            "SELECT COUNT(*) as c FROM raw_events WHERE sport_tag=? AND status='ended'",
            (tag,)
        ).fetchone()["c"]
        counts[tag] = c

    print(f"  CS2={counts['cs2']} LoL={counts['lol']} "
          f"Valorant={counts['valorant']} Other={counts['esports']}", flush=True)
    return counts


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(conn: sqlite3.Connection, api: BetsAPI):
    print("\n" + "="*60, flush=True)
    print("HARVEST REPORT", flush=True)
    print("="*60, flush=True)

    api_calls = conn.execute("SELECT COUNT(*) as c FROM api_log").fetchone()["c"]
    print(f"  API requests logged:    {api_calls}", flush=True)

    dota_events = conn.execute(
        "SELECT COUNT(*) as c FROM raw_events WHERE sport_tag='dota2'"
    ).fetchone()["c"]
    print(f"  Dota 2 events total:    {dota_events}", flush=True)

    dota_ended = conn.execute(
        "SELECT COUNT(*) as c FROM raw_events WHERE sport_tag='dota2' AND status='ended'"
    ).fetchone()["c"]
    print(f"  Dota 2 ended:           {dota_ended}", flush=True)

    dota_upcoming = conn.execute(
        "SELECT COUNT(*) as c FROM raw_events WHERE sport_tag='dota2' AND status='upcoming'"
    ).fetchone()["c"]
    print(f"  Dota 2 upcoming:        {dota_upcoming}", flush=True)

    odds_rows = conn.execute("SELECT COUNT(*) as c FROM odds_summary").fetchone()["c"]
    print(f"  Odds summary rows:      {odds_rows}", flush=True)

    unique_matches = conn.execute(
        "SELECT COUNT(DISTINCT event_id) as c FROM odds_summary"
    ).fetchone()["c"]
    print(f"  Unique matches w/ odds: {unique_matches}", flush=True)

    bookmakers = conn.execute(
        "SELECT COUNT(DISTINCT bookmaker) as c FROM odds_summary"
    ).fetchone()["c"]
    print(f"  Unique bookmakers:      {bookmakers}", flush=True)

    bm_list = conn.execute(
        "SELECT bookmaker, COUNT(*) as c FROM odds_summary"
        " GROUP BY bookmaker ORDER BY c DESC LIMIT 20"
    ).fetchall()
    print(f"\n  Bookmakers breakdown:", flush=True)
    for bm in bm_list:
        print(f"    {bm['bookmaker']:<25} {bm['c']:>6} lines", flush=True)

    oc_pairs = conn.execute(
        "SELECT COUNT(*) as c FROM odds_summary"
        " WHERE open_home IS NOT NULL AND close_home IS NOT NULL"
        "   AND open_home != close_home"
    ).fetchone()["c"]
    print(f"\n  Open+Close pairs:       {odds_rows}", flush=True)
    print(f"  Lines that moved:       {oc_pairs}", flush=True)

    hist_pts = conn.execute("SELECT COUNT(*) as c FROM odds_history").fetchone()["c"]
    hist_events = conn.execute(
        "SELECT COUNT(DISTINCT event_id) as c FROM odds_history"
    ).fetchone()["c"]
    print(f"  History movement pts:   {hist_pts}", flush=True)
    print(f"  Events w/ history:      {hist_events}", flush=True)

    # Market types
    markets = conn.execute(
        "SELECT DISTINCT market FROM odds_history"
    ).fetchall()
    print(f"\n  Markets in history:     {[m['market'] for m in markets]}", flush=True)

    for tag in ("cs2", "lol", "valorant"):
        c = conn.execute(
            "SELECT COUNT(*) as c FROM raw_events WHERE sport_tag=?", (tag,)
        ).fetchone()["c"]
        print(f"  {tag.upper():<10} raw events:    {c}", flush=True)

    db_size = DB_PATH.stat().st_size / 1024 / 1024
    print(f"\n  DB size:                {db_size:.1f} MB", flush=True)
    print("="*60, flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="BetsAPI Harvest")
    parser.add_argument(
        "--phase5-only", action="store_true",
        help="Пропустить Phases 1-4, запустить только Phase 5 (odds_history)."
    )
    parser.add_argument(
        "--cap", type=int, default=PHASE5_DAILY_CAP, metavar="N",
        help=f"Лимит вызовов Phase 5 за одну сессию (default: {PHASE5_DAILY_CAP})"
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Phase 5: обработать только первые N событий (для тестирования)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Phase 5: делать API-вызовы, парсить, но НЕ писать в БД"
    )
    parser.add_argument(
        "--reset-fake", action="store_true",
        help="Сбросить history_done=0 для всех событий где history_done=1 но нет строк в odds_history"
    )
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN not set in .env")
        sys.exit(1)

    conn = open_db(DB_PATH)

    # --reset-fake: сбросить ложные history_done=1
    if args.reset_fake:
        fakes = conn.execute("""
            SELECT COUNT(*) as c FROM harvest_progress hp
            WHERE hp.history_done = 1
              AND NOT EXISTS (SELECT 1 FROM odds_history oh WHERE oh.event_id = hp.event_id)
        """).fetchone()["c"]
        conn.execute("""
            UPDATE harvest_progress SET history_done=0, history_pts_count=0
            WHERE history_done = 1
              AND NOT EXISTS (SELECT 1 FROM odds_history oh WHERE oh.event_id = event_id)
        """)
        conn.commit()
        print(f"[reset-fake] Сброшено {fakes} событий history_done=1→0")
        if not (args.phase5_only or args.dry_run):
            return

    mode = "Phase 5 only" if args.phase5_only else "Full harvest"
    if args.dry_run:
        mode += " [DRY RUN]"
    print(f"BetsAPI Harvest [{mode}] — {now_iso()}")
    print(f"Token:  {TOKEN[:8]}...")
    print(f"DB:     {DB_PATH}")
    print(f"Rate:   1 req/{REQ_INTERVAL}s (Phase 5: {REQ_INTERVAL + PHASE5_EXTRA_SLEEP:.1f}s)")
    if args.phase5_only or args.dry_run:
        print(f"Cap:    {args.cap} calls this session")
    if args.limit:
        print(f"Limit:  {args.limit} events")
    if args.dry_run:
        print("DRY RUN: данные НЕ будут записаны в БД")
    api  = BetsAPI(TOKEN, conn)

    # Save start time
    conn.execute(
        "INSERT OR REPLACE INTO harvest_meta(key,value) VALUES('started_at',?)",
        (now_iso(),)
    )
    conn.commit()

    try:
        if args.phase5_only:
            # Jump straight to Phase 5 — Phases 1-4 already done
            phase5_dota2_odds_history(
                api, conn, max_events=15000, daily_cap=args.cap,
                limit=args.limit, dry_run=args.dry_run
            )
        else:
            # Phase 1 + 2: Upcoming
            phase1_upcoming(api, conn)
            phase2_upcoming_odds(api, conn)

            # Phase 3: All ended events (event metadata only)
            phase3_ended_events(api, conn)

            # Phase 4: odds_summary for all Dota 2 events
            phase4_dota2_odds_summary(api, conn)

            # Phase 5: odds_history (movement points) — up to 15K events
            phase5_dota2_odds_history(api, conn, max_events=15000, daily_cap=args.cap)

            # Phase 6: CS2/LoL/Valorant raw (budget 3000 req)
            phase6_other_sports(api, conn, budget_req=3000)

    except KeyboardInterrupt:
        print("\n[!] Keyboard interrupt — partial data saved", flush=True)

    # Final report
    conn.execute(
        "INSERT OR REPLACE INTO harvest_meta(key,value) VALUES('finished_at',?)",
        (now_iso(),)
    )
    conn.commit()
    print_report(conn, api)
    conn.close()


if __name__ == "__main__":
    main()
