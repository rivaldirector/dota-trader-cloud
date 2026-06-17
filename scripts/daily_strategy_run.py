#!/usr/bin/env python3
"""
Терминал 2 — Доска предиктов Strategy Tournament

Запуск:
    PYTHONPATH=. python3 scripts/daily_strategy_run.py --today
    PYTHONPATH=. python3 scripts/daily_strategy_run.py --today --refresh 300
    PYTHONPATH=. python3 scripts/daily_strategy_run.py --today --once
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

from strategy_core import (
    MatchData, STRATEGIES, run_strategy, apply_clv_top_edge,
    build_elo, build_h2h, best_match, novig, safe_float,
    open_tournament_db, ensure_bankrolls, get_bank,
    now_iso, today_str, PREFERRED_BM, START_ELO, elo_exp
)

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
SPORT_ID = 151
COOLDOWNS = [60, 120, 300, 600]

def is_dota_match(event: dict) -> bool:
    """BetsAPI называет Dota 2 лиги с префиксом 'DOTA2 - '"""
    league = (event.get("league", {}).get("name", "") or "")
    return league.upper().startswith("DOTA2")

STRATEGY_RU = {
    "Rule_C":                "Rule C (базовая)",
    "Rule_C_plus":           "Rule C+ (строгая)",
    "Rule_Elo150":           "Elo 150+",
    "Rule_H2H":              "H2H история",
    "Rule_Favorite_60_70":   "Фаворит 60-70%",
    "Rule_CLV_TopEdge":      "CLV топ-edge",
    "Rule_DreamLeague":      "DreamLeague",
    "Rule_EPL":              "EPL",
    "Rule_TotalMaps_Under":  "Тотал карт Меньше",
    "Rule_TotalMaps_Over":   "Тотал карт Больше",
    "Rule_Handicap_Favorite":"Гандикап фаворит",
    "Rule_MarketFavorite":   "Рыночный фаворит",
}

MARKET_RU = {
    "151_1": "Победитель матча",
    "151_2": "Гандикап ±1.5",
    "151_3": "Тотал карт 2.5",
}

PICK_RU = {
    "home":  "{home} победит",
    "away":  "{away} победит",
    "over":  "Больше 2.5 карт",
    "under": "Меньше 2.5 карт",
}


def ru_reason(code: str) -> str:
    STATIC = {
        "market_unavailable":           "нет котировок",
        "market_151_2_unavailable":     "нет рынка гандикап",
        "market_151_3_unavailable":     "нет рынка тотал",
        "total_maps_odds_missing":      "нет котировок тотал",
        "handicap_odds_missing":        "нет котировок гандикап",
        "handicap_novig_failed":        "ошибка гандикап",
        "handicap_mkt_below_65%":       "рынок гандикап < 65%",
        "no_h2h_data":                  "мало H2H (<3 матчей)",
        "wrong_league_not_epl":         "не EPL",
        "wrong_league_not_dreamleague": "не DreamLeague",
        "not_top_edge_today":           "не лучший edge дня",
        "edge_not_positive":            "edge отрицательный",
        "no_signal":                    "нет сигнала",
    }
    if code in STATIC:
        return STATIC[code]
    if code.startswith("edge=") and "<" in code:
        return f"edge слабый ({code})"
    if code.startswith("elo_diff=") and "<" in code:
        p = code.split("<")
        return f"Elo разница {p[0].split('=')[1]} < {p[1]}"
    if code.startswith("mkt=") and "outside" in code:
        try:
            val = float(code.split("=")[1].split("_")[0])
            return f"рынок {val:.1%} — вне зоны 60-70%"
        except:
            return "рынок вне зоны"
    if code.startswith("mkt=") and "<=" in code:
        try:
            val = float(code.split("=")[1].split("<=")[0])
            return f"рынок {val:.1%} — слишком мало"
        except:
            return "рынок слишком мал"
    if code.startswith("odds=") and ">=" in code:
        return "коэфф ≥ 2.0"
    if "mkt_under" in code:
        return "вер-ть меньше < 55%"
    if "mkt_over" in code:
        return "вер-ть больше < 55%"
    if code.startswith("model=") or "contradicts" in code:
        return "модель против рынка"
    if "h2h_wr=" in code and "below" in code:
        try:
            val = code.split("=")[1].split("_")[0]
            return f"H2H {val} < 60%"
        except:
            return "H2H слабый"
    return code


# ── API ───────────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict = {}) -> dict | None:
    for attempt, wait in enumerate([0] + COOLDOWNS):
        if wait:
            print(f"  [429] ждём {wait}с...", flush=True)
            time.sleep(wait)
        time.sleep(2.1)
        try:
            r = requests.get(f"{BASE}{path}", params={"token": TOKEN, **params}, timeout=15)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            d = r.json()
            return d if d.get("success") else None
        except Exception as e:
            print(f"  [API ошибка] {e}", flush=True)
            return None
    return None


def get_odds_summary(event_id: str):
    data = api_get("/v2/event/odds/summary", {"event_id": event_id})
    if not data:
        return None, None, None
    results = data.get("results") or {}
    o1 = o2 = o3 = None

    def _try_bm(bm, bd):
        nonlocal o1, o2, o3
        times = (bd.get("odds") or {}).get("end") or (bd.get("odds") or {}).get("start") or {}
        if not o1:
            m1 = times.get("151_1") or {}
            h, a = safe_float(m1.get("home_od")), safe_float(m1.get("away_od"))
            if h and a and h > 1 and a > 1:
                o1 = {"home_od": h, "away_od": a, "bookmaker": bm}
        if not o2:
            m2 = times.get("151_2") or {}
            h2, a2 = safe_float(m2.get("home_od")), safe_float(m2.get("away_od"))
            if h2 and a2 and h2 > 1 and a2 > 1:
                o2 = {"home_od": h2, "away_od": a2, "handicap": m2.get("handicap"), "bookmaker": bm}
        if not o3:
            m3 = times.get("151_3") or {}
            ov, un = safe_float(m3.get("over_od")), safe_float(m3.get("under_od"))
            if ov and un and ov > 1 and un > 1:
                o3 = {"over_od": ov, "under_od": un, "bookmaker": bm}

    # Сначала пробуем предпочтительных букмекеров
    for bm in PREFERRED_BM:
        bd = results.get(bm)
        if bd:
            _try_bm(bm, bd)
        if o1 and o2 and o3:
            break

    # Fallback: любой доступный букмекер если предпочтительные не дали линию
    if not o1:
        for bm, bd in results.items():
            if bm in PREFERRED_BM:
                continue
            _try_bm(bm, bd)
            if o1:
                break

    return o1, o2, o3


def build_match(event: dict, elo: dict, known: list, h2h_data: dict) -> MatchData:
    eid   = str(event.get("id"))
    home  = event.get("home", {}).get("name", "?")
    away  = event.get("away", {}).get("name", "?")
    league= event.get("league", {}).get("name", "")
    start = int(event.get("time", 0))
    hm = best_match(home, known)
    am = best_match(away, known)
    elo_h = elo.get(hm, START_ELO) if hm else START_ELO
    elo_a = elo.get(am, START_ELO) if am else START_ELO
    model_prob = elo_exp(elo_h, elo_a)
    h2h_lookup = {}
    if hm and am:
        key = tuple(sorted([hm, am]))
        raw = h2h_data.get(key, {})
        if raw:
            w1, w2 = raw.get("w1", 0), raw.get("w2", 0)
            h2h_lookup = {
                "total": raw.get("total", 0),
                "w_home": w1 if key[0] == hm else w2,
                "w_away": w2 if key[0] == hm else w1,
            }
    o1, o2, o3 = get_odds_summary(eid)
    return MatchData(
        event_id=eid, home=home, away=away, league=league, start_time=start,
        elo_h=elo_h, elo_a=elo_a, elo_diff=abs(elo_h - elo_a),
        model_prob=model_prob,
        odds_151_1=o1, odds_151_2=o2, odds_151_3=o3, h2h=h2h_lookup,
    )


def save_predictions(conn, flat, pred_date):
    for m, strategy, sr in flat:
        bank_now  = get_bank(conn, strategy)
        stake_usd = round(bank_now * 0.02, 2) if sr.bet else 0.0
        exp_profit= round(stake_usd * (sr.odds - 1), 2) if sr.bet and sr.odds else 0.0
        # Дата берётся из start_time матча (UTC), чтобы ночные матчи не попадали в "вчера"
        if m.start_time:
            match_date = datetime.fromtimestamp(int(m.start_time), tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            match_date = pred_date
        conn.execute("""
            INSERT OR IGNORE INTO strategy_daily_predictions
              (created_at, prediction_date, strategy_name, event_id,
               league, team_1, team_2, start_time, market,
               model_pick, odds, market_prob, model_prob, edge,
               confidence, reason_code, strategy_bank_before, stake_usd, stake_pct,
               expected_profit_usd, bet_status, no_bet_reason, raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now_iso(), match_date, strategy, m.event_id,
            m.league, m.home, m.away, m.start_time, sr.market,
            sr.pick, sr.odds,
            round(sr.mkt_prob, 4) if sr.mkt_prob else None,
            round(m.model_prob, 4),
            round(sr.edge, 4) if sr.edge else None,
            sr.confidence, sr.reason_code,
            bank_now, stake_usd, 0.02 if sr.bet else 0.0,
            exp_profit, sr.status(), sr.no_bet_reason,
            json.dumps({"home": m.home, "away": m.away,
                        "elo_h": round(m.elo_h, 1), "elo_a": round(m.elo_a, 1),
                        "league": m.league, "h2h": m.h2h}, ensure_ascii=False)
        ))
    conn.commit()


# ── Отображение ───────────────────────────────────────────────────────────────

W = 70  # ширина экрана

def section(title: str):
    pad = W - len(title) - 4
    print(f"\n  ┌─ {title} {'─'*max(0,pad)}┐")

def endsection():
    print(f"  └{'─'*(W-2)}┘")


def print_signals(matches: list, all_results: dict, conn):
    """
    ВЕРХНЯЯ ЧАСТЬ: только то, что реально ставим — по матчам.
    Группируем по (market, pick, odds).
    """
    # Считаем общий бюджет стратегий
    total_budget = sum(get_bank(conn, s) * 0.02 for s in STRATEGIES)

    print(f"\n{'═'*W}")
    print(f"  🎯  СИГНАЛЫ НА СЕГОДНЯ")
    print(f"{'═'*W}")

    grand_total_bets  = 0
    grand_total_stake = 0.0

    for m in matches:
        results = all_results.get(m.event_id, {})
        t_s = datetime.fromtimestamp(m.start_time, tz=timezone.utc).strftime("%H:%M") \
              if m.start_time else "?"

        # Группируем ставки по (market, pick)
        groups: dict[tuple, list] = defaultdict(list)
        for s in STRATEGIES:
            sr = results.get(s)
            if sr and sr.bet:
                key = (sr.market, sr.pick, sr.odds)
                bank  = get_bank(conn, s)
                stake = round(bank * 0.02, 2)
                groups[key].append((s, sr, stake))

        if not groups:
            continue  # матч без сигналов — не показываем в шапке

        print(f"\n  ╔{'═'*(W-4)}╗")
        print(f"  ║  [{t_s}]  {m.home} vs {m.away}")
        print(f"  ║  {m.league}")
        print(f"  ╠{'═'*(W-4)}╣")

        match_stake = 0.0
        for (market, pick, odds), strat_list in sorted(groups.items()):
            total_stake = sum(st for _, _, st in strat_list)
            match_stake += total_stake
            n_strats = len(strat_list)

            # Описание ставки
            market_name = MARKET_RU.get(market, market)
            pick_tmpl   = PICK_RU.get(pick, pick)
            pick_name   = pick_tmpl.format(home=m.home, away=m.away)

            # Букмекер
            if market == "151_1":
                bm = m.bm_151_1 or "?"
            elif market == "151_2":
                bm = (m.odds_151_2 or {}).get("bookmaker", "?")
            elif market == "151_3":
                bm = (m.odds_151_3 or {}).get("bookmaker", "?")
            else:
                bm = "?"

            print(f"  ║")
            print(f"  ║  📌 {market_name}  [{bm}]")
            print(f"  ║     Ставим на:  {pick_name}")
            print(f"  ║     Коэффициент: {odds:.2f}   "
                  f"Ставка: ${total_stake:.2f}   "
                  f"({n_strats} из 12 стратегий)")
            print(f"  ║     Потенциальный выигрыш: +${total_stake*(odds-1):.2f}")
            grand_total_bets  += 1
            grand_total_stake += total_stake

        print(f"  ║")
        print(f"  ║  Итого по матчу: ${match_stake:.2f}")
        print(f"  ╚{'═'*(W-4)}╝")

    if grand_total_bets == 0:
        print(f"\n  🔇  Сегодня нет сигналов — ни одна стратегия не ставит\n")
    else:
        print(f"\n{'═'*W}")
        print(f"  💰  ИТОГО СЕГОДНЯ: {grand_total_bets} ставок  |  ${grand_total_stake:.2f}")
        print(f"{'═'*W}")


def print_banks(conn, pred_date: str):
    """Банки стратегий — компактно."""
    print(f"\n  {'─'*W}")
    print(f"  БАНКИ СТРАТЕГИЙ  ({pred_date})")
    print(f"  {'─'*W}")
    rows = conn.execute("""
        SELECT strategy_name, current_bank_usd, roi_pct, bets_count, wins, losses
        FROM strategy_bankrolls ORDER BY current_bank_usd DESC
    """).fetchall()
    for r in rows:
        bank   = r["current_bank_usd"]
        budget = round(bank * 0.02, 2)
        roi_s  = f"{r['roi_pct']:>+.1f}%" if r["roi_pct"] else "+0.0%"
        name   = STRATEGY_RU.get(r["strategy_name"], r["strategy_name"])
        wr_s   = f"{r['wins']}П/{r['losses']}П" if r["bets_count"] else "нет ставок"
        bar_n  = int(min(bank / 1000.0, 1.5) * 10)
        bar    = "█" * bar_n + "░" * max(0, 10 - bar_n)
        print(f"  {name:<22}  ${bank:>8.2f}  [{bar}]  "
              f"бюджет ${budget:.2f}  ROI {roi_s}  {wr_s}")
    print(f"  {'─'*W}")


def print_tech_log(matches: list, all_results: dict, conn):
    """
    НИЖНЯЯ ЧАСТЬ: технический лог — почему каждая стратегия ставит или нет.
    """
    print(f"\n\n{'═'*W}")
    print(f"  📋  ТЕХНИЧЕСКИЙ ЛОГ")
    print(f"{'═'*W}")

    for m in matches:
        results = all_results.get(m.event_id, {})
        t_s = datetime.fromtimestamp(m.start_time, tz=timezone.utc).strftime("%H:%M") \
              if m.start_time else "?"

        print(f"\n  [{t_s}]  {m.home} vs {m.away}")
        mkt_s  = f"рынок={m.mkt_prob:.1%}" if m.mkt_prob else "нет котировок"
        edge_s = f"edge={m.edge:>+.1%}" if m.edge is not None else ""
        print(f"  Elo: Δ={m.elo_diff:.0f}  модель={m.model_prob:.1%}  {mkt_s}  {edge_s}")
        print(f"  {'─'*60}")

        for s in STRATEGIES:
            sr   = results.get(s)
            name = STRATEGY_RU.get(s, s)
            if sr is None:
                print(f"  ❓  {name:<22}  ошибка")
                continue
            bank  = get_bank(conn, s)
            stake = round(bank * 0.02, 2) if sr.bet else 0.0
            if sr.bet:
                pick_tmpl = PICK_RU.get(sr.pick, sr.pick or "?")
                pick_desc = pick_tmpl.format(home=m.home, away=m.away)
                edge_v = f"edge={sr.edge:>+.1%}" if sr.edge is not None else ""
                mkt_v  = f"рынок={sr.mkt_prob:.1%}" if sr.mkt_prob else ""
                print(f"  ✅  {name:<22}  ставим ${stake:.2f}  → {pick_desc} @{sr.odds:.2f}  "
                      f"{mkt_v}  {edge_v}")
            else:
                reason = ru_reason(sr.no_bet_reason or sr.reason_code or "")
                print(f"  ❌  {name:<22}  $0    {reason}")


# ── Основной цикл ─────────────────────────────────────────────────────────────

def fetch_all_pages(endpoint: str, params: dict = {}, max_pages: int = 10) -> list:
    """Листает все страницы endpoint и возвращает суммарный список results."""
    results = []
    for page in range(1, max_pages + 1):
        d = api_get(endpoint, {**params, "page": page})
        if not d:
            break
        page_results = d.get("results") or []
        if not page_results:
            break
        results.extend(page_results)
        # Если страница неполная — следующих нет
        pager = d.get("pager") or {}
        total = int(pager.get("total", 0) or 0)
        per_page = int(pager.get("per_page", 50) or 50)
        if len(results) >= total or len(page_results) < per_page:
            break
    return results


def run_once(conn, elo, known, h2h_data, pred_date):
    print(f"  Загружаем матчи (все страницы)...", flush=True)
    upcoming_raw = fetch_all_pages("/v3/events/upcoming", {"sport_id": SPORT_ID})
    inplay_raw   = fetch_all_pages("/v3/events/inplay",   {"sport_id": SPORT_ID})

    events_seen = set()
    events_raw  = []
    for e in upcoming_raw + inplay_raw:
        eid = str(e.get("id", ""))
        if eid and eid not in events_seen and is_dota_match(e):
            events_seen.add(eid)
            events_raw.append(e)

    if not events_raw:
        print("  [!] Нет матчей Dota 2 с линией")
        return None, None

    print(f"  Найдено матчей Dota 2: {len(events_raw)} (upcoming + live)\n")

    matches, all_results = [], {}
    for i, ev in enumerate(events_raw):
        home = ev.get("home", {}).get("name", "?")
        away = ev.get("away", {}).get("name", "?")
        print(f"  [{i+1}/{len(events_raw)}] {home} vs {away}...", end=" ", flush=True)
        m = build_match(ev, elo, known, h2h_data)
        if m.odds_h is None:
            # Debug: показать что вернул API для этого матча
            raw_data = api_get("/v2/event/odds/summary", {"event_id": m.event_id})
            bm_keys = list((raw_data.get("results") or {}).keys()) if raw_data else []
            print(f"нет линии — пропускаем [event_id={m.event_id}, букмекеры={bm_keys}]", flush=True)
            continue
        matches.append(m)
        results = {s: run_strategy(s, m) for s in STRATEGIES}
        all_results[m.event_id] = results
        n_bets = sum(1 for r in results.values() if r.bet)
        odds_s = f"{m.odds_h:.2f}/{m.odds_a:.2f}"
        print(f"{odds_s}  ставят {n_bets}/12", flush=True)

    flat = [(m, s, all_results[m.event_id][s]) for m in matches for s in STRATEGIES]
    flat = apply_clv_top_edge(flat)
    for m, s, sr in flat:
        all_results[m.event_id][s] = sr

    save_predictions(conn, flat, pred_date)
    return matches, all_results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today",   action="store_true", required=True)
    ap.add_argument("--refresh", type=int, default=0)
    ap.add_argument("--once",    action="store_true")
    args = ap.parse_args()

    if not TOKEN:
        print("ОШИБКА: BETSAPI_TOKEN не задан"); sys.exit(1)

    conn = open_tournament_db()
    ensure_bankrolls(conn)
    pred_date = today_str()

    print(f"\n{'═'*W}")
    print(f"  Терминал 2 — Strategy Tournament  |  {pred_date}")
    print(f"{'═'*W}")
    print(f"\n  Строим Elo модель...", end=" ", flush=True)
    elo, _ = build_elo()
    known = list(elo.keys())
    print(f"{len(known)} команд")
    print(f"  Строим H2H индекс...", end=" ", flush=True)
    h2h_data = build_h2h()
    print(f"{len(h2h_data)} пар")
    while True:
        try:
            matches, all_results = run_once(conn, elo, known, h2h_data, pred_date)
            if matches is not None:
                os.system("clear")
                # ── ШАПКА: только сигналы ──────────────────────────
                print_signals(matches, all_results, conn)
                print_banks(conn, pred_date)
                # ── ЛОГ: технические детали ────────────────────────
                print_tech_log(matches, all_results, conn)
        except KeyboardInterrupt:
            print("\n  Остановлено.")
            break
        except Exception as ex:
            import traceback
            print(f"\n  [Ошибка] {ex}", flush=True)
            traceback.print_exc()

        if args.once or args.refresh == 0:
            break

        print(f"\n  Обновление через {args.refresh}с... (Ctrl+C)\n", flush=True)
        try:
            time.sleep(args.refresh)
        except KeyboardInterrupt:
            print("\n  Остановлено.")
            break


if __name__ == "__main__":
    main()
