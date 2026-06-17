#!/usr/bin/env python3
"""
arb_scanner.py — Сканер арбитражных возможностей через BetsAPI.

Находит ситуации где сумма вероятностей < 100% у разных букмекеров
на один и тот же матч. Гарантированная прибыль без риска.

Режимы:
  --scan      Один прогон: найти текущие арбы и вывести
  --watch     Непрерывный мониторинг (раз в N минут)
  --history   Показать историю найденных арбов
  --calc      Калькулятор: задать кэфы вручную

Запуск:
  PYTHONPATH=. python3 scripts/arb_scanner.py --scan
  PYTHONPATH=. python3 scripts/arb_scanner.py --watch --interval 5
  PYTHONPATH=. python3 scripts/arb_scanner.py --calc --o1 2.10 --o2 2.15 --bank 1000
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

TOKEN = os.getenv("BETSAPI_TOKEN", "")
BASE  = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")

ARB_DB = ROOT / "data" / "arb_tracker.db"

# Спорты для сканирования (BetsAPI sport IDs)
# 151 = Dota 2, 161 = CS2, 176 = LoL, 182 = Valorant
SPORTS = {
    151: "Dota 2",
    161: "CS:GO/CS2",
    176: "LoL",
    182: "Valorant",
}

# Приоритет букмекеров для расчёта стейков
PREF_BM = [
    "Pinnacle", "PinnacleSports", "Bet365", "GGBet",
    "MelBet", "FonBet", "10Bet", "188Bet", "CashPoint",
]

MIN_PROFIT_PCT = 0.5   # минимальный арб % чтобы показывать


# ── DB ────────────────────────────────────────────────────────────────────────

def get_arb_conn() -> sqlite3.Connection:
    ARB_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ARB_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
    CREATE TABLE IF NOT EXISTS arb_opportunities (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        found_at    TEXT NOT NULL,
        sport       TEXT,
        event_id    TEXT,
        league      TEXT,
        team_home   TEXT,
        team_away   TEXT,
        begin_at    TEXT,
        bm_home     TEXT,
        bm_away     TEXT,
        odds_home   REAL,
        odds_away   REAL,
        arb_pct     REAL,
        stake_home  REAL,
        stake_away  REAL,
        bank        REAL DEFAULT 1000,
        profit      REAL,
        status      TEXT DEFAULT 'FOUND'
    )
    """)
    conn.commit()
    return conn


# ── API ────────────────────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> dict | None:
    params["token"] = TOKEN
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  API error {path}: {e}")
        return None


def fetch_upcoming(sport_id: int) -> list[dict]:
    """Получить предстоящие матчи с кэфами для данного спорта."""
    data = _get("/v3/events/upcoming", {"sport_id": sport_id, "per_page": 50})
    if not data or data.get("success") != 1:
        return []
    return data.get("results", [])


def fetch_odds(event_id: str) -> dict | None:
    """Получить все кэфы для матча."""
    data = _get("/v2/event/odds/summary", {"event_id": event_id})
    if not data or data.get("success") != 1:
        return None
    return data.get("results", {})


# ── ARB MATH ──────────────────────────────────────────────────────────────────

def find_arb(odds_by_bm: dict) -> dict | None:
    """
    Из словаря {bm: {home: X, away: Y}} найти лучший арб.
    Ищем: max(home) у любого бука + max(away) у любого бука.
    Если 1/max_home + 1/max_away < 1 — арб есть.
    """
    best_home = (None, 0.0)  # (bm, odds)
    best_away = (None, 0.0)

    for bm, o in odds_by_bm.items():
        h = o.get("home", 0)
        a = o.get("away", 0)
        if h > best_home[1]:
            best_home = (bm, h)
        if a > best_away[1]:
            best_away = (bm, a)

    bm_h, oh = best_home
    bm_a, oa = best_away

    if not bm_h or not bm_a or oh <= 1 or oa <= 1:
        return None

    implied = 1/oh + 1/oa
    if implied >= 1.0:
        return None

    profit_pct = (1 - implied) * 100
    return dict(
        bm_home=bm_h, odds_home=oh,
        bm_away=bm_a, odds_away=oa,
        arb_pct=profit_pct,
        implied=implied,
    )


def calc_stakes(odds_home: float, odds_away: float, bank: float) -> tuple[float, float, float]:
    """Оптимальное распределение банкролла для арба."""
    p_h = 1 / odds_home
    p_a = 1 / odds_away
    total_p = p_h + p_a
    s_h = round(bank * p_h / total_p, 2)
    s_a = round(bank - s_h, 2)
    profit = round(s_h * odds_home - bank, 2)
    return s_h, s_a, profit


def parse_odds_response(raw: dict, home_team: str, away_team: str) -> dict:
    """Парсим ответ BetsAPI odds/summary в {bm: {home, away}}."""
    result = {}
    odds_data = raw.get("odds", {})

    for bm_name, bm_data in odds_data.items():
        if not isinstance(bm_data, dict):
            continue
        # Берём start (opening) odds
        start = bm_data.get("start", {})
        if not start:
            continue
        try:
            home_o = float(start.get("home_od", 0) or 0)
            away_o = float(start.get("away_od", 0) or 0)
            if home_o > 1 and away_o > 1:
                result[bm_name] = {"home": home_o, "away": away_o}
        except Exception:
            continue

    return result


# ── SCAN ──────────────────────────────────────────────────────────────────────

def cmd_scan(bank: float = 1000.0, save: bool = True):
    conn = get_arb_conn()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    found_total = 0

    print(f"\n{'='*65}")
    print(f"ARB SCAN — {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Банк: {bank:.0f}$")
    print(f"{'='*65}")

    for sport_id, sport_name in SPORTS.items():
        print(f"\n[{sport_name}]")
        events = fetch_upcoming(sport_id)
        if not events:
            print("  Нет upcoming матчей.")
            continue

        print(f"  Матчей в ленте: {len(events)}")
        found_sport = 0

        for ev in events:
            eid     = str(ev.get("id", ""))
            league  = ev.get("league", {}).get("name", "?")
            home    = (ev.get("home") or {}).get("name", "?")
            away    = (ev.get("away") or {}).get("name", "?")
            begin   = ev.get("time", "")

            raw_odds = fetch_odds(eid)
            time.sleep(0.5)  # rate limit

            if not raw_odds:
                continue

            odds_by_bm = parse_odds_response(raw_odds, home, away)
            if len(odds_by_bm) < 2:
                continue

            arb = find_arb(odds_by_bm)
            if not arb or arb["arb_pct"] < MIN_PROFIT_PCT:
                continue

            s_h, s_a, profit = calc_stakes(arb["odds_home"], arb["odds_away"], bank)
            found_total += 1
            found_sport += 1

            begin_dt = ""
            if begin:
                try:
                    begin_dt = datetime.fromtimestamp(int(begin)).strftime("%m-%d %H:%M")
                except Exception:
                    begin_dt = str(begin)

            print(f"\n  {'★ АРБ НАЙДЕН! ':─<50}")
            print(f"  {home} vs {away}")
            print(f"  Лига:  {league}  |  Начало: {begin_dt}")
            print(f"  {arb['bm_home']:15} → {home[:20]:20} @ {arb['odds_home']:.3f}")
            print(f"  {arb['bm_away']:15} → {away[:20]:20} @ {arb['odds_away']:.3f}")
            print(f"  Арб: {arb['arb_pct']:.2f}%  |  Стейки: {s_h:.0f}$ / {s_a:.0f}$  |  Профит: +{profit:.0f}$")

            if save:
                conn.execute("""
                    INSERT OR IGNORE INTO arb_opportunities
                    (found_at, sport, event_id, league, team_home, team_away, begin_at,
                     bm_home, bm_away, odds_home, odds_away, arb_pct,
                     stake_home, stake_away, bank, profit)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (now_str, sport_name, eid, league, home, away,
                      begin_dt, arb["bm_home"], arb["bm_away"],
                      arb["odds_home"], arb["odds_away"], arb["arb_pct"],
                      s_h, s_a, bank, profit))
                conn.commit()

        if found_sport == 0:
            print(f"  Арбов не найдено (проверено {len(events)} матчей).")

    conn.close()
    print(f"\n{'='*65}")
    print(f"  Итого арбов найдено: {found_total}")
    if found_total == 0:
        print("  💡 Арбы редки — попробуй --watch для непрерывного мониторинга.")
    print()


# ── WATCH ─────────────────────────────────────────────────────────────────────

def cmd_watch(interval_min: int = 5, bank: float = 1000.0):
    print(f"Запускаем мониторинг каждые {interval_min} мин. Ctrl+C для остановки.\n")
    while True:
        cmd_scan(bank=bank, save=True)
        print(f"  Следующий скан через {interval_min} мин...")
        time.sleep(interval_min * 60)


# ── HISTORY ───────────────────────────────────────────────────────────────────

def cmd_history(limit: int = 50):
    conn = get_arb_conn()
    rows = conn.execute("""
        SELECT * FROM arb_opportunities ORDER BY found_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    print(f"\n{'='*65}")
    print(f"ИСТОРИЯ АРБОВ (последние {limit})")
    print(f"{'='*65}\n")

    if not rows:
        print("  Пока нет записей. Запусти --scan или --watch.\n")
        return

    total_profit = 0.0
    for r in rows:
        print(f"  {r['found_at'][:16]}  [{r['sport']}]  {r['team_home']} vs {r['team_away']}")
        print(f"    {r['bm_home']}@{r['odds_home']:.2f} / {r['bm_away']}@{r['odds_away']:.2f}")
        print(f"    Арб: {r['arb_pct']:.2f}%  |  Профит: +{r['profit']:.0f}$ на {r['bank']:.0f}$  [{r['status']}]")
        total_profit += r["profit"] or 0
        print()

    print(f"  Всего арбов: {len(rows)}  |  Суммарный потенциал: +{total_profit:.0f}$\n")


# ── CALC ──────────────────────────────────────────────────────────────────────

def cmd_calc(o1: float, o2: float, bank: float):
    implied = 1/o1 + 1/o2
    profit_pct = (1 - implied) * 100

    print(f"\n{'='*50}")
    print(f"АРБ КАЛЬКУЛЯТОР")
    print(f"{'='*50}")
    print(f"  Кэф 1 (BM1 → Home):  {o1:.3f}")
    print(f"  Кэф 2 (BM2 → Away):  {o2:.3f}")
    print(f"  Сумма вероятностей:   {implied*100:.2f}%")
    print()

    if profit_pct <= 0:
        print(f"  ❌ Арба нет. Сумма {implied*100:.1f}% > 100%.")
        print(f"     Нужно чтобы сумма была < 100%.")
    else:
        s1, s2, profit = calc_stakes(o1, o2, bank)
        print(f"  ✅ АРБ: {profit_pct:.2f}% гарантированного дохода")
        print()
        print(f"  Банкролл:    {bank:.0f}$")
        print(f"  Ставка BM1:  {s1:.2f}$  → если выиграет: {s1*o1:.2f}$")
        print(f"  Ставка BM2:  {s2:.2f}$  → если выиграет: {s2*o2:.2f}$")
        print(f"  Профит:      +{profit:.2f}$ при любом исходе")
    print()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Арбитраж сканер через BetsAPI")
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("--scan", help="Один прогон")
    p_scan.add_argument("--bank", type=float, default=1000.0)

    p_watch = sub.add_parser("--watch", help="Непрерывный мониторинг")
    p_watch.add_argument("--interval", type=int, default=5)
    p_watch.add_argument("--bank", type=float, default=1000.0)

    sub.add_parser("--history", help="История арбов")

    p_calc = sub.add_parser("--calc", help="Калькулятор")
    p_calc.add_argument("--o1", type=float, required=True)
    p_calc.add_argument("--o2", type=float, required=True)
    p_calc.add_argument("--bank", type=float, default=1000.0)

    # Поддержка: python3 arb_scanner.py --scan / --watch / --history / --calc
    args, unknown = parser.parse_known_args()

    raw = sys.argv[1] if len(sys.argv) > 1 else ""

    if raw == "--scan":
        bank = 1000.0
        for i, a in enumerate(sys.argv):
            if a == "--bank" and i+1 < len(sys.argv):
                bank = float(sys.argv[i+1])
        cmd_scan(bank=bank)

    elif raw == "--watch":
        interval = 5
        bank = 1000.0
        for i, a in enumerate(sys.argv):
            if a == "--interval" and i+1 < len(sys.argv):
                interval = int(sys.argv[i+1])
            if a == "--bank" and i+1 < len(sys.argv):
                bank = float(sys.argv[i+1])
        cmd_watch(interval_min=interval, bank=bank)

    elif raw == "--history":
        cmd_history()

    elif raw == "--calc":
        o1 = o2 = bank = None
        for i, a in enumerate(sys.argv):
            if a == "--o1" and i+1 < len(sys.argv):
                o1 = float(sys.argv[i+1])
            if a == "--o2" and i+1 < len(sys.argv):
                o2 = float(sys.argv[i+1])
            if a == "--bank" and i+1 < len(sys.argv):
                bank = float(sys.argv[i+1])
        if o1 and o2:
            cmd_calc(o1, o2, bank or 1000.0)
        else:
            print("Нужно: --calc --o1 2.10 --o2 2.15 --bank 1000")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
