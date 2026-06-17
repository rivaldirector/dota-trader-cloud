#!/usr/bin/env python3
"""
Live API Explorer — смотрим что доступно из live/upcoming данных BetsAPI.
Никакой записи в БД, просто печатает в консоль.

Terminal 2:  python3 scripts/live_explorer.py --mode inplay
Terminal 3:  python3 scripts/live_explorer.py --mode upcoming

Режимы:
  inplay    — текущие live матчи Dota 2 + коэффициенты
  upcoming  — ближайшие матчи + pre-match коэффициенты
  event ID  — детальный дамп одного матча (--event 12345678)
"""
import argparse, json, os, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
TOKEN = os.getenv("BETSAPI_TOKEN", "")
BASE  = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")

SPORT_ID = 151  # E-sports (Dota 2 = лига внутри)
REQ_GAP  = 3.0  # нет спешки, live explorer не в гонке за лимитом


def get(path, params={}):
    time.sleep(REQ_GAP)
    r = requests.get(f"{BASE}{path}", params={"token": TOKEN, **params}, timeout=15)
    r.raise_for_status()
    d = r.json()
    return d if d.get("success") else None


def ts(unix):
    if not unix:
        return "—"
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime("%d.%m %H:%M")


def print_odds(odds_results: dict):
    """Печатает коэффициенты из /v2/event/odds/summary."""
    if not odds_results:
        print("    odds: нет данных")
        return
    for bm, bm_data in list(odds_results.items())[:3]:  # топ-3 букмекера
        od = bm_data.get("odds", {}) or {}
        end = od.get("end") or od.get("start") or {}
        m1 = end.get("151_1") or {}
        if m1:
            h = m1.get("home_od", "—")
            a = m1.get("away_od", "—")
            print(f"    [{bm}] МЛ: home={h}  away={a}")


def mode_inplay():
    print(f"\n{'='*60}")
    print(f"  LIVE (in-play) Dota 2 — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    data = get("/v3/events/inplay", {"sport_id": SPORT_ID})
    if not data:
        print("  Нет данных или ошибка API"); return

    events = data.get("results", [])
    dota = [e for e in events if "dota" in e.get("league", {}).get("name", "").lower()]
    print(f"  E-sports live: {len(events)} total | Dota 2: {len(dota)}\n")

    for e in dota:
        eid   = e.get("id")
        home  = e.get("home", {}).get("name", "?")
        away  = e.get("away", {}).get("name", "?")
        score = e.get("ss", "")
        timer = e.get("timer", {})
        league = e.get("league", {}).get("name", "")
        print(f"  [{eid}] {home} vs {away}")
        print(f"    Лига: {league}")
        print(f"    Счёт: {score or 'нет'}  |  Время: {timer}")

        # Текущие коэффициенты
        odds_data = get("/v2/event/odds/summary", {"event_id": eid})
        if odds_data:
            print_odds(odds_data.get("results", {}))

            # Сколько снапшотов в истории?
            hist = get("/v2/event/odds", {"event_id": eid, "since_time": "0"})
            if hist:
                results = hist.get("results", {})
                odds = results.get("odds", {}) if isinstance(results, dict) else {}
                total_snaps = sum(len(v) for v in odds.values() if isinstance(v, list))
                markets = list(odds.keys())
                print(f"    История: {total_snaps} снапшотов | рынки: {markets}")
        print()


def mode_upcoming():
    print(f"\n{'='*60}")
    print(f"  UPCOMING Dota 2 — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    data = get("/v3/events/upcoming", {"sport_id": SPORT_ID})
    if not data:
        print("  Нет данных или ошибка API"); return

    events = data.get("results", [])
    dota = [e for e in events if "dota" in e.get("league", {}).get("name", "").lower()]
    print(f"  E-sports upcoming: {len(events)} total | Dota 2: {len(dota)}\n")

    for e in dota[:10]:  # первые 10
        eid    = e.get("id")
        home   = e.get("home", {}).get("name", "?")
        away   = e.get("away", {}).get("name", "?")
        league = e.get("league", {}).get("name", "")
        start  = ts(e.get("time"))
        print(f"  [{eid}] {home} vs {away}  | старт: {start}")
        print(f"    Лига: {league}")

        odds_data = get("/v2/event/odds/summary", {"event_id": eid})
        if odds_data:
            print_odds(odds_data.get("results", {}))
        print()


def mode_event(event_id: str):
    print(f"\n{'='*60}")
    print(f"  Детальный дамп event_id={event_id}")
    print(f"{'='*60}\n")

    # odds/summary
    print("--- /v2/event/odds/summary ---")
    d = get("/v2/event/odds/summary", {"event_id": event_id})
    print(json.dumps(d, indent=2, ensure_ascii=False)[:3000] if d else "нет данных")

    print("\n--- /v2/event/odds (история, топ-3 снапшота) ---")
    d2 = get("/v2/event/odds", {"event_id": event_id, "since_time": "0"})
    if d2:
        results = d2.get("results", {})
        odds = results.get("odds", {}) if isinstance(results, dict) else {}
        for mk, snaps in odds.items():
            if isinstance(snaps, list):
                print(f"\n  Рынок {mk} ({len(snaps)} снапшотов):")
                for s in snaps[:3]:
                    print(f"    {s}")
    else:
        print("нет данных")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",   choices=["inplay", "upcoming"], default="inplay")
    parser.add_argument("--event",  help="event_id для детального дампа")
    parser.add_argument("--loop",   action="store_true", help="Повторять каждые N минут")
    parser.add_argument("--every",  type=int, default=5, help="Интервал в минутах (default: 5)")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: BETSAPI_TOKEN не задан в .env"); return

    if args.event:
        mode_event(args.event); return

    fn = mode_inplay if args.mode == "inplay" else mode_upcoming

    fn()
    if args.loop:
        while True:
            print(f"  [пауза {args.every} мин... Ctrl+C для выхода]\n")
            time.sleep(args.every * 60)
            fn()


if __name__ == "__main__":
    main()
