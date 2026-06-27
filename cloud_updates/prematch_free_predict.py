#!/usr/bin/env python3
"""
Prematch Free Predict — модельные пики на pro-матчи Dota2 с реальными коэффами.

Источники:
  1. Расписание:  dota.haglund.dev/v1/matches  (Liquipedia, бесплатно, без ключа)
  2. Elo-сила:    Supabase (betsapi_events + elo_pandascore_history + elo_own_history)
  3. Коэффициенты: BetsAPI /v3/events/upcoming + /v2/event/odds/summary
                   (требует BETSAPI_TOKEN; если токен не задан — режим без коэфов)

Edge = fav_prob × real_odds_fav − 1
  Положительный edge → модель считает ставку value bet.

Run:
    python3 scripts/prematch_free_predict.py

GitHub Actions: каждые 3 часа (prematch_free_predict_pipeline.yml).
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from math import pow
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_ANON_KEY", "")
BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

MATCHES_URL      = "https://dota.haglund.dev/v1/matches"
BETSAPI_BASE     = "https://api.b365api.com"
BETSAPI_SPORT_ID = 151       # E-sports
BETSAPI_MARKET   = "151_1"   # Match Winner 2-Way
HOURS_AHEAD      = 72
START_ELO        = 1500.0
K_FACTOR         = 32
FUZZY_MIN        = 0.72      # порог fuzzy-match имён команд в Elo
ODDS_FUZZY_MIN   = 0.60      # порог match Liquipedia↔BetsAPI (чуть ниже, т.к. разные источники)
ODDS_TIME_WINDOW = 6 * 3600  # 6ч окно для совпадения времени начала
PREFERRED_BM     = ["PinnacleSports", "Pinnacle", "Bet365", "GGBet", "MelBet"]
MIN_EDGE         = -0.20     # не сохраняем строки с edge хуже −20% (шум)


# ── Supabase helpers ─────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


# ── BetsAPI client (inline, без зависимости от local adapters/) ──────────────

class BetsAPIClient:
    """Минимальный клиент BetsAPI только для нужных нам эндпоинтов."""

    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self._last_call = 0.0

    def _get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.time() - self._last_call
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        p = {"token": self.token, **(params or {})}
        r = self.session.get(f"{BETSAPI_BASE}{path}", params=p, timeout=15)
        self._last_call = time.time()
        if r.status_code == 429:
            print("  [BetsAPI] 429 rate limit — ждём 65 сек...", flush=True)
            time.sleep(65)
            return self._get(path, params)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"BetsAPI error: {data}")
        return data

    def get_upcoming_dota2(self) -> list[dict]:
        """Все предстоящие Dota 2 матчи (до 3 страниц)."""
        results = []
        for page in range(1, 4):
            data  = self._get("/v3/events/upcoming", {"sport_id": BETSAPI_SPORT_ID, "page": page})
            items = data.get("results", [])
            total = int(data.get("pager", {}).get("total", 0))
            for e in items:
                league = (e.get("league") or {}).get("name", "").lower()
                if any(kw in league for kw in ["dota", "dota2", "dota 2"]):
                    results.append(e)
            if len(results) >= total or not items:
                break
        return results

    def get_odds_summary(self, event_id: str) -> dict:
        """Opening + closing odds из /v2/event/odds/summary."""
        return self._get("/v2/event/odds/summary", {"event_id": event_id})


def extract_best_odds(summary: dict) -> tuple[str | None, float | None, float | None]:
    """
    Из ответа /v2/event/odds/summary выбрать лучшего букмекера
    (PinnacleSports приоритет) и вернуть (bookmaker, open_home, open_away).

    Структура ответа:
      { "BookmakerName": { "odds": { "start": { "151_1": { "home_od": "...", "away_od": "..." } } } } }
    """
    results_raw = summary.get("results", {})
    candidates: dict[str, tuple[float, float]] = {}

    for bm_name, bm_data in results_raw.items():
        try:
            start_odds = bm_data["odds"]["start"].get(BETSAPI_MARKET, {})
            home_od = float(start_odds.get("home_od") or 0)
            away_od = float(start_odds.get("away_od") or 0)
            if home_od > 1.01 and away_od > 1.01:
                candidates[bm_name] = (home_od, away_od)
        except (KeyError, TypeError, ValueError):
            continue

    if not candidates:
        return None, None, None

    for preferred in PREFERRED_BM:
        for bm_name, (h, a) in candidates.items():
            if preferred.lower() in bm_name.lower():
                return bm_name, h, a

    bm_name, (h, a) = next(iter(candidates.items()))
    return bm_name, h, a


# ── Elo ───────────────────────────────────────────────────────────────────────

def elo_exp(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


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
        print(f"  [WARN] team_aliases недоступна: {ex}")
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


def build_elo_from_supabase() -> dict[str, float]:
    print("Тяну историю dota2 матчей из Supabase для Elo...")
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
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    for r in rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys:
            continue
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
        if not chunk:
            break
        ps_rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    ps_added = 0
    for r in ps_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys:
            continue
        nw = normalize_team(w)
        act_h = 1.0 if nw == normalize_team(t1) else (0.0 if nw == normalize_team(t2) else None)
        if act_h is None:
            continue
        seen_keys.add(key)
        history.append((int(st), t1, t2, act_h))
        ps_added += 1

    # 3) Своя накопленная история
    own_rows, offset = [], 0
    while True:
        chunk = sb_get(
            "elo_own_history",
            f"winner=neq.&select=home_team,away_team,winner,start_time&"
            f"order=start_time.asc&limit={page}&offset={offset}",
        )
        if not chunk:
            break
        own_rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    own_added = 0
    for r in own_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None:
            continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen_keys:
            continue
        nw = normalize_team(w)
        act_h = 1.0 if nw == normalize_team(t1) else (0.0 if nw == normalize_team(t2) else None)
        if act_h is None:
            continue
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

    print(f"  матчей: {len(history)} (BetsAPI: {n_betsapi}, +PandaScore: {ps_added}, "
          f"+своя: {own_added})  |  команд: {len(elo)}")
    return elo


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_elo_match(name: str, elo: dict[str, float]) -> tuple[str | None, float]:
    best, score = None, 0.0
    for team in elo:
        s = fuzzy(name, team)
        if s > score:
            best, score = team, s
    return (best, score) if score >= FUZZY_MIN else (None, score)


# ── BetsAPI ↔ Liquipedia matching ────────────────────────────────────────────

def match_liquipedia_to_betsapi(
    liq_t1: str, liq_t2: str, liq_ts: datetime,
    betsapi_events: list[dict],
) -> dict | None:
    """
    Находим BetsAPI-событие для Liquipedia-матча по:
      1. Fuzzy совпадению обеих команд (≥ ODDS_FUZZY_MIN каждая)
      2. Разнице времени ≤ ODDS_TIME_WINDOW
    Возвращаем best match или None.
    """
    liq_ts_ts = liq_ts.timestamp()
    best_score, best_event = 0.0, None

    for ev in betsapi_events:
        home = (ev.get("home") or {}).get("name", "")
        away = (ev.get("away") or {}).get("name", "")
        ev_ts = float(ev.get("time", 0))

        if abs(ev_ts - liq_ts_ts) > ODDS_TIME_WINDOW:
            continue

        # прямое совпадение (t1=home, t2=away)
        s1h = fuzzy(liq_t1, home)
        s2a = fuzzy(liq_t2, away)
        # обратное (t1=away, t2=home)
        s1a = fuzzy(liq_t1, away)
        s2h = fuzzy(liq_t2, home)

        score_direct  = (s1h + s2a) / 2 if (s1h >= ODDS_FUZZY_MIN and s2a >= ODDS_FUZZY_MIN) else 0
        score_reverse = (s1a + s2h) / 2 if (s1a >= ODDS_FUZZY_MIN and s2h >= ODDS_FUZZY_MIN) else 0
        score = max(score_direct, score_reverse)

        if score > best_score:
            best_score = score
            # Запоминаем: если обратное лучше — ставим флаг, чтобы знать,
            # что home/away у BetsAPI перевёрнуты относительно Liquipedia
            best_event = {**ev, "_reversed": score_reverse > score_direct, "_match_score": score}

    return best_event


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    has_betsapi = bool(BETSAPI_TOKEN)
    if not has_betsapi:
        print("  [INFO] BETSAPI_TOKEN не задан — работаем без реальных коэфов (только Elo-прогноз)")

    # Загружаем расписание Liquipedia
    try:
        matches_raw = requests.get(MATCHES_URL, timeout=20).json()
    except Exception as ex:
        print(f"ERROR: dota.haglund.dev недоступен: {ex}")
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_AHEAD)
    soon = []
    for m in matches_raw:
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
            if t1 and t1 != "TBD" and t2 and t2 != "TBD":
                soon.append({**m, "_t1": t1, "_t2": t2, "_ts": ts})

    print(f"Матчей в окне {HOURS_AHEAD}ч: {len(soon)}")
    if not soon:
        print("Нет матчей — выходим.")
        return

    # Elo
    elo = build_elo_from_supabase()
    alias_map = fetch_team_aliases()
    if alias_map:
        print(f"  алиасов загружено: {len(alias_map)}")

    # BetsAPI: загружаем все предстоящие матчи одним запросом
    betsapi_events: list[dict] = []
    if has_betsapi:
        print("\nЗагружаю предстоящие матчи из BetsAPI...")
        try:
            client = BetsAPIClient(BETSAPI_TOKEN)
            betsapi_events = client.get_upcoming_dota2()
            print(f"  BetsAPI: найдено {len(betsapi_events)} Dota2-матчей")
        except Exception as ex:
            print(f"  [WARN] BetsAPI недоступен: {ex} — продолжаем без коэфов")

    rows = []
    for m in soon:
        t1_raw, t2_raw = m["_t1"], m["_t2"]
        t1 = clean_team_name(t1_raw)
        t2 = clean_team_name(t2_raw)
        t1_lookup = resolve_alias(t1, alias_map)
        t2_lookup = resolve_alias(t2, alias_map)
        if t1_lookup != t1:
            print(f"  [алиас] {t1} -> {t1_lookup}")
        if t2_lookup != t2:
            print(f"  [алиас] {t2} -> {t2_lookup}")

        m1, _ = best_elo_match(t1_lookup, elo)
        m2, _ = best_elo_match(t2_lookup, elo)
        e1 = elo.get(m1, START_ELO) if m1 else START_ELO
        e2 = elo.get(m2, START_ELO) if m2 else START_ELO
        elo_diff     = round(e1 - e2, 1)
        model_prob_1 = round(elo_exp(e1, e2), 4)
        has_elo      = bool(m1 and m2)

        fav      = t1 if model_prob_1 >= 0.5 else t2
        fav_prob = model_prob_1 if model_prob_1 >= 0.5 else round(1 - model_prob_1, 4)
        # True если фаворит — команда 1 (home в терминах BetsAPI)
        fav_is_t1 = model_prob_1 >= 0.5

        print(f"\n  {t1} vs {t2}  ({m.get('leagueName')})  старт {m['_ts'].strftime('%Y-%m-%d %H:%M')}Z")
        if has_elo:
            print(f"    Elo: {m1}={e1:.0f}  {m2}={e2:.0f}  diff={elo_diff:+.0f}  фаворит {fav} ({fav_prob:.0%})")
        else:
            print(f"    [нет Elo-данных]")

        # BetsAPI odds
        betsapi_event_id = None
        real_odds_fav    = None
        real_odds_dog    = None
        edge_pct         = None
        bookmaker        = None
        has_real_odds    = False

        if has_betsapi and betsapi_events and has_elo:
            bev = match_liquipedia_to_betsapi(t1, t2, m["_ts"], betsapi_events)
            if bev:
                betsapi_event_id = str(bev.get("id", ""))
                reversed_sides   = bev.get("_reversed", False)
                try:
                    summary = client.get_odds_summary(betsapi_event_id)
                    bm, home_od, away_od = extract_best_odds(summary)
                    if bm and home_od and away_od:
                        bookmaker = bm
                        # Если BetsAPI home = наш t1, то odds_fav зависит от fav_is_t1
                        # Если reversed_sides=True, то BetsAPI home = наш t2
                        if not reversed_sides:
                            real_odds_fav = home_od if fav_is_t1 else away_od
                            real_odds_dog = away_od if fav_is_t1 else home_od
                        else:
                            real_odds_fav = away_od if fav_is_t1 else home_od
                            real_odds_dog = home_od if fav_is_t1 else away_od
                        edge_pct = round(fav_prob * real_odds_fav - 1, 4)
                        has_real_odds = True
                        sign = "+" if edge_pct >= 0 else ""
                        print(f"    Коэф {bm}: фаворит {real_odds_fav}  аутсайдер {real_odds_dog}  "
                              f"edge {sign}{edge_pct*100:.1f}%")
                except Exception as ex:
                    print(f"    [WARN] odds error {betsapi_event_id}: {ex}")
            else:
                print(f"    [INFO] матч не найден в BetsAPI")

        row = {
            "match_hash":       m.get("hash"),
            "team_1":           t1, "team_2": t2,
            "league_name":      m.get("leagueName"),
            "starts_at":        m["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elo_team_1":       round(e1, 1), "elo_team_2": round(e2, 1),
            "elo_diff":         elo_diff, "model_prob_team_1": model_prob_1,
            "favorite":         fav, "favorite_prob": fav_prob,
            "has_elo_data":     has_elo,
            "betsapi_event_id": betsapi_event_id,
            "real_odds_fav":    real_odds_fav,
            "real_odds_underdog": real_odds_dog,
            "edge_pct":         edge_pct,
            "bookmaker":        bookmaker,
            "has_real_odds":    has_real_odds,
            "checked_at":       now_iso(),
        }
        rows.append(row)

    sb_upsert("prematch_model_picks", rows, on_conflict="match_hash")
    odds_count = sum(1 for r in rows if r["has_real_odds"])
    print(f"\nЗаписано в prematch_model_picks: {len(rows)}  "
          f"(с реальными коэфами: {odds_count}, только Elo: {len(rows)-odds_count})")


if __name__ == "__main__":
    main()
