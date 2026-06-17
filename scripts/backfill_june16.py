#!/usr/bin/env python3
"""
Ретроспективный прогон Tournament за 16 июня 2026.

Запуск:
    PYTHONPATH=. python3 scripts/backfill_june16.py
"""
from __future__ import annotations
import json, os, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

from strategy_core import (
    STRATEGIES, open_tournament_db, ensure_bankrolls, get_bank, update_bank,
    now_iso, today_str, PREFERRED_BM, safe_float,
    build_elo, build_h2h, MatchData, run_strategy, apply_clv_top_edge,
)

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
SPORT_ID = 151
BACKFILL_DATE = "2026-06-16"

TS_START = 1781568000  # 2026-06-16 00:00 UTC
TS_END   = 1781654399  # 2026-06-16 23:59 UTC

COOLDOWNS = [60, 120, 300]
W = 70


def api_get(path: str, params: dict = {}) -> dict | None:
    for wait in [0] + COOLDOWNS:
        if wait:
            print(f"  [429] ждём {wait}с...")
            time.sleep(wait)
        time.sleep(2.1)
        try:
            r = requests.get(f"{BASE}{path}",
                             params={"token": TOKEN, **params}, timeout=15)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            d = r.json()
            return d if d.get("success") else None
        except Exception as e:
            print(f"  [err] {e}")
            return None
    return None


def is_dota(event: dict) -> bool:
    name = (event.get("league", {}).get("name", "") or "")
    return name.upper().startswith("DOTA2")


def fetch_june16_events() -> list[dict]:
    """Тянем ended-матчи Dota2 за 16 июня — листаем страницы пока не пройдём дату."""
    events = []
    seen   = set()
    for page in range(1, 20):
        data = api_get("/v3/events/ended", {"sport_id": SPORT_ID, "page": page})
        if not data:
            print(f"  [ended] страница {page}: нет ответа")
            break
        results = data.get("results") or []
        if not results:
            print(f"  [ended] страница {page}: пусто")
            break

        page_times = []
        for ev in results:
            st  = int(ev.get("time", 0) or 0)
            eid = str(ev.get("id", ""))
            page_times.append(st)
            if TS_START <= st <= TS_END and is_dota(ev) and eid not in seen:
                seen.add(eid)
                events.append(ev)

        min_ts = min(page_times) if page_times else 0
        max_ts = max(page_times) if page_times else 0
        min_dt = datetime.fromtimestamp(min_ts, tz=timezone.utc).strftime("%m-%d %H:%M") if min_ts else "?"
        max_dt = datetime.fromtimestamp(max_ts, tz=timezone.utc).strftime("%m-%d %H:%M") if max_ts else "?"
        print(f"  [ended] стр.{page}: {len(results)} матчей [{min_dt} … {max_dt}]  Dota16: +{sum(1 for e in results if is_dota(e) and TS_START <= int(e.get('time',0) or 0) <= TS_END)}")

        # Если самые старые события на странице уже раньше 16 июня — дальше не нужно
        if min_ts < TS_START:
            break
        # Если самые свежие ещё позже 16 июня — продолжаем листать
    print(f"  Итого Dota2 за 16 июня: {len(events)}")
    return events


def get_result_for_event(event_id: str) -> str | None:
    """Пробуем /v2/event/view для получения счёта."""
    data = api_get("/v2/event/view", {"event_id": event_id})
    if not data:
        return None
    results = data.get("results")
    ev = (results[0] if isinstance(results, list) and results else
          results if isinstance(results, dict) else None)
    if not ev:
        return None
    score = ev.get("ss") or ev.get("score", "")
    if not score or score == "0-0":
        return None
    try:
        parts = str(score).split("-")
        sh, sa = int(parts[0].strip()), int(parts[1].strip())
        return "home" if sh > sa else "away"
    except:
        return None


def get_result_from_pandascore(home: str, away: str) -> str | None:
    """Ищем результат в dota_research.sqlite3 по именам команд (нечёткий матч)."""
    db_path = ROOT / "storage" / "dota_research.sqlite3"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT team_1_name, team_2_name, winner_name
            FROM matches
            WHERE begin_at >= '2026-06-16' AND begin_at < '2026-06-17'
              AND status = 'finished'
        """).fetchall()
        conn.close()
    except Exception as e:
        print(f"    [PS] ошибка БД: {e}")
        return None

    home_l = home.lower().strip()
    away_l = away.lower().strip()
    for r in rows:
        t1 = (r["team_1_name"] or "").lower().strip()
        t2 = (r["team_2_name"] or "").lower().strip()
        # Простое частичное совпадение
        if (home_l in t1 or t1 in home_l) and (away_l in t2 or t2 in away_l):
            winner = r["winner_name"] or ""
            t1_orig = r["team_1_name"] or ""
            return "home" if winner.lower() in t1_orig.lower() or t1_orig.lower() in winner.lower() else "away"
        if (away_l in t1 or t1 in away_l) and (home_l in t2 or t2 in home_l):
            winner = r["winner_name"] or ""
            t2_orig = r["team_2_name"] or ""
            # home в BetsAPI = away в PandaScore
            return "away" if winner.lower() in t2_orig.lower() or t2_orig.lower() in winner.lower() else "home"
    print(f"    [PS] не нашли {home} vs {away} за 16 июня в PandaScore")
    return None


def parse_result_from_event(ev: dict) -> str | None:
    score = str(ev.get("ss") or ev.get("score", "") or "").strip().lower()
    if not score or score == "0-0":
        return None
    # BetsAPI для esports возвращает ss="home"/"away" напрямую
    if score in ("home", "away"):
        return score
    try:
        parts = score.split("-")
        sh, sa = int(parts[0].strip()), int(parts[1].strip())
        return "home" if sh > sa else "away"
    except:
        return None


def get_odds(event_id: str) -> tuple[float | None, float | None, str | None]:
    data = api_get("/v2/event/odds/summary", {"event_id": event_id})
    if not data:
        return None, None, None
    bm_data = data.get("results") or {}
    for bm in PREFERRED_BM:
        bd = bm_data.get(bm)
        if not bd:
            continue
        od    = bd.get("odds") or {}
        start = od.get("start") or {}
        line  = start.get("151_1") or {}
        h = safe_float(line.get("home_od"))
        a = safe_float(line.get("away_od"))
        if h and a and h > 1.0 and a > 1.0:
            return h, a, bm
    return None, None, None


def build_match_from_api(ev: dict, elo: dict, h2h_data: dict,
                         odds_h: float | None = None,
                         odds_a: float | None = None,
                         bm: str | None = None) -> MatchData:
    import math
    START_ELO = 1000.0
    home   = (ev.get("home") or {}).get("name", "?")
    away   = (ev.get("away") or {}).get("name", "?")
    league = (ev.get("league") or {}).get("name", "?")
    eid    = str(ev.get("id", ""))
    st     = int(ev.get("time", 0) or 0)

    # elo — просто {team: float}
    elo_h    = elo.get(home, START_ELO)
    elo_a    = elo.get(away, START_ELO)
    elo_diff = elo_h - elo_a
    p_raw    = 1.0 / (1.0 + math.exp(-elo_diff / 400.0))
    model_prob = max(0.50, min(0.95, p_raw))

    # h2h — {(t1,t2): {"w1": int, "w2": int, "total": int}}
    key     = tuple(sorted([home, away]))
    h2h_raw = h2h_data.get(key, {})
    h2h_val = {}
    if isinstance(h2h_raw, dict) and h2h_raw.get("total", 0) > 0:
        w1 = h2h_raw.get("w1", 0)
        total = h2h_raw["total"]
        w_home = w1 if key[0] == home else (total - w1)
        h2h_val = {"w_home": w_home, "w_away": total - w_home, "total": total}

    # odds_151_1 dict как ожидает MatchData
    odds_151_1 = None
    if odds_h and odds_a:
        odds_151_1 = {"home_od": odds_h, "away_od": odds_a, "bookmaker": bm or "?"}

    return MatchData(
        event_id=eid, league=league,
        home=home, away=away,
        start_time=st,
        elo_h=elo_h, elo_a=elo_a, elo_diff=elo_diff,
        model_prob=model_prob,
        odds_151_1=odds_151_1,
        h2h=h2h_val,
    )


def print_report(conn: sqlite3.Connection):
    """Отчёт за 16 июня прямо здесь — не импортируем из report-скрипта."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT sdp.strategy_name,
               COUNT(CASE WHEN sdp.bet_status NOT IN ('NO_BET') THEN 1 END)  AS bets,
               COUNT(CASE WHEN sdp.bet_status='SETTLED_WIN'  THEN 1 END)     AS wins,
               COUNT(CASE WHEN sdp.bet_status='SETTLED_LOSS' THEN 1 END)     AS losses,
               COALESCE(SUM(sdp.stake_usd),  0) AS staked,
               COALESCE(SUM(sdp.profit_usd), 0) AS profit,
               sb.current_bank_usd AS bank
        FROM strategy_daily_predictions sdp
        JOIN strategy_bankrolls sb ON sb.strategy_name = sdp.strategy_name
        WHERE sdp.prediction_date = ?
        GROUP BY sdp.strategy_name
        ORDER BY COALESCE(SUM(sdp.profit_usd), 0) DESC
    """, (BACKFILL_DATE,)).fetchall()

    print(f"\n{'═'*W}")
    print(f"  ФИНАНСОВЫЙ ОТЧЁТ — {BACKFILL_DATE}  (ретроспектива)")
    print(f"{'═'*W}\n")

    NAMES = {
        "Rule_C": "Rule C", "Rule_C_plus": "Rule C+", "Rule_Elo150": "Elo 150+",
        "Rule_H2H": "H2H история", "Rule_Favorite_60_70": "Фаворит 60-70%",
        "Rule_CLV_TopEdge": "CLV топ-edge", "Rule_DreamLeague": "DreamLeague",
        "Rule_EPL": "EPL", "Rule_TotalMaps_Under": "Тотал Меньше",
        "Rule_TotalMaps_Over": "Тотал Больше", "Rule_Handicap_Favorite": "Гандикап",
        "Rule_MarketFavorite": "Рыночный фаворит",
    }

    day_staked = day_profit = 0.0
    for r in rows:
        name   = NAMES.get(r["strategy_name"], r["strategy_name"])
        staked = r["staked"] or 0.0
        profit = r["profit"] or 0.0
        if staked == 0:
            print(f"  🔇 {name:<22}  не ставила  банк ${r['bank']:>9.2f}")
            continue
        sym = "✅" if profit > 0 else ("❌" if profit < 0 else "〰")
        roi = profit / staked * 100
        wr  = f"{r['wins']}П/{r['losses']}П"
        print(f"  {sym} {name:<22}  ${staked:.2f}  {profit:>+8.2f}$  ROI{roi:>+6.1f}%  банк ${r['bank']:>8.2f}  {wr}")
        day_staked += staked
        day_profit += profit

    print(f"  {'─'*66}")
    if day_staked > 0:
        roi = day_profit / day_staked * 100
        sym = "✅" if day_profit > 0 else ("❌" if day_profit < 0 else "〰")
        print(f"  {sym} ИТОГО  ${day_staked:.2f}  {day_profit:>+8.2f}$  ROI{roi:>+6.1f}%")
    else:
        print("  Ни одной ставки.")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="Удалить старые записи за 16 июня и пересчитать")
    args = ap.parse_args()

    if not TOKEN:
        print("ОШИБКА: BETSAPI_TOKEN не задан"); sys.exit(1)

    conn = open_tournament_db()
    ensure_bankrolls(conn)

    print(f"\n{'═'*W}")
    print(f"  РЕТРОСПЕКТИВА 16 ИЮНЯ 2026")
    print(f"{'═'*W}\n")

    if args.reset:
        n = conn.execute(
            "DELETE FROM strategy_daily_predictions WHERE prediction_date=?",
            (BACKFILL_DATE,)
        ).rowcount
        conn.commit()
        print(f"  Удалено {n} старых записей за {BACKFILL_DATE}")

    # Уже есть данные?
    existing = conn.execute(
        "SELECT COUNT(*) FROM strategy_daily_predictions WHERE prediction_date=? AND bet_status != 'NO_BET'",
        (BACKFILL_DATE,)
    ).fetchone()[0]
    if existing > 0 and not args.reset:
        print(f"  Уже есть {existing} ставок за {BACKFILL_DATE} — показываю отчёт.")
        print_report(conn)
        return

    # Загружаем модель
    print("  Строим Elo + H2H...", end=" ", flush=True)
    elo, _ = build_elo()
    h2h_data = build_h2h()
    print(f"{len(elo)} команд, {len(h2h_data)} H2H пар")

    # Тянем матчи
    events = fetch_june16_events()
    if not events:
        print("\n  Матчи не найдены через ended endpoint.")
        print("  Попробуй завтра — BetsAPI иногда задерживает исторические данные.")
        return

    settled = 0
    for ev in events:
        eid    = str(ev.get("id", ""))
        home   = (ev.get("home") or {}).get("name", "?")
        away   = (ev.get("away") or {}).get("name", "?")
        league = (ev.get("league") or {}).get("name", "?")
        st     = int(ev.get("time", 0) or 0)
        dt     = datetime.fromtimestamp(st, tz=timezone.utc).strftime("%H:%M UTC")

        print(f"\n  ▶ [{dt}] {home} vs {away}  [{league}]")

        # Результат: 1) из события, 2) BetsAPI view, 3) PandaScore DB
        result = parse_result_from_event(ev)
        if result is None:
            result = get_result_for_event(eid)
        if result is None:
            result = get_result_from_pandascore(home, away)
        print(f"    result={result}  event_id={eid}")

        # Коэффициенты
        odds_h, odds_a, bm = get_odds(eid)
        if odds_h is None:
            print(f"    нет коэффициентов — пропускаем")
            continue
        print(f"    odds: {odds_h}/{odds_a}  [{bm}]")

        # Матч-объект (с коэффициентами)
        m = build_match_from_api(ev, elo, h2h_data, odds_h=odds_h, odds_a=odds_a, bm=bm)

        # Стратегии
        strategy_results = [(s, run_strategy(s, m)) for s in STRATEGIES]

        for strategy_name, sr in strategy_results:
            bank_before = get_bank(conn, strategy_name)
            stake_usd   = round(bank_before * 0.02, 2) if sr.bet else 0.0
            exp_profit  = round(stake_usd * (sr.odds - 1), 2) if sr.bet and sr.odds else 0.0

            if sr.bet and result is not None:
                correct    = (sr.pick == result)
                profit     = round(stake_usd * (sr.odds - 1), 2) if correct else -stake_usd
                bet_status = "SETTLED_WIN" if correct else "SETTLED_LOSS"
            elif sr.bet:
                profit     = 0.0
                bet_status = "VOID"
            else:
                profit     = 0.0
                bet_status = "NO_BET"

            conn.execute("""
                INSERT OR IGNORE INTO strategy_daily_predictions
                  (created_at, prediction_date, strategy_name, event_id,
                   league, team_1, team_2, start_time, market,
                   model_pick, odds, market_prob, model_prob, edge,
                   confidence, reason_code, strategy_bank_before, stake_usd, stake_pct,
                   expected_profit_usd, bet_status, no_bet_reason, result, profit_usd,
                   settled_at, raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                now_iso(), BACKFILL_DATE, strategy_name, eid,
                league, home, away, st, sr.market,
                sr.pick, sr.odds,
                round(sr.mkt_prob, 4) if sr.mkt_prob else None,
                round(m.model_prob, 4),
                round(sr.edge, 4) if sr.edge else None,
                sr.confidence, sr.reason_code,
                bank_before, stake_usd, 0.02 if sr.bet else 0.0,
                exp_profit, bet_status, sr.no_bet_reason,
                result, profit if sr.bet else None,
                now_iso() if sr.bet else None,
                json.dumps({"home": home, "away": away, "league": league,
                            "odds_h": odds_h, "odds_a": odds_a, "bm": bm})
            ))

            if sr.bet and bet_status in ("SETTLED_WIN", "SETTLED_LOSS"):
                update_bank(conn, strategy_name, profit, stake_usd,
                            bet_status == "SETTLED_WIN")
                settled += 1
                sym = "✅" if bet_status == "SETTLED_WIN" else "❌"
                print(f"    {sym} {strategy_name}: {sr.pick} @ {sr.odds} → {profit:+.2f}$")

    conn.commit()
    print(f"\n  Записано и сеттлено: {settled} ставок\n")
    print_report(conn)


if __name__ == "__main__":
    main()
