#!/usr/bin/env python3
"""
Терминал 3 — Финансовый отчёт Strategy Tournament
Сетлит ставки, обновляет банки, показывает отчёт за вчера / неделю.

Запуск:
    PYTHONPATH=. python3 scripts/daily_strategy_report.py --yesterday
    PYTHONPATH=. python3 scripts/daily_strategy_report.py --weekly
    PYTHONPATH=. python3 scripts/daily_strategy_report.py --loop        (обновление 60с)
    PYTHONPATH=. python3 scripts/daily_strategy_report.py --settle      (сетлим открытые)
"""
from __future__ import annotations
import argparse, os, sqlite3, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

from strategy_core import (
    STRATEGIES, open_tournament_db, ensure_bankrolls, get_bank, update_bank,
    now_iso, today_str, PREFERRED_BM, safe_float
)

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
SPORT_ID = 151   # Esports (Dota 2 лиги с префиксом "DOTA2 - ")
COOLDOWNS = [60, 120, 300, 600]

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


# ── API ───────────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict = {}) -> dict | None:
    for attempt, wait in enumerate([0] + COOLDOWNS):
        if wait:
            print(f"  [429] ждём {wait}с...", flush=True)
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
        except:
            return None
    return None


def _parse_score(ev: dict, event_id: str, debug: bool) -> str | None:
    score = ev.get("ss") or ev.get("score", "")
    if not score:
        if debug: print(f"    [debug {event_id}] ss пустой")
        return None
    score = str(score).strip().lower()
    # BetsAPI для esports иногда возвращает ss="home" или ss="away" напрямую
    if score in ("home", "away"):
        if debug: print(f"    [debug {event_id}] ss={score!r} (прямой результат)")
        return score
    if score == "0-0":
        if debug: print(f"    [debug {event_id}] ss=0-0")
        return None
    try:
        parts = score.split("-")
        sh, sa = int(parts[0].strip()), int(parts[1].strip())
        return "home" if sh > sa else "away"
    except:
        if debug: print(f"    [debug {event_id}] не смогли разобрать ss: {score!r}")
        return None


def get_event_result(event_id: str, debug: bool = False) -> str | None:
    # Способ 1: /v2/event/view
    data = api_get("/v2/event/view", {"event_id": event_id})
    if data:
        results = data.get("results")
        ev = (results[0] if isinstance(results, list) and results else
              results if isinstance(results, dict) else None)
        if ev:
            time_status = str(ev.get("time_status", ""))
            score = ev.get("ss") or ev.get("score", "")
            if debug: print(f"    [debug {event_id}] view: status={time_status} score={score!r}")
            if time_status in ("3", "6", "9"):  # 3=завершён 6=walkover 9=retired
                r = _parse_score(ev, event_id, debug)
                if r: return r

    # Способ 2: /v3/events/ended — ищем event_id в завершённых
    data2 = api_get("/v3/events/ended", {"sport_id": SPORT_ID})
    if data2:
        for ev in (data2.get("results") or []):
            if str(ev.get("id")) == str(event_id):
                score = ev.get("ss") or ""
                if debug: print(f"    [debug {event_id}] ended: score={score!r}")
                r = _parse_score(ev, event_id, debug)
                if r: return r

    if debug: print(f"    [debug {event_id}] результат не найден ни в одном endpoint")
    return None


def get_close_odds(event_id: str) -> float | None:
    data = api_get("/v2/event/odds/summary", {"event_id": event_id})
    if not data:
        return None
    for bm in PREFERRED_BM:
        bd = (data.get("results") or {}).get(bm)
        if not bd:
            continue
        od  = bd.get("odds") or {}
        end = od.get("end") or od.get("start") or {}
        h   = safe_float((end.get("151_1") or {}).get("home_od"))
        if h and h > 1:
            return h
    return None


# ── Settle ────────────────────────────────────────────────────────────────────

def settle_bets(conn, date: str | None = None, verbose: bool = True) -> int:
    now_ts = int(time.time())
    if date:
        rows = conn.execute("""
            SELECT * FROM strategy_daily_predictions
            WHERE prediction_date=? AND bet_status IN ('BET','PENDING')
        """, (date,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM strategy_daily_predictions
            WHERE bet_status IN ('BET','PENDING') AND start_time < ?
        """, (now_ts - 3600,)).fetchall()

    if not rows:
        if verbose:
            print("  Нет ставок для подведения итогов.")
        return 0

    by_event: dict = {}
    for row in rows:
        by_event.setdefault(row["event_id"], []).append(row)

    settled_n = 0
    for eid, bet_rows in by_event.items():
        result_hv = get_event_result(eid, debug=verbose)
        close_h   = get_close_odds(eid)

        for row in bet_rows:
            market   = row["market"] or "151_1"
            pick     = row["model_pick"]
            odds     = row["odds"]
            stake    = row["stake_usd"] or 0.0
            strategy = row["strategy_name"]
            mkt_prob = row["market_prob"]

            profit: float      = 0.0
            correct: bool|None = None
            new_status: str    = "VOID"
            result_label: str  = "?"

            if market in ("151_1", "151_2"):
                if result_hv is None:
                    if verbose:
                        print(f"  [{eid}] результат пока недоступен (матч ещё идёт?)")
                    continue
                correct      = (pick == result_hv)
                result_label = result_hv
            elif market == "151_3":
                correct      = None
                result_label = "нет данных карт"

            if correct is True:
                profit     = round(stake * (odds - 1), 2)
                new_status = "SETTLED_WIN"
            elif correct is False:
                profit     = -stake
                new_status = "SETTLED_LOSS"
            else:
                profit     = 0.0
                new_status = "VOID"

            clv = None
            if close_h and mkt_prob and market == "151_1":
                try:
                    clv = round(mkt_prob - 1.0 / close_h, 4)
                except:
                    pass

            conn.execute("""
                UPDATE strategy_daily_predictions
                SET bet_status=?, result=?, profit_usd=?,
                    close_odds=?, clv=?, settled_at=?
                WHERE id=?
            """, (new_status, result_label, profit, close_h, clv, now_iso(), row["id"]))

            if stake > 0:
                update_bank(conn, strategy, profit, stake,
                    True if correct is True else (False if correct is False else None))

            settled_n += 1
            if verbose:
                name = STRATEGY_RU.get(strategy, strategy)
                sym  = "✓ ПОБЕДА  " if new_status == "SETTLED_WIN" else \
                       ("✗ ПРОИГРЫШ" if new_status == "SETTLED_LOSS" else "○ АННУЛ.  ")
                p_s  = f"+${profit:.2f}" if profit > 0 else f"-${abs(profit):.2f}" if profit < 0 else "$0"
                c_s  = f"  CLV={clv:>+.4f}" if clv is not None else ""
                print(f"  {sym}  {name:<22}  "
                      f"{row['team_1'][:12]} vs {row['team_2'][:12]}  {p_s}{c_s}")

    conn.commit()
    return settled_n


# ── Дневной отчёт ─────────────────────────────────────────────────────────────

def print_daily_report(conn, report_date: str):
    os.system("clear")
    now = datetime.now().strftime("%d.%m.%Y  %H:%M:%S")

    print(f"\n{'═'*70}")
    print(f"  💼  ФИНАНСОВЫЙ ОТЧЁТ — {report_date}   [{now}]")
    print(f"{'═'*70}\n")

    rows = conn.execute("""
        SELECT sdp.strategy_name,
               COUNT(CASE WHEN sdp.bet_status NOT IN ('NO_BET') THEN 1 END)  AS bets,
               COUNT(CASE WHEN sdp.bet_status='SETTLED_WIN'  THEN 1 END)     AS wins,
               COUNT(CASE WHEN sdp.bet_status='SETTLED_LOSS' THEN 1 END)     AS losses,
               COUNT(CASE WHEN sdp.bet_status IN ('BET','PENDING') THEN 1 END) AS pending,
               COALESCE(SUM(sdp.stake_usd),  0) AS staked,
               COALESCE(SUM(sdp.profit_usd), 0) AS profit,
               AVG(CASE WHEN sdp.clv IS NOT NULL THEN sdp.clv END) AS avg_clv,
               COUNT(CASE WHEN sdp.clv > 0 THEN 1 END) AS clv_pos,
               COUNT(CASE WHEN sdp.clv IS NOT NULL THEN 1 END) AS clv_n,
               sb.current_bank_usd AS bank,
               sb.roi_pct
        FROM strategy_daily_predictions sdp
        JOIN strategy_bankrolls sb ON sb.strategy_name = sdp.strategy_name
        WHERE sdp.prediction_date = ?
        GROUP BY sdp.strategy_name
        ORDER BY COALESCE(SUM(sdp.profit_usd), 0) DESC
    """, (report_date,)).fetchall()

    if not rows:
        print(f"  Нет данных за {report_date}.")
        print(f"  Сначала запустите: PYTHONPATH=. python3 scripts/daily_strategy_run.py --today")
        return

    print(f"  РЕЗУЛЬТАТЫ ЗА {report_date}")
    print(f"  {'─'*68}")

    day_staked = day_profit = 0.0
    any_bets   = False

    for r in rows:
        staked  = r["staked"] or 0.0
        profit  = r["profit"] or 0.0
        name    = STRATEGY_RU.get(r["strategy_name"], r["strategy_name"])
        pending = r["pending"] or 0

        if staked == 0 and not pending:
            # Стратегия сегодня молчала
            print(f"  🔇 {name:<22}  не ставила                    банк ${r['bank']:>9.2f}")
            continue

        any_bets = True
        roi_day  = (profit / staked * 100) if staked > 0 else 0.0
        clv_s    = f"CLV={r['avg_clv']:>+.4f}" if r["avg_clv"] is not None else ""
        clvp_s   = f"CLV+:{r['clv_pos']}/{r['clv_n']}" if r["clv_n"] else ""

        if pending:
            sym = "⏳"
            pnl_s = f"ждём... ({pending} не закрыто)"
        elif profit > 0:
            sym   = "✅"
            pnl_s = f"+${profit:.2f}  (ROI {roi_day:>+.1f}%)"
        elif profit < 0:
            sym   = "❌"
            pnl_s = f"-${abs(profit):.2f}  (ROI {roi_day:>+.1f}%)"
        else:
            sym   = "〰"
            pnl_s = "$0.00"

        wr_s = f"{r['wins']}П/{r['losses']}П" if r["bets"] else "—"
        print(f"  {sym} {name:<22}  поставила ${staked:.2f}  {pnl_s:<28}  "
              f"банк ${r['bank']:>8.2f}  {wr_s}  {clv_s}  {clvp_s}")

        day_staked += staked
        day_profit += profit

    print(f"  {'─'*68}")
    if any_bets:
        day_roi = (day_profit / day_staked * 100) if day_staked > 0 else 0.0
        pnl_sym = "✅" if day_profit > 0 else ("❌" if day_profit < 0 else "〰")
        print(f"  {pnl_sym} ИТОГО  поставлено ${day_staked:.2f}  "
              f"{'+'if day_profit>=0 else ''}${day_profit:.2f}  ROI {day_roi:>+.1f}%")
    else:
        print(f"  Вчера не было ни одной ставки.")

    # ── Хайлайты ──────────────────────────────────────────────────────────────
    active = [r for r in rows if (r["staked"] or 0) > 0]
    if active:
        best  = max(active, key=lambda r: r["profit"] or 0)
        worst = min(active, key=lambda r: r["profit"] or 0)
        print(f"\n  🏆 Лучшая стратегия дня:  "
              f"{STRATEGY_RU.get(best['strategy_name'], best['strategy_name'])}  "
              f"{'+'if (best['profit'] or 0)>=0 else ''}${best['profit']:.2f}")
        if worst["strategy_name"] != best["strategy_name"]:
            print(f"  📉 Худшая стратегия дня:   "
                  f"{STRATEGY_RU.get(worst['strategy_name'], worst['strategy_name'])}  "
                  f"{'+'if (worst['profit'] or 0)>=0 else ''}${worst['profit']:.2f}")

        clv_rows = [r for r in active if r["avg_clv"] is not None]
        if clv_rows:
            best_clv = max(clv_rows, key=lambda r: r["avg_clv"])
            print(f"  📊 Лучший CLV:             "
                  f"{STRATEGY_RU.get(best_clv['strategy_name'], best_clv['strategy_name'])}  "
                  f"avgCLV={best_clv['avg_clv']:>+.4f}")

    # ── Серии проигрышей ──────────────────────────────────────────────────────
    streaks = []
    for s in STRATEGIES:
        last = conn.execute("""
            SELECT bet_status FROM strategy_daily_predictions
            WHERE strategy_name=? AND bet_status IN ('SETTLED_WIN','SETTLED_LOSS')
            ORDER BY created_at DESC LIMIT 10
        """, (s,)).fetchall()
        streak = 0
        for row in last:
            if row["bet_status"] == "SETTLED_LOSS":
                streak += 1
            else:
                break
        if streak >= 3:
            streaks.append((s, streak))

    if streaks:
        streaks.sort(key=lambda x: -x[1])
        print(f"\n  ⚠️  Серии проигрышей:")
        for s, n in streaks:
            print(f"    {STRATEGY_RU.get(s, s):<22}  {n} поражений подряд")

    # ── Текущие просадки ──────────────────────────────────────────────────────
    dd = conn.execute("""
        SELECT strategy_name, max_drawdown_usd, max_drawdown_pct
        FROM strategy_bankrolls WHERE max_drawdown_usd > 0
        ORDER BY max_drawdown_usd DESC LIMIT 3
    """).fetchall()
    if dd:
        print(f"\n  📉 Максимальные просадки:")
        for r in dd:
            print(f"    {STRATEGY_RU.get(r['strategy_name'], r['strategy_name']):<22}  "
                  f"-${r['max_drawdown_usd']:.2f} ({r['max_drawdown_pct']:.1f}%)")

    print(f"\n{'═'*70}")
    print(f"  Команды: --yesterday | --weekly | --settle | --loop")
    print(f"{'═'*70}")


# ── Недельный отчёт ───────────────────────────────────────────────────────────

def recommend(avg_clv, roi, max_dd_pct) -> str:
    clv = avg_clv or 0.0
    if clv > 0.005 and roi > 0:
        return "✅ Продолжать"
    elif clv > 0 or roi > -5:
        return "⚠️ Следить"
    else:
        return "🛑 Пауза"


def print_weekly_report(conn):
    os.system("clear")
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    until = today_str()
    now   = datetime.now().strftime("%d.%m.%Y  %H:%M:%S")

    print(f"\n{'═'*70}")
    print(f"  🏆  НЕДЕЛЬНЫЙ ТУРНИР СТРАТЕГИЙ")
    print(f"  Период: {since} → {until}   [{now}]")
    print(f"{'═'*70}\n")

    rows = conn.execute("""
        SELECT sdp.strategy_name,
               COUNT(CASE WHEN sdp.bet_status IN
                 ('SETTLED_WIN','SETTLED_LOSS','VOID') THEN 1 END)   AS bets,
               COUNT(CASE WHEN sdp.bet_status='SETTLED_WIN'  THEN 1 END) AS wins,
               COUNT(CASE WHEN sdp.bet_status='SETTLED_LOSS' THEN 1 END) AS losses,
               COALESCE(SUM(sdp.stake_usd),  0) AS staked,
               COALESCE(SUM(sdp.profit_usd), 0) AS profit_week,
               AVG(CASE WHEN sdp.clv IS NOT NULL THEN sdp.clv END)  AS avg_clv,
               COUNT(CASE WHEN sdp.clv > 0 THEN 1 END)              AS clv_pos,
               COUNT(CASE WHEN sdp.clv IS NOT NULL THEN 1 END)      AS clv_n,
               sb.current_bank_usd AS bank,
               sb.roi_pct          AS roi_total,
               sb.max_drawdown_pct AS max_dd_pct,
               sb.max_drawdown_usd AS max_dd_usd
        FROM strategy_daily_predictions sdp
        JOIN strategy_bankrolls sb ON sb.strategy_name = sdp.strategy_name
        WHERE sdp.prediction_date >= ?
        GROUP BY sdp.strategy_name
    """, (since,)).fetchall()

    if not rows:
        print("  Нет данных за последние 7 дней.")
        return

    def rank_key(r):
        clv  = r["avg_clv"] or -99
        clvp = (r["clv_pos"] / r["clv_n"]) if r["clv_n"] else 0
        roi  = (r["profit_week"] / r["staked"] * 100) if r["staked"] > 0 else 0
        dd   = -(r["max_dd_pct"] or 0)
        return (clv, clvp, roi, dd)

    ranked = sorted(rows, key=rank_key, reverse=True)
    medals = ["🥇", "🥈", "🥉"] + [f"  {i+4}." for i in range(len(STRATEGIES))]

    print(f"  РЕЙТИНГ (avgCLV → CLV+% → ROI → -просадка)\n")
    print(f"  {'':>4}  {'Стратегия':<22}  {'Банк':>9}  {'P&L нед':>9}  "
          f"{'ROI нед':>8}  {'ROI всё':>8}  {'Ставок':>7}  {'W/L':>5}  "
          f"{'AvgCLV':>8}  {'Просадка':>9}  Рекомендация")
    print(f"  {'─'*120}")

    for rank, r in enumerate(ranked):
        staked   = r["staked"]   or 0.0
        profit_w = r["profit_week"] or 0.0
        roi_wk   = (profit_w / staked * 100) if staked > 0 else 0.0
        roi_tot  = r["roi_total"] or 0.0
        wr       = f"{r['wins']}/{r['losses']}" if r["bets"] else "—"
        clv_s    = f"{r['avg_clv']:>+.4f}" if r["avg_clv"] is not None else "     —"
        dd_s     = f"-${r['max_dd_usd']:.0f} ({r['max_dd_pct']:.1f}%)" \
                   if r["max_dd_usd"] else "$0"
        rec      = recommend(r["avg_clv"], roi_tot, r["max_dd_pct"])
        med      = medals[rank]
        name     = STRATEGY_RU.get(r["strategy_name"], r["strategy_name"])
        pnl_s    = f"+${profit_w:.2f}" if profit_w >= 0 else f"-${abs(profit_w):.2f}"
        roi_wk_s = f"{roi_wk:>+.1f}%"
        roi_tt_s = f"{roi_tot:>+.1f}%"

        print(f"  {med:>4}  {name:<22}  ${r['bank']:>8.2f}  {pnl_s:>9}  "
              f"{roi_wk_s:>8}  {roi_tt_s:>8}  {r['bets']:>7}  {wr:>5}  "
              f"{clv_s:>8}  {dd_s:>9}  {rec}")

    print(f"  {'─'*120}\n")

    # ── Хайлайты недели ───────────────────────────────────────────────────────
    active = [r for r in ranked if (r["staked"] or 0) > 0]
    if active:
        best_pnl  = max(active, key=lambda r: r["profit_week"] or 0)
        worst_pnl = min(active, key=lambda r: r["profit_week"] or 0)
        best_clv  = max(active, key=lambda r: r["avg_clv"] or -99)
        best_roi  = max(active, key=lambda r:
                        (r["profit_week"] / r["staked"]) if r["staked"] else -99)
        max_dd    = max(active, key=lambda r: r["max_dd_pct"] or 0)

        print(f"  ИТОГИ НЕДЕЛИ")
        print(f"  {'─'*50}")

        # Лучший рынок
        best_mkt = conn.execute("""
            SELECT market, COUNT(*) as n, SUM(profit_usd) as pnl, AVG(clv) as clv
            FROM strategy_daily_predictions
            WHERE prediction_date >= ?
              AND bet_status IN ('SETTLED_WIN','SETTLED_LOSS') AND stake_usd > 0
            GROUP BY market ORDER BY COALESCE(AVG(clv),0) DESC LIMIT 1
        """, (since,)).fetchone()
        if best_mkt:
            mkt_name = {"151_1": "Победитель матча", "151_2": "Гандикап ±1.5",
                        "151_3": "Тотал карт 2.5"}.get(best_mkt["market"], best_mkt["market"])
            pnl_s = f"+${best_mkt['pnl']:.2f}" if best_mkt["pnl"] >= 0 \
                    else f"-${abs(best_mkt['pnl']):.2f}"
            print(f"  🎯 Лучший рынок:         {mkt_name}  "
                  f"({best_mkt['n']} ставок, P&L {pnl_s})")

        print(f"  🏆 Лучшая стратегия:     "
              f"{STRATEGY_RU.get(best_pnl['strategy_name'],'?')}  "
              f"P&L +${best_pnl['profit_week']:.2f}")
        print(f"  📉 Худшая стратегия:     "
              f"{STRATEGY_RU.get(worst_pnl['strategy_name'],'?')}  "
              f"P&L {worst_pnl['profit_week']:>+.2f}")
        print(f"  📊 Лучший CLV:           "
              f"{STRATEGY_RU.get(best_clv['strategy_name'],'?')}  "
              f"avgCLV={best_clv['avg_clv'] or 0:>+.4f}")
        print(f"  💰 Лучший ROI:           "
              f"{STRATEGY_RU.get(best_roi['strategy_name'],'?')}  "
              f"${best_roi['bank']:.2f}")
        print(f"  ⚠️  Макс просадка:        "
              f"{STRATEGY_RU.get(max_dd['strategy_name'],'?')}  "
              f"-${max_dd['max_dd_usd']:.0f} ({max_dd['max_dd_pct']:.1f}%)")

    # ── По дням ───────────────────────────────────────────────────────────────
    print(f"\n  {'─'*50}")
    print(f"  ПО ДНЯМ (последние 7 дней)\n")
    days = conn.execute("""
        SELECT prediction_date,
               COUNT(CASE WHEN bet_status NOT IN ('NO_BET') THEN 1 END) AS bets,
               COUNT(CASE WHEN bet_status='SETTLED_WIN'  THEN 1 END)    AS wins,
               COUNT(CASE WHEN bet_status='SETTLED_LOSS' THEN 1 END)    AS losses,
               COALESCE(SUM(profit_usd), 0) AS profit,
               AVG(CASE WHEN clv IS NOT NULL THEN clv END) AS avg_clv
        FROM strategy_daily_predictions
        WHERE prediction_date >= ?
          AND bet_status IN ('BET','PENDING','SETTLED_WIN','SETTLED_LOSS','VOID')
        GROUP BY prediction_date ORDER BY prediction_date DESC
    """, (since,)).fetchall()

    for d in days:
        clv_s  = f"CLV={d['avg_clv']:>+.4f}" if d["avg_clv"] is not None else ""
        pnl_s  = f"+${d['profit']:.2f}" if d["profit"] >= 0 else f"-${abs(d['profit']):.2f}"
        sym    = "✅" if d["profit"] > 0 else ("❌" if d["profit"] < 0 else "〰")
        print(f"  {sym}  {d['prediction_date']}  ставок={d['bets']:>2}  "
              f"побед={d['wins']:>2}  проигрышей={d['losses']:>2}  "
              f"P&L {pnl_s}  {clv_s}")

    next_w = (datetime.now() + timedelta(days=7)).strftime("%d.%m.%Y")
    print(f"\n  Следующий недельный отчёт: {next_w}")
    print(f"{'═'*70}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yesterday", action="store_true",
                    help="Подвести итоги вчерашнего дня")
    ap.add_argument("--weekly",    action="store_true",
                    help="Недельный рейтинг стратегий")
    ap.add_argument("--settle",    action="store_true",
                    help="Сетлим все открытые ставки прямо сейчас")
    ap.add_argument("--loop",      action="store_true",
                    help="Живой режим, обновление каждые N секунд")
    ap.add_argument("--once",      action="store_true",
                    help="Один прогон и выход")
    ap.add_argument("--interval",  type=int, default=60)
    args = ap.parse_args()

    conn = open_tournament_db()
    ensure_bankrolls(conn)

    if args.weekly:
        print_weekly_report(conn)
        return

    if args.yesterday:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"\n  Подводим итоги {yesterday}...\n")
        n = settle_bets(conn, date=yesterday, verbose=True)
        print(f"\n  Обработано: {n} ставок\n")
        print_daily_report(conn, yesterday)
        return

    if args.settle:
        print(f"\n  Сетлим все открытые ставки...\n")
        n = settle_bets(conn, verbose=True)
        print(f"\n  Обработано: {n} ставок")
        return

    # --loop или --once: живой дашборд
    date = today_str()

    def cycle():
        n = settle_bets(conn, verbose=False)
        if n:
            print(f"  [авто-сетл] закрыто {n} ставок", flush=True)
        print_daily_report(conn, date)

    cycle()
    if args.once or not args.loop:
        return

    while True:
        try:
            time.sleep(args.interval)
            cycle()
        except KeyboardInterrupt:
            print("\n  Остановлено.")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Остановлено.")
