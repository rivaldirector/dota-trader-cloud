#!/usr/bin/env python3
"""
Phase 5 overnight run — максимальная скорость, без капов, без остановок.

Читает из betsapi_harvest.db, пишет туда же.
Останавливается только когда все события обработаны.

Usage:
    python3 scripts/phase5_run.py
    python3 scripts/phase5_run.py --interval 2.0   # default: 2.0s = 1800 req/h
    python3 scripts/phase5_run.py --dry-run         # без записи в БД
"""
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

TOKEN   = os.getenv("BETSAPI_TOKEN", "")
BASE    = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
DB_PATH = ROOT / "storage" / "betsapi_harvest.db"

COOLDOWNS = [60, 120, 300, 600]   # авто-cooldown при 429 (сек)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_float(v):
    try: return float(v) if v is not None else None
    except: return None


def fetch(session, event_id, last_req, interval):
    for attempt, wait in enumerate([0] + COOLDOWNS):
        if wait:
            print(f"\n  [429] cooldown {wait}s (попытка {attempt}/{len(COOLDOWNS)})...", flush=True)
            time.sleep(wait)

        elapsed = time.time() - last_req[0]
        if elapsed < interval:
            time.sleep(interval - elapsed)

        r = session.get(
            f"{BASE}/v2/event/odds",
            params={"token": TOKEN, "event_id": event_id, "since_time": "0"},
            timeout=20
        )
        last_req[0] = time.time()

        if r.status_code == 429:
            if attempt == len(COOLDOWNS):
                raise RuntimeError("429 persistent")
            continue

        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"API error: {d}")
        return d

    raise RuntimeError("unreachable")


def parse(data):
    rows = []
    results = data.get("results", {})
    if not isinstance(results, dict):
        return rows
    for market, snaps in results.get("odds", {}).items():
        if not isinstance(snaps, list):
            continue
        for s in snaps:
            if not isinstance(s, dict):
                continue
            rows.append({
                "market":   market,
                "snap_id":  s.get("id"),
                "home_od":  safe_float(s.get("home_od")),
                "away_od":  safe_float(s.get("away_od")),
                "over_od":  safe_float(s.get("over_od")),
                "under_od": safe_float(s.get("under_od")),
                "handicap": s.get("handicap"),
                "ss":       s.get("ss"),
                "add_time": s.get("add_time"),
                "raw":      json.dumps(s, ensure_ascii=False),
            })
    return rows


def write(conn, event_id, rows):
    for r in rows:
        conn.execute("""
            INSERT INTO odds_history
              (event_id,market,snapshot_id,home_od,away_od,
               over_od,under_od,handicap,ss,add_time,raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (event_id, r["market"], r["snap_id"],
              r["home_od"], r["away_od"],
              r["over_od"], r["under_od"],
              r["handicap"], r["ss"],
              r["add_time"], r["raw"]))

    done = 1 if rows else -1
    conn.execute("""
        UPDATE harvest_progress
        SET history_done=?, history_pts_count=?, updated_at=?
        WHERE event_id=?
    """, (done, len(rows), now_iso(), event_id))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0,
                    help="Секунд между запросами (default: 2.0 = 1800 req/hour)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN не задан"); sys.exit(1)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Все необработанные события (history_done = 0 или NULL)
    events = conn.execute("""
        SELECT re.event_id, re.home_team, re.away_team, re.league
        FROM raw_events re
        JOIN harvest_progress hp ON re.event_id = hp.event_id
        WHERE re.sport_tag = 'dota2'
          AND re.status = 'ended'
          AND hp.summary_done = 1
          AND (hp.history_done = 0 OR hp.history_done IS NULL)
        ORDER BY re.start_time DESC
    """).fetchall()

    total     = len(events)
    done_before = conn.execute(
        "SELECT COUNT(*) FROM harvest_progress WHERE history_done != 0 AND history_done IS NOT NULL"
    ).fetchone()[0]
    grand_total = total + done_before

    req_per_hour = 3600 / args.interval
    eta_h = total * args.interval / 3600

    print(f"\n{'='*60}")
    print(f"  Phase 5 Overnight Run")
    print(f"  DB:       {DB_PATH.name}")
    print(f"  Speed:    {args.interval}s/req = {req_per_hour:.0f} req/hour")
    print(f"  Осталось: {total:,} событий")
    print(f"  ETA:      {eta_h:.1f}h")
    if args.dry_run:
        print(f"  MODE:     DRY RUN")
    print(f"{'='*60}\n")

    if total == 0:
        print("✓ Все события уже обработаны!"); return

    session   = requests.Session()
    session.headers.update({"User-Agent": "Phase5Overnight/1.0"})
    last_req  = [0.0]
    inserted  = 0
    empty     = 0
    errors    = 0
    t_start   = time.time()

    for i, row in enumerate(events):
        eid   = row["event_id"]
        label = f"{row['home_team']} vs {row['away_team']}"

        try:
            data = fetch(session, eid, last_req, args.interval)
            rows = parse(data)

            n = len(rows)
            if n == 0:
                empty += 1
            else:
                inserted += n

            if not args.dry_run:
                write(conn, eid, rows)

            # Печатаем каждое событие (краткий вывод)
            snap_str = f"→ {n}" if n > 0 else "→ 0"
            print(f"  [{i+1}/{total}] {label}  {snap_str}", flush=True)

            # Прогресс-строка каждые 100 событий
            if (i + 1) % 100 == 0:
                elapsed   = time.time() - t_start
                speed     = (i + 1) / elapsed * 3600
                remain_h  = (total - i - 1) * args.interval / 3600
                done_now  = done_before + i + 1
                pct       = done_now / grand_total * 100
                print(f"\n  [{pct:.1f}%] {done_now:,}/{grand_total:,} | "
                      f"pts={inserted:,} empty={empty} err={errors} | "
                      f"speed={speed:.0f}/h | ETA={remain_h:.1f}h\n", flush=True)

        except KeyboardInterrupt:
            print("\n\n[!] Остановлено. Прогресс сохранён.")
            break

        except Exception as e:
            errors += 1
            print(f"  [ERR] {eid}: {e}", flush=True)
            if not args.dry_run:
                conn.execute(
                    "UPDATE harvest_progress SET history_done=-1, updated_at=? WHERE event_id=?",
                    (now_iso(), eid)
                )
                conn.commit()

    # Финал
    elapsed = time.time() - t_start
    done_total = conn.execute(
        "SELECT COUNT(*) FROM harvest_progress WHERE history_done=1"
    ).fetchone()[0]
    oh_rows = conn.execute("SELECT COUNT(*) FROM odds_history").fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  ГОТОВО")
    print(f"  Время:         {elapsed/3600:.2f}h")
    print(f"  Вставлено pts: {inserted:,}")
    print(f"  Пустых:        {empty}")
    print(f"  Ошибок:        {errors}")
    print(f"  history_done=1:{done_total:,}/{grand_total:,}")
    print(f"  odds_history:  {oh_rows:,} строк")
    print(f"{'='*60}\n")
    conn.close()


if __name__ == "__main__":
    main()
