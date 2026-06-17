"""
Pinnacle Odds API через RapidAPI.
Host: pinnacle-odds-api.p.rapidapi.com

Структура запросов:
  1. GET /pinnacle/sports          → найти sport_id Dota 2
  2. GET /pinnacle/leagues         → найти лиги Dota 2 (sportId=...)
  3. GET /pinnacle/matchups        → матчи по leagueIds
  4. GET /pinnacle/odds/{eventId}  → коэффициенты на матч
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from config import settings

HOST = "pinnacle-odds-api.p.rapidapi.com"
BASE = f"https://{HOST}"


class PinnacleClient:
    def __init__(self, key: Optional[str] = None):
        self.key = key or settings.rapidapi_key
        if not self.key:
            raise RuntimeError("RAPIDAPI_KEY not set in .env")
        self.session = requests.Session()
        self.session.headers.update({
            "X-RapidAPI-Key":  self.key,
            "X-RapidAPI-Host": HOST,
            "Accept":          "application/json",
        })

    def _get(self, path: str, params: Optional[dict] = None):
        r = self.session.get(f"{BASE}{path}", params=params or {}, timeout=20)
        r.raise_for_status()
        return r.json()

    def get_sports(self) -> list[dict]:
        return self._get("/pinnacle/sports")

    def find_dota2_sport_id(self) -> Optional[int]:
        sports = self.get_sports()
        for s in sports:
            name = (s.get("name") or s.get("sportName") or "").lower()
            if "dota" in name or "esport" in name:
                return s.get("id") or s.get("sportId")
        return None

    def get_leagues(self, sport_id: int) -> list[dict]:
        return self._get("/pinnacle/leagues", {"sportId": sport_id})

    def get_matchups(self, league_ids: list[int]) -> list[dict]:
        ids = ",".join(str(i) for i in league_ids)
        return self._get("/pinnacle/matchups", {"leagueIds": ids})

    def get_odds(self, event_id: int) -> Optional[dict]:
        try:
            return self._get(f"/pinnacle/odds/{event_id}")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_dota2_odds(self) -> list[dict]:
        """
        Полный pipeline: sports → leagues → matchups → odds.
        Возвращает список {matchup, odds} для всех Dota 2 матчей.
        """
        # 1. Найти Dota 2 sport
        sport_id = self.find_dota2_sport_id()
        if sport_id is None:
            # Попробуем esports напрямую
            sports = self.get_sports()
            esport_ids = [
                s.get("id") or s.get("sportId")
                for s in sports
                if "sport" in str(s).lower()
            ]
            # Если нет Dota — вернуть пустой список, но показать доступные
            return []

        # 2. Получить лиги
        leagues = self.get_leagues(sport_id)
        league_ids = [l.get("id") or l.get("leagueId") for l in leagues if l.get("id") or l.get("leagueId")]
        if not league_ids:
            return []

        # 3. Получить матчи (батчами по 10 лиг)
        all_matchups = []
        batch_size = 10
        for i in range(0, len(league_ids), batch_size):
            batch = league_ids[i:i+batch_size]
            try:
                mu = self.get_matchups(batch)
                all_matchups.extend(mu if isinstance(mu, list) else [])
            except Exception:
                pass

        if not all_matchups:
            return []

        # 4. Получить odds для каждого матча
        results = []
        for matchup in all_matchups:
            event_id = matchup.get("id") or matchup.get("eventId") or matchup.get("matchupId")
            if not event_id:
                continue
            odds = self.get_odds(int(event_id))
            results.append({"matchup": matchup, "odds": odds})

        return results

    def probe(self) -> dict:
        """Диагностика: что возвращают эндпоинты."""
        try:
            sports = self.get_sports()
            dota = [s for s in sports if "dota" in str(s).lower() or "esport" in str(s).lower()]
            esport_like = [s for s in sports if "sport" in str(s.get("name","")).lower() or
                           any(k in str(s).lower() for k in ["esport", "dota", "cs", "lol", "counter"])]

            return {
                "status":       "ok",
                "total_sports": len(sports),
                "dota_found":   len(dota) > 0,
                "dota_entries": dota[:3],
                "esport_like":  esport_like[:5],
                "all_sports":   sports[:10],
            }
        except requests.HTTPError as e:
            return {"status": "error", "code": e.response.status_code,
                    "detail": e.response.text[:300]}
        except Exception as e:
            return {"status": "error", "detail": str(e)}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_team(conn: sqlite3.Connection, t1: str, t2: str):
    n1, n2 = _normalize(t1), _normalize(t2)
    rows = conn.execute(
        "SELECT external_id, name, team_1_name, team_2_name "
        "FROM matches WHERE status='not_started' ORDER BY begin_at ASC"
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
    return None, f"{t1} vs {t2}"


def collect_from_pinnacle(db_path: Path) -> int:
    if not settings.rapidapi_key:
        print("    [Pinnacle] RAPIDAPI_KEY not set — skipping")
        return 0

    client = PinnacleClient()
    try:
        data = client.get_dota2_odds()
    except Exception as e:
        print(f"    [Pinnacle] Fetch error: {e}")
        return 0

    if not data:
        print("    [Pinnacle] No Dota 2 events")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0

    for item in data:
        matchup = item["matchup"]
        odds_data = item["odds"]

        # Извлечь команды
        participants = matchup.get("participants") or matchup.get("teams") or []
        if len(participants) >= 2:
            home = (participants[0].get("name") or participants[0].get("teamName") or "")
            away = (participants[1].get("name") or participants[1].get("teamName") or "")
        else:
            home = matchup.get("home") or matchup.get("homeName") or ""
            away = matchup.get("away") or matchup.get("awayName") or ""

        starts   = matchup.get("startTime") or matchup.get("starts") or matchup.get("date") or ""
        event_id = str(matchup.get("id") or matchup.get("eventId") or "")
        ext_id, match_name = _match_team(conn, home, away)

        # Извлечь moneyline из odds
        o_home = o_away = None
        if odds_data:
            # Типичная структура: odds_data["prices"] или odds_data["moneyline"]
            prices = odds_data.get("prices") or odds_data.get("moneyline") or {}
            if isinstance(prices, dict):
                o_home = prices.get("home") or prices.get("1") or prices.get("team1")
                o_away = prices.get("away") or prices.get("2") or prices.get("team2")
            elif isinstance(prices, list) and len(prices) >= 2:
                o_home = prices[0].get("price") or prices[0].get("odds")
                o_away = prices[1].get("price") or prices[1].get("odds")

        imp1 = round(1.0 / o_home, 6) if o_home else None
        imp2 = round(1.0 / o_away, 6) if o_away else None
        overround = round((imp1 or 0) + (imp2 or 0), 4) if imp1 and imp2 else None

        conn.execute(
            """INSERT INTO odds_snapshots
               (captured_at, match_external_id, match_name, match_start_at,
                source, bookmaker,
                team_1_name, team_2_name,
                team_1_odds, team_2_odds,
                team_1_implied_prob, team_2_implied_prob,
                overround, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (captured_at, ext_id or event_id, match_name, starts,
             "rapidapi-pinnacle", "pinnacle",
             home, away,
             o_home, o_away, imp1, imp2, overround,
             json.dumps(item)),
        )
        inserted += 1

    conn.commit()
    conn.close()
    print(f"    [Pinnacle] Inserted {inserted} snapshots from {len(data)} events")
    return inserted
