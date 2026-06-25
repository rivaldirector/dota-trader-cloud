#!/usr/bin/env python3
"""
elo_auto_bet.py — АВТОНОМНАЯ машина решений v2.

Архитектура принятия решений (5 слоёв):
  1. СИГНАЛ       — Elo + форма (last 10) + H2H (last 8) → composite_prob
  2. EDGE FILTER  — ставим ТОЛЬКО если real_edge >= edge_min для тира лиги
                    (T1: 2%, T2: 3%, T3/qual: 5%)
                    Без реальных odds от BetsAPI — пропускаем матч
  3. KELLY        — stake = bankroll × fractional_kelly × adaptive_mult
                    Fraction=0.25 (conservative), cap по тиру
  4. ПОРТФЕЛЬ     — max 5% банка на матч, max 20% банка в сутки
  5. ADAPTIVE     — rolling ROI за 20 бетов → множитель Kelly (0.15–1.0)
                    ЗАМЕНА жёсткого стоп-лосса: никогда не останавливает
                    полностью, только плавно снижает размер

Источники данных:
  Расписание: https://dota.haglund.dev/v1/matches  (Liquipedia, бесплатно)
  История:    betsapi_events + elo_pandascore_history + elo_own_history
  Odds:       BetsAPI /v3/events/upcoming + /v2/event/odds/summary
  Тиры лиг:  Supabase league_tiers
  Банк:       Supabase bankroll_state (обновляется после каждого settle)

Run:
    python3 scripts/elo_auto_bet.py

GitHub Actions: paper_trader.yml — каждые 10 минут.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from math import pow
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

# Импортируем модуль сигналов
sys.path.insert(0, str(ROOT / "scripts"))
from totals_model import (
    find_od_team, get_od_team_stats, combine_team_stats,
    prob_over, prob_under, parse_totals_from_odds,
    GLOBAL_AVG_KILLS, GLOBAL_AVG_DUR_MIN,
)
from signals import (
    compute_form,
    compute_h2h,
    compute_fatigue,
    fatigue_adjustment,
    composite_prob,
    kelly_stake,
    adaptive_kelly_mult,
    get_league_tier,
    get_tier_params,
    KELLY_FRACTION_DEFAULT,
)

SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_ANON_KEY", "")
BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

MATCHES_URL        = "https://dota.haglund.dev/v1/matches"
HOURS_AHEAD        = 72
START_ELO          = 1500.0
K_FACTOR           = 32
FUZZY_MIN          = 0.72
ODDS_FUZZY_MIN     = 0.60
AVG_OVERROUND_HIST = 1.0585
STRATEGY_NAME      = "AUTO_ELO_FLAT"
DIVISION           = "FREE"
PREFERRED_BM       = ["PinnacleSports", "Pinnacle", "Bet365", "GGBet", "MelBet", "1xBet"]

# Минимальный банк для ставки (защита от случайного обнуления)
MIN_BANKROLL       = 100.0
# Дневной лимит — не более 20% банка за сутки
DAILY_STAKE_CAP    = 0.20


# ── BetsAPI ──────────────────────────────────────────────────────────────────

def _bapi(path, params=None):
    if not BETSAPI_TOKEN:
        return {}
    import time
    url = f"https://api.betsapi.com{path}"
    p = {"token": BETSAPI_TOKEN, **(params or {})}
    for attempt in range(3):
        try:
            r = requests.get(url, params=p, timeout=12)
            if r.status_code == 429:
                time.sleep(6); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                print(f"    [BetsAPI err] {path}: {e}"); return {}
        time.sleep(1.2)
    return {}


_cached_upcoming = None


def _fetch_upcoming():
    global _cached_upcoming
    if _cached_upcoming is not None:
        return _cached_upcoming
    import time
    if not BETSAPI_TOKEN:
        print("  [BetsAPI] токен не задан — пропускаем поиск коэффов")
        _cached_upcoming = []
        return []
    events, page = [], 1
    while page <= 3:
        data = _bapi("/v3/events/upcoming", {"sport_id": 151, "page": page})
        res  = data.get("results", [])
        if not res: break
        events.extend(res)
        time.sleep(1.2)
        if len(res) < 50: break
        page += 1
    print(f"  [BetsAPI] sport_id=151 → {len(events)} событий")
    if events:
        sample = [(e.get("home",{}).get("name"), e.get("away",{}).get("name")) for e in events[:3]]
        print(f"  [BetsAPI] первые 3: {sample}")
    _cached_upcoming = events
    return events


def _extract_real_odds(odds_data, bet_side):
    results = odds_data.get("results", {})
    if not isinstance(results, dict): return None, None
    candidates = []
    for bm_name, bm_data in results.items():
        if not isinstance(bm_data, dict): continue
        for _m, mdata in bm_data.items():
            if not isinstance(mdata, dict): continue
            olist = mdata.get("odds", [])
            if not isinstance(olist, list) or len(olist) < 2: continue
            try:
                def _f(x): return float(x["odds"] if isinstance(x, dict) else x)
                o_home, o_away = _f(olist[0]), _f(olist[1])
            except Exception: continue
            if o_home <= 1.0 or o_away <= 1.0: continue
            our = o_home if bet_side == "home" else o_away
            prio = next((i for i, p in enumerate(PREFERRED_BM)
                         if p.lower() in bm_name.lower()), len(PREFERRED_BM))
            candidates.append((prio, our, bm_name))
    if not candidates: return None, None
    _, best_odds, best_bm = sorted(candidates)[0]
    return round(best_odds, 4), best_bm


def _find_betsapi_event(home: str, away: str, start_ts: int):
    """Находит лучшее совпадение в BetsAPI upcoming событиях.
    Возвращает (event_id, is_reversed) или (None, False)."""
    import time
    from difflib import SequenceMatcher
    events = _fetch_upcoming()
    if not events:
        return None, False
    home_c = re.sub(r"\s+", " ", home.strip().lower())
    away_c = re.sub(r"\s+", " ", away.strip().lower())
    best_ev, best_score, best_rev = None, 0.0, False
    for ev in events:
        ev_ts = int(ev.get("time", 0))
        if abs(ev_ts - start_ts) > 8 * 3600:
            continue
        h = re.sub(r"\s+", " ", (ev.get("home", {}).get("name", "")).strip().lower())
        a = re.sub(r"\s+", " ", (ev.get("away", {}).get("name", "")).strip().lower())
        s_norm = SequenceMatcher(None, home_c, h).ratio() + SequenceMatcher(None, away_c, a).ratio()
        s_rev  = SequenceMatcher(None, home_c, a).ratio() + SequenceMatcher(None, away_c, h).ratio()
        score, rev = (s_norm, False) if s_norm >= s_rev else (s_rev, True)
        if score > best_score and score >= ODDS_FUZZY_MIN * 2:
            best_score, best_ev, best_rev = score, ev, rev
    if best_ev is None:
        return None, False
    return best_ev.get("id"), best_rev


def _get_event_odds_data(event_id: str) -> dict:
    import time
    time.sleep(1.2)
    return _bapi("/v2/event/odds/summary", {"event_id": event_id})


def lookup_real_odds(home, away, start_ts, bet_side):
    ev_id, is_rev = _find_betsapi_event(home, away, start_ts)
    if ev_id is None:
        return None, None
    odds_data = _get_event_odds_data(ev_id)
    eff_side = ("away" if bet_side == "home" else "home") if is_rev else bet_side
    return _extract_real_odds(odds_data, eff_side)


# Минимальный edge для totals ставок (выше чем moneyline — модель слабее)
TOTALS_EDGE_MIN_DATA   = 0.06   # есть статистика хотя бы одной команды
TOTALS_EDGE_MIN_GLOBAL = 0.10   # только глобальные средние (слабый сигнал)
KELLY_CAP_TOTALS       = 0.04   # max 4% банка на totals ставку


def try_totals_bet(
    home: str, away: str, start_ts: int, match_data: dict,
    tiers: list, bankroll: float, a_mult: float,
    daily_cap_usd: float, today_staked: float, eid: str,
) -> dict | None:
    """
    Пробует найти выгодную totals ставку (убийства / длительность / карты)
    для матча без Elo-данных.
    Возвращает строку для elo_paper_bets или None.
    """
    # 1. Ищем событие в BetsAPI
    ev_id, _rev = _find_betsapi_event(home, away, start_ts)
    if ev_id is None:
        print(f"    [тоталы] {home} vs {away} — не найдено в BetsAPI")
        return None

    odds_data = _get_event_odds_data(ev_id)
    totals = parse_totals_from_odds(odds_data)
    if not totals:
        print(f"    [тоталы] {home} vs {away} — нет тотальных рынков в BetsAPI")
        return None

    print(f"    [тоталы] {home} vs {away} — найдено {len(totals)} тотальных рынков")

    # 2. Получаем статистику команд из OpenDota
    h_id, h_sc = find_od_team(home)
    a_id, a_sc = find_od_team(away)
    h_stats = get_od_team_stats(h_id) if h_id else None
    a_stats = get_od_team_stats(a_id) if a_id else None

    if h_stats:
        print(f"    [тоталы] {home}: avg_kills={h_stats['avg_kills']} avg_dur={h_stats.get('avg_dur')} (n={h_stats['n']})")
    if a_stats:
        print(f"    [тоталы] {away}: avg_kills={a_stats['avg_kills']} avg_dur={a_stats.get('avg_dur')} (n={a_stats['n']})")

    model = combine_team_stats(h_stats, a_stats)
    using_global = model["using_global"]

    edge_min = TOTALS_EDGE_MIN_GLOBAL if using_global else TOTALS_EDGE_MIN_DATA
    tier = get_league_tier(match_data.get("leagueName"), tiers)

    # 3. Ищем рынок с позитивным edge
    best_row = None
    best_edge = -999.0

    for t in totals:
        mtype = t["market_type"]
        line  = t["line"]

        if mtype == "kills":
            mu, sigma = model["exp_kills"], model["std_kills"]
        elif mtype == "duration":
            mu, sigma = model["exp_dur"], model["std_dur"]
        elif mtype == "maps":
            # Для maps у нас нет модели без Elo — пропускаем
            continue
        else:
            continue

        p_ov = prob_over(line, mu, sigma)
        p_un = prob_under(line, mu, sigma)

        edge_over  = round(p_ov * t["over_odds"]  - 1, 4)
        edge_under = round(p_un * t["under_odds"] - 1, 4)

        print(
            f"    [тоталы] {mtype} {line}: "
            f"μ={mu:.1f} σ={sigma:.1f} | "
            f"over={t['over_odds']} edge={edge_over:+.1%} | "
            f"under={t['under_odds']} edge={edge_under:+.1%}"
        )

        for direction, edge, odds, p in [
            ("over",  edge_over,  t["over_odds"],  p_ov),
            ("under", edge_under, t["under_odds"], p_un),
        ]:
            if edge >= edge_min and edge > best_edge:
                best_edge = edge
                best_row = {
                    "market_type": mtype,
                    "line":        line,
                    "direction":   direction,
                    "edge":        edge,
                    "real_odds":   odds,
                    "bookmaker":   t["bookmaker"],
                    "p":           p,
                }

    if best_row is None:
        print(f"    [тоталы] нет рынка с edge ≥ {edge_min:.0%}")
        return None

    # 4. Kelly sizing (консервативный cap для totals)
    p     = best_row["p"]
    odds  = best_row["real_odds"]
    b     = odds - 1.0
    full_k = (b * p - (1 - p)) / b if b > 0 else 0
    kf     = round(min(full_k * KELLY_FRACTION_DEFAULT * a_mult, KELLY_CAP_TOTALS), 5)
    stake  = round(bankroll * kf, 1)

    if stake < 1.0:
        print(f"    [тоталы] Kelly stake слишком мал: ${stake:.1f}")
        return None

    if today_staked + stake > daily_cap_usd:
        print(f"    [тоталы] дневной лимит исчерпан")
        return None

    mtype     = best_row["market_type"]
    direction = best_row["direction"]
    line      = best_row["line"]

    print(
        f"\n  ✓ [ТОТАЛ] {home} vs {away}  ({match_data.get('leagueName')})\n"
        f"    {mtype} {direction.upper()} {line} | "
        f"μ={model['exp_kills'] if mtype == 'kills' else model['exp_dur']:.1f} "
        f"({'глобал' if using_global else 'стат'})\n"
        f"    odds={odds} edge={best_row['edge']:+.1%} kelly={kf:.4f} stake=${stake:.0f}"
    )

    return {
        "run_ts":         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy_name":  STRATEGY_NAME,
        "event_id":       eid,
        "division":       DIVISION,
        "league":         match_data.get("leagueName"),
        "home_team":      home,
        "away_team":      away,
        "start_time":     start_ts,
        "bookmaker":      best_row["bookmaker"],
        "bet_team":       direction,          # "over" / "under"
        "bet_market":     mtype,              # "kills" / "duration"
        "bet_line":       line,
        "odds":           round(1.0 / (p * AVG_OVERROUND_HIST), 3),
        "market_prob":    round(1.0 / odds, 4),
        "model_prob":     round(p, 4),
        "composite_prob": round(p, 4),
        "edge":           best_row["edge"],
        "stake_usd":      stake,
        "settled":        False,
        "real_odds":      odds,
        "real_bookmaker": best_row["bookmaker"],
        "form_score":     None,
        "h2h_score":      None,
        "kelly_f":        kf,
        "league_tier":    tier,
    }


# ── Supabase ─────────────────────────────────────────────────────────────────

def sb_get(table: str, qs: str) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers=SB_HEADERS,
        json=rows,
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] upsert {table}: {r.status_code} {r.text[:200]}")


def sb_patch(table: str, qs: str, body: dict) -> None:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers=SB_HEADERS,
        json=body,
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] patch {table}: {r.status_code} {r.text[:200]}")


# ── Team helpers ──────────────────────────────────────────────────────────────

def normalize_team(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def clean_team_name(name: str | None) -> str:
    if not name:
        return "?"
    return name.split(" (page does not exist)")[0].strip()


def fetch_team_aliases() -> dict[str, str]:
    try:
        rows = sb_get("team_aliases", "select=alias_name,canonical_name")
    except Exception as ex:
        print(f"  [WARN] team_aliases: {ex}")
        return {}
    return {
        normalize_team(r["alias_name"]): r["canonical_name"]
        for r in rows if r.get("alias_name") and r.get("canonical_name")
    }


def resolve_alias(name: str | None, alias_map: dict[str, str]) -> str | None:
    if not name:
        return name
    key = normalize_team(clean_team_name(name))
    return alias_map.get(key, name)


def fuzzy(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_elo_match(name: str, elo: dict[str, float]) -> tuple[str | None, float]:
    best, score = None, 0.0
    for team in elo:
        s = fuzzy(name, team)
        if s > score:
            best, score = team, s
    return (best, score) if score >= FUZZY_MIN else (None, score)


# ── Elo build ─────────────────────────────────────────────────────────────────

def elo_exp(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def build_elo_from_supabase() -> tuple[dict[str, float], list[tuple]]:
    """
    Строит Elo и возвращает (elo_dict, history).
    history — список (start_time, home, away, act_h) для signals.py.
    """
    print("Тяну историю матчей для Elo + сигналов...")
    page = 1000
    history: list[tuple[int, str, str, float]] = []
    seen_keys: set[tuple[str, str, int]] = set()

    # 1) BetsAPI
    rows, offset = [], 0
    while True:
        chunk = sb_get(
            "betsapi_events",
            f"sport_tag=eq.dota2&status=eq.ended&winner=neq.&"
            f"select=home_team,away_team,winner,start_time&"
            f"order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk: break
        rows.extend(chunk)
        if len(chunk) < page: break
        offset += page

    for r in rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None: continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys: continue
        seen_keys.add(key)
        history.append((int(st), t1, t2, 1.0 if w == t1 else 0.0))
    n_betsapi = len(history)

    # 2) PandaScore
    ps_rows, offset = [], 0
    while True:
        chunk = sb_get(
            "elo_pandascore_history",
            f"winner=neq.&select=home_team,away_team,winner,start_time&"
            f"order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk: break
        ps_rows.extend(chunk)
        if len(chunk) < page: break
        offset += page

    ps_added = 0
    for r in ps_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None: continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys: continue
        nw = normalize_team(w)
        if nw == normalize_team(t1): act_h = 1.0
        elif nw == normalize_team(t2): act_h = 0.0
        else: continue
        seen_keys.add(key)
        history.append((int(st), t1, t2, act_h))
        ps_added += 1

    # 3) Своя история
    own_rows, offset = [], 0
    while True:
        chunk = sb_get(
            "elo_own_history",
            f"winner=neq.&select=home_team,away_team,winner,start_time&"
            f"order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk: break
        own_rows.extend(chunk)
        if len(chunk) < page: break
        offset += page

    own_added = 0
    for r in own_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None: continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys: continue
        nw = normalize_team(w)
        if nw == normalize_team(t1): act_h = 1.0
        elif nw == normalize_team(t2): act_h = 0.0
        else: continue
        seen_keys.add(key)
        history.append((int(st), t1, t2, act_h))
        own_added += 1

    history.sort(key=lambda r: r[0])

    elo: dict[str, float] = {}
    for st, t1, t2, act_h in history:
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        ea = elo_exp(e1, e2)
        elo[t1] = e1 + K_FACTOR * (act_h - ea)
        elo[t2] = e2 + K_FACTOR * ((1 - act_h) - (1 - ea))

    print(f"  матчей: {len(history)} (BetsAPI:{n_betsapi} +PS:{ps_added} +own:{own_added}) | команд: {len(elo)}")
    return elo, history


def fetch_upcoming_matches() -> list[dict]:
    r = requests.get(MATCHES_URL, timeout=20)
    r.raise_for_status()
    return r.json()


def is_real_team(name: str | None) -> bool:
    return bool(name) and name != "TBD"


# ── Bankroll ──────────────────────────────────────────────────────────────────

def get_bankroll() -> float:
    """Читает текущий банк из bankroll_state."""
    try:
        rows = sb_get("bankroll_state", f"strategy=eq.{STRATEGY_NAME}&select=balance")
        if rows:
            return max(float(rows[0]["balance"]), MIN_BANKROLL)
    except Exception as e:
        print(f"  [WARN] bankroll_state: {e}")
    return 1000.0


def update_bankroll(new_balance: float, total_bets: int) -> None:
    sb_patch(
        "bankroll_state",
        f"strategy=eq.{STRATEGY_NAME}",
        {"balance": round(new_balance, 2), "total_bets": total_bets,
         "updated_at": datetime.now(timezone.utc).isoformat()},
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    try:
        matches = fetch_upcoming_matches()
    except Exception as ex:
        print(f"ERROR: dota.haglund.dev недоступен: {ex}")
        sys.exit(1)

    # ── Загружаем вспомогательные данные ────────────────────────────────────
    tiers = []
    try:
        tiers = sb_get("league_tiers", "select=pattern,tier,edge_min,kelly_cap&order=tier.asc")
        print(f"Тиров лиг: {len(tiers)}")
    except Exception as e:
        print(f"  [WARN] league_tiers: {e}")

    bankroll = get_bankroll()
    print(f"Текущий банк: ${bankroll:,.2f}")

    # Загружаем последние settled ставки для adaptive Kelly
    recent_settled = []
    try:
        recent_settled = sb_get(
            "elo_paper_bets",
            f"strategy_name=eq.{STRATEGY_NAME}&settled=eq.true"
            f"&select=stake_usd,pnl,outcome,real_odds&order=settled_ts.desc&limit=30",
        )
    except Exception as e:
        print(f"  [WARN] recent_settled: {e}")

    a_mult = adaptive_kelly_mult(recent_settled)
    print(f"Adaptive Kelly множитель: {a_mult:.2f} (по {len(recent_settled)} settled бетам)")

    # Загружаем калиброванные веса ансамбля из model_config
    ensemble_weights = {"elo": 0.60, "form": 0.25, "h2h": 0.15}
    try:
        cfg = sb_get("model_config", "key=in.(w_elo,w_form,w_h2h)&select=key,value")
        if cfg:
            cfg_map = {r["key"]: float(r["value"]) for r in cfg}
            ensemble_weights = {
                "elo":  cfg_map.get("w_elo",  0.60),
                "form": cfg_map.get("w_form", 0.25),
                "h2h":  cfg_map.get("w_h2h",  0.15),
            }
            print(f"Веса ансамбля: elo={ensemble_weights['elo']} form={ensemble_weights['form']} h2h={ensemble_weights['h2h']}")
    except Exception as e:
        print(f"  [WARN] model_config: {e}")

    # ── Окно матчей ──────────────────────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_AHEAD)
    soon = []
    for m in matches:
        starts_at = m.get("startsAt")
        if not starts_at:
            continue
        try:
            ts = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts <= cutoff:
            teams = m.get("teams") or [None, None]
            t1 = (teams[0] or {}).get("name") if teams[0] else None
            t2 = (teams[1] or {}).get("name") if teams[1] else None
            if is_real_team(t1) and is_real_team(t2):
                soon.append({**m, "_t1": t1, "_t2": t2, "_ts": ts})

    print(f"Матчей в окне {HOURS_AHEAD}ч: {len(soon)}")
    if not soon:
        print("Нет матчей — выходим.")
        return

    elo, history = build_elo_from_supabase()
    alias_map = fetch_team_aliases()
    if alias_map:
        print(f"  алиасов: {len(alias_map)}")

    # Уже поставленные (идемпотентность по event_id И по матчу)
    existing = sb_get(
        "elo_paper_bets",
        f"strategy_name=eq.{STRATEGY_NAME}&division=eq.{DIVISION}"
        "&select=event_id,home_team,away_team,start_time",
    )
    done_ids = {r["event_id"] for r in existing}
    # Дедупликация по матчу: (norm_home, norm_away, start_hour)
    # Только для РЕАЛЬНЫХ ставок (stake_usd > 0) — tracking-записи без одсов
    # не блокируют будущие прогоны, если BetsAPI вдруг вернёт коэффы
    done_matchups: set[tuple[str, str, int]] = {
        (
            normalize_team(r["home_team"]),
            normalize_team(r["away_team"]),
            int(r["start_time"] or 0) // 3600,
        )
        for r in existing
        if r.get("start_time") and float(r.get("stake_usd") or 0) > 0
    }

    # Дневной лимит ставок
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_staked = 0.0
    try:
        today_bets = sb_get(
            "elo_paper_bets",
            f"strategy_name=eq.{STRATEGY_NAME}"
            f"&run_ts=gte.{today_start.isoformat()}"
            f"&select=stake_usd,bet_team,home_team,away_team,league",
        )
        today_staked = sum(float(b.get("stake_usd") or 0) for b in today_bets)
    except Exception:
        today_bets = []
    daily_cap_usd = bankroll * DAILY_STAKE_CAP
    print(f"Дневной лимит: ${daily_cap_usd:.0f} | уже поставлено сегодня: ${today_staked:.0f}")

    # Корреляционный трекинг
    from collections import defaultdict
    team_bets_today: dict[str, int]     = defaultdict(int)
    league_staked_today: dict[str, float] = defaultdict(float)
    try:
        for b in today_bets:
            bt  = b.get("bet_team", "home")
            key = b.get("home_team") if bt == "home" else b.get("away_team")
            if key:
                team_bets_today[key] += 1
            lg = b.get("league") or ""
            league_staked_today[lg] += float(b.get("stake_usd") or 0)
    except Exception:
        pass

    rows = []
    skipped_no_odds = 0
    skipped_no_edge = 0
    skipped_no_elo  = 0
    skipped_daily   = 0
    skipped_corr    = 0

    for m in soon:
        eid = f"liq_{m.get('hash')}"
        if eid in done_ids:
            continue

        t1, t2 = m["_t1"], m["_t2"]

        # Дедупликация по матчу (защита от нестабильного hash Liquipedia)
        _start_ts_pre = int(m["_ts"].timestamp())
        matchup_key = (normalize_team(t1), normalize_team(t2), _start_ts_pre // 3600)
        if matchup_key in done_matchups:
            print(f"  [дубль матча] {t1} vs {t2} — уже поставлено ранее, пропуск")
            continue
        t1_lookup = resolve_alias(t1, alias_map)
        t2_lookup = resolve_alias(t2, alias_map)

        m1, _ = best_elo_match(t1_lookup, elo)
        m2, _ = best_elo_match(t2_lookup, elo)
        if not (m1 and m2):
            print(f"  ? {t1} vs {t2} — нет Elo, пробуем тоталы...")
            totals_row = try_totals_bet(
                t1, t2, int(m["_ts"].timestamp()), m,
                tiers, bankroll, a_mult, daily_cap_usd, today_staked, eid,
            )
            if totals_row and totals_row["stake_usd"] > 0:
                rows.append(totals_row)
                today_staked += totals_row["stake_usd"]
                done_matchups.add(matchup_key)
            elif totals_row:
                rows.append(totals_row)  # tracking без $
            else:
                skipped_no_elo += 1
            continue

        # ── Слой 1: Сигналы ─────────────────────────────────────────────────
        e1, e2 = elo[m1], elo[m2]
        elo_prob_t1 = elo_exp(e1, e2)

        form_t1  = compute_form(t1_lookup, history, n=10)
        form_t2  = compute_form(t2_lookup, history, n=10)
        h2h_t1   = compute_h2h(t1_lookup, t2_lookup, history, n=8)
        start_ts = int(m["_ts"].timestamp())

        # Fatigue
        fat_t1   = compute_fatigue(t1_lookup, history, start_ts)
        fat_t2   = compute_fatigue(t2_lookup, history, start_ts)

        # Определяем на кого ставим (фаворит по elo_prob)
        fav_is_t1  = elo_prob_t1 >= 0.5
        fav_team   = t1 if fav_is_t1 else t2
        fav_lookup = t1_lookup if fav_is_t1 else t2_lookup
        opp_lookup = t2_lookup if fav_is_t1 else t1_lookup

        elo_prob_fav = elo_prob_t1 if fav_is_t1 else (1 - elo_prob_t1)
        form_fav     = (form_t1 if fav_is_t1 else form_t2)
        h2h_fav      = h2h_t1 if fav_is_t1 else (1 - h2h_t1 if h2h_t1 is not None else None)
        fat_fav      = fat_t1 if fav_is_t1 else fat_t2
        fat_opp      = fat_t2 if fav_is_t1 else fat_t1
        fat_adj      = fatigue_adjustment(fat_fav, fat_opp)

        comp_prob = composite_prob(
            elo_prob_fav, form_fav, h2h_fav,
            weights=ensemble_weights,
            fatigue_adj=fat_adj,
        )

        # ── Слой 2: BetsAPI + Edge filter ───────────────────────────────────
        bet_side  = "home" if fav_is_t1 else "away"
        real_odds, real_bm = lookup_real_odds(t1, t2, start_ts, bet_side)

        if not real_odds:
            skipped_no_odds += 1
            # Записываем информационную ставку без размера (для трекинга winrate)
            notional_odds = round(1.0 / (comp_prob * AVG_OVERROUND_HIST), 3)
            print(f"  [нет реальных одсов] {t1} vs {t2} — notional={notional_odds}, не ставим")
            # Пишем с stake=0 чтобы иметь трек прогнозов без $ риска
            rows.append({
                "run_ts":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "strategy_name": STRATEGY_NAME,
                "event_id":      eid,
                "division":      DIVISION,
                "league":        m.get("leagueName"),
                "home_team": t1, "away_team": t2,
                "start_time":    start_ts,
                "bookmaker":     "NOTIONAL_HIST_AVG",
                "bet_team":      bet_side,
                "odds":          notional_odds,
                "market_prob":   round(1.0 / notional_odds, 4),
                "model_prob":    round(elo_prob_fav, 4),
                "composite_prob": comp_prob,
                "edge":          None,
                "stake_usd":     0.0,   # не ставим без реальных одсов
                "settled":       False,
                "real_odds":     None,
                "real_bookmaker": None,
                "form_score":    round(form_fav, 4) if form_fav is not None else None,
                "h2h_score":     round(h2h_fav, 4) if h2h_fav is not None else None,
                "kelly_f":       None,
                "league_tier":   get_league_tier(m.get("leagueName"), tiers),
            })
            continue

        # Считаем реальный edge
        real_edge = round(comp_prob * real_odds - 1, 4)
        tier      = get_league_tier(m.get("leagueName"), tiers)
        edge_min, kelly_cap = get_tier_params(tier, tiers)
        notional_odds = round(1.0 / (comp_prob * AVG_OVERROUND_HIST), 3)

        if real_edge < edge_min:
            skipped_no_edge += 1
            print(f"  [edge мал] {t1} vs {t2}  edge={real_edge:+.1%} < {edge_min:.0%} (T{tier}) — пропуск")
            continue

        # ── Слой 3: Kelly sizing ─────────────────────────────────────────────
        stake = kelly_stake(
            p=comp_prob,
            odds=real_odds,
            bankroll=bankroll,
            fraction=KELLY_FRACTION_DEFAULT,
            cap=kelly_cap,
            adaptive_mult=a_mult,
        )

        if stake <= 0:
            print(f"  [kelly=0] {t1} vs {t2} — Kelly отрицательный, пропуск")
            continue

        # ── Слой 4a: Корреляционный лимит ───────────────────────────────────
        if team_bets_today[fav_team] >= 2:
            skipped_corr += 1
            print(f"  [корреляция] {fav_team} уже {team_bets_today[fav_team]}× сегодня — пропуск")
            continue

        league_name_key    = m.get("leagueName") or ""
        league_budget_used = league_staked_today[league_name_key]
        league_budget_max  = daily_cap_usd * 0.30
        if league_budget_used + stake > league_budget_max:
            remaining = max(0.0, league_budget_max - league_budget_used)
            if remaining < 1.0:
                skipped_corr += 1
                print(f"  [турнир лимит] {league_name_key[:30]} — лимит 30% исчерпан")
                continue
            stake = round(remaining, 1)
            print(f"  [турнир лимит] {league_name_key[:30]} — ставка срезана до ${stake}")

        # ── Слой 4b: Дневной лимит ───────────────────────────────────────────
        if today_staked + stake > daily_cap_usd:
            skipped_daily += 1
            print(f"  [дневной лимит] {t1} vs {t2} — лимит ${daily_cap_usd:.0f} исчерпан")
            continue
        today_staked += stake
        team_bets_today[fav_team] += 1
        league_staked_today[league_name_key] += stake

        # ── Расчёт kelly_f для сохранения ────────────────────────────────────
        b = real_odds - 1.0
        full_k = (b * comp_prob - (1 - comp_prob)) / b if b > 0 else 0
        kelly_f_applied = round(min(full_k * KELLY_FRACTION_DEFAULT * a_mult, kelly_cap), 5)

        print(
            f"\n  ✓ {t1} vs {t2}  ({m.get('leagueName')})  {m['_ts'].strftime('%d.%m %H:%M')}Z  T{tier}"
            f"\n    elo_p={elo_prob_fav:.3f} form={form_fav:.3f if form_fav else '—'} "
            f"h2h={h2h_fav:.3f if h2h_fav else '—'} → comp_p={comp_prob:.3f}"
            f"\n    real={real_odds} [{real_bm}]  edge={real_edge:+.1%}  "
            f"kelly_f={kelly_f_applied:.4f}  stake=${stake:.0f}  bank=${bankroll:.0f}"
        )

        rows.append({
            "run_ts":         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy_name":  STRATEGY_NAME,
            "event_id":       eid,
            "division":       DIVISION,
            "league":         m.get("leagueName"),
            "home_team": t1,  "away_team": t2,
            "start_time":     start_ts,
            "bookmaker":      real_bm,
            "bet_team":       bet_side,
            "odds":           notional_odds,
            "market_prob":    round(comp_prob / real_odds, 4),
            "model_prob":     round(elo_prob_fav, 4),
            "composite_prob": comp_prob,
            "edge":           real_edge,
            "stake_usd":      stake,
            "settled":        False,
            "real_odds":      real_odds,
            "real_bookmaker": real_bm,
            "form_score":     round(form_fav, 4) if form_fav is not None else None,
            "h2h_score":      round(h2h_fav, 4) if h2h_fav is not None else None,
            "kelly_f":        kelly_f_applied,
            "league_tier":    tier,
        })
        done_matchups.add(matchup_key)  # не ставим дважды в одном прогоне

    sb_upsert("elo_paper_bets", rows, on_conflict="strategy_name,event_id,division")

    real_bets    = [r for r in rows if r["stake_usd"] > 0]
    track_only   = [r for r in rows if r["stake_usd"] == 0]
    elo_bets     = [r for r in real_bets if r.get("bet_market", "moneyline") == "moneyline"]
    totals_bets  = [r for r in real_bets if r.get("bet_market", "moneyline") != "moneyline"]
    print(
        f"\n── Итог ──────────────────────────────────────────\n"
        f"  Реальных ставок: {len(real_bets)}"
        f" (moneyline: {len(elo_bets)}, тоталы: {len(totals_bets)})\n"
        f"  Трекинг без $: {len(track_only)}\n"
        f"  Пропущено (нет Elo/тоталов): {skipped_no_elo}\n"
        f"  Пропущено (нет odds): {skipped_no_odds}\n"
        f"  Пропущено (мало edge): {skipped_no_edge}\n"
        f"  Пропущено (дневной лимит): {skipped_daily}\n"
        f"  Пропущено (корреляция/турнир): {skipped_corr}"
    )


if __name__ == "__main__":
    main()
