#!/usr/bin/env python3
"""
Debug: реальная структура ответа /v2/event/odds (Phase 5 history endpoint).
Запускать ПЕРЕД перекачкой Phase 5, чтобы убедиться что парсер работает.

Usage:
    python3 scripts/debug_phase5_response.py              # один матч (последний)
    python3 scripts/debug_phase5_response.py --id 12345   # конкретный event_id
    python3 scripts/debug_phase5_response.py --n 3        # 3 разных матча
"""
import os, json, sqlite3, sys, time, argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("BETSAPI_TOKEN", "")
BASE  = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
DB    = ROOT / "storage" / "betsapi_harvest.db"


def fetch_odds_history(event_id: str) -> dict | None:
    url = f"{BASE}/v2/event/odds"
    r = requests.get(url, params={"token": TOKEN, "event_id": event_id, "since_time": "0"}, timeout=20)
    print(f"  HTTP {r.status_code}  ({len(r.content)} bytes)")
    if r.status_code == 429:
        print("  [429] Rate limit — подожди и попробуй ещё раз")
        return None
    if r.status_code != 200:
        print(f"  Error: {r.text[:300]}")
        return None
    return r.json()


def describe_value(v, depth=0, max_depth=4, indent="  "):
    """Рекурсивно описывает структуру значения."""
    pad = indent * depth
    if isinstance(v, dict):
        print(f"{pad}dict({len(v)} keys): {list(v.keys())[:12]}")
        if depth < max_depth:
            for k, sv in list(v.items())[:5]:
                print(f"{pad}  [{repr(k)}]:")
                describe_value(sv, depth+2, max_depth, indent)
    elif isinstance(v, list):
        print(f"{pad}list(len={len(v)})")
        if v and depth < max_depth:
            print(f"{pad}  [0]:")
            describe_value(v[0], depth+2, max_depth, indent)
    elif isinstance(v, str):
        print(f"{pad}str: {repr(v[:80])}")
    else:
        print(f"{pad}{type(v).__name__}: {v}")


def analyse_response(data: dict, event_id: str):
    """Полный анализ ответа — находит все рыночные данные."""
    print(f"\n{'─'*65}")
    print(f"EVENT {event_id}")
    print(f"  success = {data.get('success')}")

    results = data.get("results", {})
    print(f"\n  СТРУКТУРА results ({type(results).__name__}):")
    describe_value(results, depth=1, max_depth=5)

    # Попытка найти market коды (151_1 / 151_2 / 151_3)
    found_markets = {}

    def find_markets(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                new_path = f"{path}.{k}" if path else k
                if str(k) in ('151_1', '151_2', '151_3'):
                    found_markets[new_path] = v
                find_markets(v, new_path)
        elif isinstance(node, list):
            for i, item in enumerate(node[:3]):
                find_markets(item, f"{path}[{i}]")

    find_markets(results)

    if found_markets:
        print(f"\n  НАЙДЕННЫЕ РЫНОЧНЫЕ КОДЫ 151_x:")
        for path, val in found_markets.items():
            print(f"    {path} → {json.dumps(val, ensure_ascii=False)[:200]}")
    else:
        print(f"\n  [!] РЫНОЧНЫЕ КОДЫ 151_1/151_2/151_3 НЕ НАЙДЕНЫ в results")
        print(f"      Возможно структура отличается от /v2/event/odds/summary")

    # Проверить что возвращает наш текущий парсер
    rows_current = try_current_parser(data, event_id)
    print(f"\n  Текущий _parse_history() → {rows_current} строк")
    if rows_current == 0:
        print("  [!] БАГ ПОДТВЕРЖДЁН — парсер возвращает 0")

    # Попробовать альтернативные структуры
    rows_alt = try_alt_parsers(data, event_id)
    for name, n in rows_alt.items():
        print(f"  Альтернативный parser [{name}] → {n} строк")

    print(f"\n  RAW RESPONSE (первые 3000 chars):")
    print(f"  {json.dumps(data, ensure_ascii=False)[:3000]}")


def try_current_parser(data: dict, event_id: str) -> int:
    """Тест текущего (сломанного) парсера."""
    rows = 0
    results = data.get("results", {})
    for market_key, market_data in results.items():
        if not isinstance(market_data, dict):
            continue
        odds_per_bm = market_data.get("odds", {})
        if not isinstance(odds_per_bm, dict):
            continue
        for bm_name, points in odds_per_bm.items():
            if not isinstance(points, list):
                continue
            for pt in points:
                oh = pt.get("home_od") or pt.get("home") or pt.get("1")
                if oh:
                    rows += 1
    return rows


def _safe_float(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def try_alt_parsers(data: dict, event_id: str) -> dict:
    """Тестирует несколько возможных структур."""
    counts = {}

    results = data.get("results", {})

    # Alt A: results = { bookmaker: { odds: { market: [{...}] } } }
    # (похоже на /v2/event/odds/summary но с историческими точками как список)
    n = 0
    if isinstance(results, dict):
        for bm_name, bm_data in results.items():
            if not isinstance(bm_data, dict):
                continue
            odds = bm_data.get("odds", {}) or {}
            for market_key, mkt_val in odds.items():
                if isinstance(mkt_val, list):
                    for pt in mkt_val:
                        if isinstance(pt, dict) and (pt.get("home_od") or pt.get("over_od")):
                            n += 1
    counts["A: bm→odds→market→list"] = n

    # Alt B: results = { market: { bookmaker: [{point}, ...] } }
    n = 0
    if isinstance(results, dict):
        for market_key, mkt_data in results.items():
            if not isinstance(mkt_data, dict):
                continue
            for bm_name, points in mkt_data.items():
                if isinstance(points, list):
                    for pt in points:
                        if isinstance(pt, dict) and (pt.get("home_od") or pt.get("over_od")):
                            n += 1
    counts["B: market→bm→list"] = n

    # Alt C: results = { bookmaker: { market: [{point}] } }
    n = 0
    if isinstance(results, dict):
        for bm_name, bm_data in results.items():
            if not isinstance(bm_data, dict):
                continue
            for market_key, points in bm_data.items():
                if isinstance(points, list):
                    for pt in points:
                        if isinstance(pt, dict) and (pt.get("home_od") or pt.get("over_od")):
                            n += 1
    counts["C: bm→market→list"] = n

    # Alt D: results is a list of historical snapshots
    n = 0
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and (item.get("home_od") or item.get("over_od")):
                n += 1
    counts["D: list of snapshots"] = n

    # Alt E: results = { "odds": { market: { bm: [{point}] } } }
    n = 0
    if isinstance(results, dict):
        odds_top = results.get("odds", {}) or {}
        if isinstance(odds_top, dict):
            for market_key, mkt_data in odds_top.items():
                if isinstance(mkt_data, dict):
                    for bm_name, points in mkt_data.items():
                        if isinstance(points, list):
                            for pt in points:
                                if isinstance(pt, dict) and (pt.get("home_od") or pt.get("over_od")):
                                    n += 1
    counts["E: odds→market→bm→list"] = n

    # Alt F: same as E but snapshots at timestamps
    n = 0
    if isinstance(results, dict):
        for ts_key, ts_val in results.items():
            if not ts_key.isdigit():
                continue
            if isinstance(ts_val, dict):
                for bm, bm_data in ts_val.items():
                    if isinstance(bm_data, dict) and (bm_data.get("home_od") or bm_data.get("over_od")):
                        n += 1
    counts["F: timestamp→bm→odds"] = n

    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id",   default=None, help="Конкретный event_id")
    parser.add_argument("--n",    type=int, default=1, help="Сколько матчей проверить")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN not set in .env"); sys.exit(1)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if args.id:
        event_ids = [args.id]
    else:
        # Берём последние N матчей с summary_done=1
        rows = conn.execute("""
            SELECT re.event_id, re.home_team, re.away_team
            FROM raw_events re
            JOIN harvest_progress hp ON re.event_id = hp.event_id
            WHERE re.sport_tag='dota2' AND hp.summary_done=1
              AND (hp.history_done IS NULL OR hp.history_done=0)
            ORDER BY re.start_time DESC
            LIMIT ?
        """, (args.n,)).fetchall()
        if not rows:
            print("Нет матчей для проверки"); sys.exit(1)
        event_ids = [(r['event_id'], r['home_team'], r['away_team']) for r in rows]

    conn.close()

    print(f"BetsAPI Phase 5 Debug — {args.n} matche(s)")
    print(f"Endpoint: {BASE}/v2/event/odds")
    print()

    for i, item in enumerate(event_ids):
        if isinstance(item, tuple):
            eid, home, away = item
        else:
            eid, home, away = item, "?", "?"

        print(f"[{i+1}/{len(event_ids)}] {home} vs {away}  id={eid}")
        data = fetch_odds_history(eid)
        if data is None:
            continue

        analyse_response(data, eid)

        if i < len(event_ids) - 1:
            print("\nПауза 7 сек...")
            time.sleep(7)

    print(f"\n{'='*65}")
    print("Используй вывод выше чтобы определить правильную структуру,")
    print("затем обнови _parse_history() в betsapi_harvest.py.")


if __name__ == "__main__":
    main()
