"""
BetsAPI adapter — E-sports (sport_id=151), Dota 2.

Эндпоинты:
  /v3/events/upcoming  — предстоящие матчи
  /v3/events/ended     — завершённые (исторические)
  /v2/event/odds       — odds + история движения (since_time=0)
  /v2/event/odds/summary — opening/closing по всем букмекерам

Market 151_1 = Match Winner 2-Way (home_od / away_od)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from config import settings

BASE       = settings.betsapi_base_url  # https://api.b365api.com
SPORT_ID   = 151    # E-sports
MARKET_KEY = "151_1"  # Match Winner 2-Way
PAGE_SIZE  = 50
DOTA_KEYWORDS = ["dota", "dota2", "dota 2"]


class BetsAPIClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or settings.betsapi_token
        if not self.token:
            raise RuntimeError("BETSAPI_TOKEN not set in .env")
        self.session = requests.Session()
        self._last_call = 0.0

    def _get(self, path: str, params: Optional[dict] = None,
             _retry: int = 3) -> dict:
        # Уважаем rate limit: 1800 req/hour → ~1s между запросами
        elapsed = time.time() - self._last_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        p = {"token": self.token, **(params or {})}
        r = self.session.get(f"{BASE}{path}", params=p, timeout=15)
        self._last_call = time.time()

        if r.status_code == 429:
            if _retry > 0:
                wait = 300  # 5 минут — окно rate limit BetsAPI
                print(f"    [BetsAPI] 429 rate limit — ждём {wait}s (осталось попыток: {_retry})...", flush=True)
                time.sleep(wait)
                return self._get(path, params, _retry=_retry - 1)
            raise RuntimeError("BetsAPI 429: rate limit exceeded after retries")

        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"BetsAPI error: {data}")
        return data

    # ── Upcoming ──────────────────────────────────────────────────────────────

    def get_upcoming(self, page: int = 1) -> dict:
        return self._get("/v3/events/upcoming", {"sport_id": SPORT_ID, "page": page})

    def get_upcoming_dota2(self) -> list[dict]:
        """Все предстоящие Dota 2 матчи (все страницы)."""
        results = []
        page = 1
        while True:
            data  = self.get_upcoming(page)
            items = data.get("results", [])
            total = data.get("pager", {}).get("total", 0)
            for e in items:
                if _is_dota2(e):
                    results.append(e)
            if page * PAGE_SIZE >= total or not items:
                break
            page += 1
        return results

    # ── Ended (historical) ────────────────────────────────────────────────────

    def get_ended(self, page: int = 1) -> dict:
        return self._get("/v3/events/ended", {"sport_id": SPORT_ID, "page": page})

    def get_ended_dota2(self, max_pages: int = 500) -> list[dict]:
        """
        Завершённые Dota 2 матчи за все страницы.
        236K событий → ~4720 страниц. max_pages ограничивает.
        """
        results = []
        page = 1
        while page <= max_pages:
            data  = self.get_ended(page)
            items = data.get("results", [])
            if not items:
                break
            for e in items:
                if _is_dota2(e):
                    results.append(e)
            total = data.get("pager", {}).get("total", 0)
            if page * PAGE_SIZE >= total:
                break
            page += 1
        return results

    # ── Odds ─────────────────────────────────────────────────────────────────

    def get_odds_summary(self, event_id: str) -> dict:
        """Opening + closing odds по всем букмекерам."""
        return self._get("/v2/event/odds/summary", {"event_id": event_id})

    def get_odds_history(self, event_id: str,
                         source: str = "bet365") -> dict:
        """Полная история движения линии (since_time=0)."""
        return self._get("/v2/event/odds", {
            "event_id":   event_id,
            "source":     source,
            "since_time": "0",
        })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_dota2(event: dict) -> bool:
    league = (event.get("league") or {}).get("name", "").lower()
    return any(kw in league for kw in DOTA_KEYWORDS)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_to_db(conn: sqlite3.Connection,
                 home: str, away: str,
                 event_time: str) -> tuple[Optional[str], str]:
    """Fuzzy-матч к matches.external_id по именам команд."""
    n1, n2 = _normalize(home), _normalize(away)
    rows = conn.execute(
        "SELECT external_id, name, team_1_name, team_2_name FROM matches "
        "WHERE team_1_name IS NOT NULL ORDER BY begin_at DESC LIMIT 5000"
    ).fetchall()
    for r in rows:
        d1 = _normalize(r["team_1_name"] or "")
        d2 = _normalize(r["team_2_name"] or "")
        if (d1 == n1 and d2 == n2) or (d1 == n2 and d2 == n1):
            return r["external_id"], r["name"]
    for r in rows:
        d1 = _normalize(r["team_1_name"] or "")
        d2 = _normalize(r["team_2_name"] or "")
        if (n1 in d1 or d1 in n1) and (n2 in d2 or d2 in n2):
            return r["external_id"], r["name"]
        if (n2 in d1 or d1 in n2) and (n1 in d2 or d2 in n1):
            return r["external_id"], r["name"]
    return None, f"{home} vs {away}"


def _extract_moneyline(odds_summary: dict) -> list[dict]:
    """
    Из /v2/event/odds/summary извлечь opening/closing по каждому букмекеру.

    Реальная структура BetsAPI:
    {
      "Bet365": {
        "odds": {
          "start": {"151_1": {"home_od": "1.01", "away_od": "15.0", ...}},
          "end":   {"151_1": {"home_od": "1.02", "away_od": "12.0", ...}}
        }
      }
    }
    """
    rows = []
    results = odds_summary.get("results", {})
    for bm_name, bm_data in results.items():
        odds  = bm_data.get("odds", {})
        start = odds.get("start", {})
        end   = odds.get("end", {}) or start

        mk_start = start.get(MARKET_KEY, {})
        mk_end   = end.get(MARKET_KEY, {}) or mk_start

        if not mk_start:
            continue

        o_home = _safe_float(mk_start.get("home_od") or mk_start.get("home"))
        o_away = _safe_float(mk_start.get("away_od") or mk_start.get("away"))
        c_home = _safe_float(mk_end.get("home_od")   or mk_end.get("home"))
        c_away = _safe_float(mk_end.get("away_od")   or mk_end.get("away"))

        if not o_home or not o_away:
            continue

        rows.append({
            "bookmaker":  bm_name,
            "open_home":  o_home,
            "open_away":  o_away,
            "close_home": c_home or o_home,
            "close_away": c_away or o_away,
        })
    return rows


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


# ── DB writer ─────────────────────────────────────────────────────────────────

def _insert_snapshot(conn: sqlite3.Connection,
                     captured_at: str,
                     event: dict,
                     bm: dict,
                     odds_home: float,
                     odds_away: float,
                     ext_id: Optional[str],
                     match_name: str,
                     raw: dict,
                     league_name: Optional[str] = None):
    imp1 = round(1.0 / odds_home, 6)
    imp2 = round(1.0 / odds_away, 6)
    overround = round(imp1 + imp2, 4)
    conn.execute(
        """INSERT INTO odds_snapshots
           (captured_at, match_external_id, match_name, match_start_at,
            source, bookmaker,
            team_1_name, team_2_name,
            team_1_odds, team_2_odds,
            team_1_implied_prob, team_2_implied_prob,
            overround, league_name, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (captured_at,
         ext_id or str(event.get("id", "")),
         match_name,
         str(event.get("time", "")),
         "betsapi", bm["bookmaker"],
         event.get("home", {}).get("name", ""),
         event.get("away", {}).get("name", ""),
         odds_home, odds_away, imp1, imp2, overround,
         league_name or event.get("league", {}).get("name", ""),
         json.dumps(raw, ensure_ascii=False)),
    )


# ── Public collect functions ──────────────────────────────────────────────────

def collect_live_dota2(db_path: Path) -> int:
    """
    Собрать текущие odds для всех предстоящих Dota 2 матчей.
    Сохраняет opening snapshot (или текущий если уже близко к старту).
    """
    client = BetsAPIClient()
    events = client.get_upcoming_dota2()
    if not events:
        print("    [BetsAPI] No upcoming Dota 2 events")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0

    for event in events:
        home    = event.get("home", {}).get("name", "")
        away    = event.get("away", {}).get("name", "")
        eid     = str(event.get("id", ""))
        ext_id, match_name = _match_to_db(conn, home, away, str(event.get("time", "")))

        try:
            summary = client.get_odds_summary(eid)
            bms = _extract_moneyline(summary)
        except Exception as e:
            print(f"    [BetsAPI] odds error for {match_name}: {e}")
            continue

        for bm in bms:
            _insert_snapshot(conn, captured_at, event, bm,
                             bm["close_home"], bm["close_away"],
                             ext_id, match_name,
                             {"event": event, "bm": bm})
            inserted += 1

    conn.commit()
    conn.close()
    print(f"    [BetsAPI] Inserted {inserted} snapshots from {len(events)} Dota 2 events")
    return inserted
