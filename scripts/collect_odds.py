#!/usr/bin/env python3
"""
Odds daemon — непрерывный сбор коэффициентов.

Расписание:
  > 3 часов до матча  → сбор каждые 30 минут
  1-3 часа до матча   → сбор каждые 10 минут
  < 1 часа до матча   → сбор каждые 5 минут

Запуск:
    python3 scripts/collect_odds.py          # запустить демон
    python3 scripts/collect_odds.py --once   # один сбор и выйти
    python3 scripts/collect_odds.py --probe  # проверить источники и выйти
"""

import sys
import time
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Добавляем корень проекта в путь
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from adapters.odds_collector import collect_all

DB_PATH = PROJECT_ROOT / settings.database_path

# ── Интервалы сбора ───────────────────────────────────────────────────────────

INTERVAL_FAR    = 30 * 60   # > 3 часов → каждые 30 мин
INTERVAL_NEAR   = 10 * 60   # 1-3 часа  → каждые 10 мин
INTERVAL_CLOSE  =  5 * 60   # < 1 часа  → каждые 5 мин
CHECK_INTERVAL  =  1 * 60   # проверять расписание каждую минуту


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upcoming_matches(db_path: Path) -> list[dict]:
    """Возвращает список not_started матчей с begin_at."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, begin_at FROM matches "
        "WHERE status='not_started' AND begin_at IS NOT NULL "
        "ORDER BY begin_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _next_collect_interval(db_path: Path) -> int:
    """
    Смотрит на ближайший матч и возвращает нужный интервал сбора (секунды).
    """
    matches = _upcoming_matches(db_path)
    if not matches:
        return INTERVAL_FAR

    now = _now()
    for m in matches:
        try:
            start = datetime.fromisoformat(m["begin_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        delta = (start - now).total_seconds()
        if delta < 0:
            continue  # уже начался
        if delta < 3600:
            return INTERVAL_CLOSE
        if delta < 10800:
            return INTERVAL_NEAR
    return INTERVAL_FAR


def run_once(db_path: Path, verbose: bool = True) -> dict:
    """Один цикл сбора."""
    ts = _now().strftime("%Y-%m-%d %H:%M:%S UTC")
    if verbose:
        print(f"\n[{ts}] Collecting odds...")
    results = collect_all(db_path)
    total = sum(results.values())
    if verbose:
        for src, n in results.items():
            print(f"  {src}: {n} snapshots")
        print(f"  Total: {total} snapshots inserted")
    return results


def probe_sources() -> None:
    """Проверить все источники и показать что они возвращают."""
    import requests

    print("\n=== PROBE: The Odds API — доступные esports ===")
    try:
        # Шаг 1: найти все доступные sport keys
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": settings.odds_api_key},
            timeout=20,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        used      = r.headers.get("x-requests-used", "?")
        print(f"  Requests used/remaining: {used}/{remaining}")

        if r.status_code != 200:
            print(f"  Status {r.status_code}: {r.text[:200]}")
        else:
            sports = r.json()
            esports = [s for s in sports if "esport" in s.get("key","").lower()
                       or "dota" in s.get("key","").lower()
                       or "esport" in s.get("group","").lower()]
            if esports:
                print(f"  Esports sport keys available:")
                for s in esports:
                    print(f"    {s['key']:35} active={s.get('active')} title={s.get('title')}")
            else:
                print(f"  No esports keys found. All available groups:")
                groups = sorted(set(s.get("group","?") for s in sports))
                for g in groups:
                    print(f"    {g}")
                print(f"\n  Total sports: {len(sports)}")

    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n=== PROBE: The Odds API — попытка получить odds ===")
    # Пробуем все вероятные ключи для Dota 2
    dota_keys = [
        "esports_dota2", "dota2", "esports", "esports_lol",
    ]
    for key in dota_keys:
        try:
            r2 = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                params={"apiKey": settings.odds_api_key, "regions": "eu",
                        "markets": "h2h", "oddsFormat": "decimal"},
                timeout=10,
            )
            if r2.status_code == 200:
                data = r2.json()
                print(f"  ✓ {key}: {len(data)} events")
                for ev in data[:2]:
                    bms = ev.get("bookmakers", [])
                    print(f"    {ev.get('home_team')} vs {ev.get('away_team')} "
                          f"| start={ev.get('commence_time','?')[:16]} "
                          f"| books={[b['key'] for b in bms[:3]]}")
            else:
                print(f"  ✗ {key}: {r2.status_code} {r2.json().get('message','')}")
        except Exception as e:
            print(f"  ✗ {key}: {e}")

    print("\n=== PROBE: DotaScore API ===")
    try:
        from adapters.dotascore import DotaScoreClient
        client = DotaScoreClient()
        result = client.probe()
        for path, info in result.items():
            print(f"  {path}: {info}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n=== PROBE: DB stats ===")
    try:
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        sources = conn.execute(
            "SELECT source, bookmaker, COUNT(*) as n FROM odds_snapshots GROUP BY source, bookmaker"
        ).fetchall()
        conn.close()
        print(f"  Total snapshots in DB: {total}")
        for row in sources:
            print(f"  {row[0]} / {row[1]}: {row[2]} rows")
    except Exception as e:
        print(f"  DB error: {e}")


def run_daemon(db_path: Path) -> None:
    """
    Основной цикл демона.
    Собирает по расписанию и логирует каждый запуск.
    """
    print(f"[{_now():%Y-%m-%d %H:%M:%S}] Odds daemon started")
    print(f"  DB: {db_path}")
    print(f"  Schedule: >3h={INTERVAL_FAR//60}min | 1-3h={INTERVAL_NEAR//60}min | <1h={INTERVAL_CLOSE//60}min")
    print("  Press Ctrl+C to stop\n")

    last_collect  = datetime.min.replace(tzinfo=timezone.utc)
    last_interval = 0

    while True:
        try:
            interval = _next_collect_interval(db_path)
            now = _now()
            elapsed = (now - last_collect).total_seconds()

            if elapsed >= interval:
                if interval != last_interval:
                    print(f"[{now:%H:%M:%S}] Interval changed → {interval//60} min")
                    last_interval = interval

                run_once(db_path)
                last_collect = _now()
                next_run = last_collect + timedelta(seconds=interval)
                print(f"  Next run: {next_run:%H:%M:%S UTC}")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n[{_now():%Y-%m-%d %H:%M:%S}] Daemon stopped")
            break
        except Exception as e:
            print(f"[{_now():%H:%M:%S}] ERROR: {e} — retrying in 60s")
            time.sleep(60)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Odds collector daemon")
    parser.add_argument("--once",  action="store_true", help="Один сбор и выйти")
    parser.add_argument("--probe", action="store_true", help="Проверить источники")
    args = parser.parse_args()

    if args.probe:
        probe_sources()
    elif args.once:
        run_once(DB_PATH)
    else:
        run_daemon(DB_PATH)


if __name__ == "__main__":
    main()
