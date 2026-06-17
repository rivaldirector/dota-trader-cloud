"""
Odds collector: The Odds API + DotaScore (если есть odds).

Логика:
  - append-only: каждый snapshot — новая строка, никогда не UPDATE
  - сохраняем raw_json для воспроизводимости
  - матчим по имени команды (fuzzy) к matches.external_id
"""
import json
import re
import sqlite3
import requests
from datetime import datetime, timezone
from pathlib import Path

from config import settings


# ── The Odds API ──────────────────────────────────────────────────────────────

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY     = "esports_dota2"


def _fetch_odds_api() -> list[dict]:
    """Fetch current prematch odds from The Odds API. Returns raw event list."""
    if not settings.odds_api_key:
        raise RuntimeError("ODDS_API_KEY not set in .env")
    r = requests.get(
        f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds",
        params={
            "apiKey":      settings.odds_api_key,
            "regions":     "eu,uk,us",
            "markets":     "h2h",
            "oddsFormat":  "decimal",
            "dateFormat":  "iso",
        },
        timeout=20,
    )
    remaining = r.headers.get("x-requests-remaining", "?")
    used      = r.headers.get("x-requests-used", "?")
    print(f"    [OddsAPI] used={used} remaining={remaining}")
    r.raise_for_status()
    return r.json()


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation — for fuzzy team name matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_team(db_conn: sqlite3.Connection, t1: str, t2: str):
    """
    Try to find a match in our matches table by team names.
    Returns (external_id, match_name) or (None, None).
    """
    n1, n2 = _normalize(t1), _normalize(t2)
    rows = db_conn.execute(
        "SELECT external_id, name, team_1_name, team_2_name, begin_at "
        "FROM matches WHERE status='not_started' ORDER BY begin_at ASC"
    ).fetchall()
    for r in rows:
        d1 = _normalize(r["team_1_name"] or "")
        d2 = _normalize(r["team_2_name"] or "")
        if (d1 == n1 and d2 == n2) or (d1 == n2 and d2 == n1):
            return r["external_id"], r["name"]
    # looser: substring match
    for r in rows:
        d1 = _normalize(r["team_1_name"] or "")
        d2 = _normalize(r["team_2_name"] or "")
        if (n1 in d1 or d1 in n1) and (n2 in d2 or d2 in n2):
            return r["external_id"], r["name"]
        if (n2 in d1 or d1 in n2) and (n1 in d2 or d2 in n1):
            return r["external_id"], r["name"]
    return None, f"{t1} vs {t2}"


def collect_from_odds_api(db_path: Path) -> int:
    """
    Pull current odds from The Odds API and append to odds_snapshots.
    Returns number of rows inserted.
    """
    events = _fetch_odds_api()
    if not events:
        print("    [OddsAPI] No events returned")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0

    for event in events:
        home      = event.get("home_team", "")
        away      = event.get("away_team", "")
        start_at  = event.get("commence_time", "")
        ext_id, match_name = _match_team(conn, home, away)

        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            # Store even without odds — at least we know the event exists
            conn.execute(
                """INSERT INTO odds_snapshots
                   (captured_at, match_external_id, match_name, match_start_at,
                    source, bookmaker, team_1_name, team_2_name, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (captured_at, ext_id, match_name, start_at,
                 "the-odds-api", "none", home, away, json.dumps(event)),
            )
            inserted += 1
            continue

        for bm in bookmakers:
            bm_key = bm.get("key", "")
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                # Map outcomes to home/away
                odds_map = {o["name"]: o["price"] for o in outcomes}
                o1 = odds_map.get(home)
                o2 = odds_map.get(away)
                if o1 is None or o2 is None:
                    # fallback: first two
                    prices = [o["price"] for o in outcomes[:2]]
                    o1, o2 = prices[0], prices[1]

                imp1 = round(1.0 / o1, 6) if o1 else None
                imp2 = round(1.0 / o2, 6) if o2 else None
                overround = round((imp1 or 0) + (imp2 or 0), 4)

                conn.execute(
                    """INSERT INTO odds_snapshots
                       (captured_at, match_external_id, match_name, match_start_at,
                        source, bookmaker,
                        team_1_name, team_2_name,
                        team_1_odds, team_2_odds,
                        team_1_implied_prob, team_2_implied_prob,
                        overround, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (captured_at, ext_id, match_name, start_at,
                     "the-odds-api", bm_key,
                     home, away,
                     o1, o2, imp1, imp2, overround,
                     json.dumps({"event_id": event.get("id"), "bookmaker": bm})),
                )
                inserted += 1

    conn.commit()
    conn.close()
    print(f"    [OddsAPI] Inserted {inserted} snapshots from {len(events)} events")
    return inserted


# ── DotaScore ────────────────────────────────────────────────────────────────

def collect_from_dotascore(db_path: Path) -> int:
    """
    Pull odds from DotaScore API (if available).
    Returns number of rows inserted, 0 if API has no odds.
    """
    if not settings.dotascore_api_key:
        return 0

    from adapters.dotascore import DotaScoreClient
    client = DotaScoreClient()

    try:
        matches = client.get_upcoming_matches()
    except Exception as e:
        print(f"    [DotaScore] Error fetching matches: {e}")
        return 0

    if not matches:
        print("    [DotaScore] No upcoming matches returned")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0

    for m in matches:
        # Try to extract odds — field names vary by API
        odds_data = m.get("odds") or m.get("markets") or []
        if not odds_data:
            continue

        # Extract team names (common field names)
        t1 = (m.get("team1") or m.get("home_team") or {}).get("name", "")
        t2 = (m.get("team2") or m.get("away_team") or {}).get("name", "")
        start_at = m.get("begin_at") or m.get("start_time") or m.get("date", "")
        ext_id = str(m.get("id") or m.get("match_id") or "")
        match_name = m.get("name") or f"{t1} vs {t2}"

        for odd_entry in (odds_data if isinstance(odds_data, list) else [odds_data]):
            bm = odd_entry.get("bookmaker") or odd_entry.get("provider") or "dotascore"
            o1 = odd_entry.get("odds1") or odd_entry.get("team1_odds")
            o2 = odd_entry.get("odds2") or odd_entry.get("team2_odds")
            if not o1 or not o2:
                continue

            imp1 = round(1.0 / o1, 6)
            imp2 = round(1.0 / o2, 6)
            overround = round(imp1 + imp2, 4)

            conn.execute(
                """INSERT INTO odds_snapshots
                   (captured_at, match_external_id, match_name, match_start_at,
                    source, bookmaker,
                    team_1_name, team_2_name,
                    team_1_odds, team_2_odds,
                    team_1_implied_prob, team_2_implied_prob,
                    overround, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (captured_at, ext_id, match_name, start_at,
                 "dotascore", str(bm),
                 t1, t2, o1, o2, imp1, imp2, overround,
                 json.dumps(m)),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(f"    [DotaScore] Inserted {inserted} snapshots from {len(matches)} matches")
    return inserted


# ── Unified collect ───────────────────────────────────────────────────────────

def collect_all(db_path: Path) -> dict:
    """Run all collectors. Returns {source: rows_inserted}."""
    results = {}

    # BetsAPI — основной источник (Dota 2, sport_id=151)
    try:
        from adapters.betsapi import collect_live_dota2
        results["betsapi"] = collect_live_dota2(db_path)
    except Exception as e:
        print(f"    [BetsAPI] FAILED: {e}")
        results["betsapi"] = 0

    # The Odds API — только если ключ задан (нет esports на free плане)
    if settings.odds_api_key:
        try:
            results["the-odds-api"] = collect_from_odds_api(db_path)
        except Exception as e:
            print(f"    [OddsAPI] FAILED: {e}")
            results["the-odds-api"] = 0

    # DotaScore — в backlog, пропускаем
    # try:
    #     results["dotascore"] = collect_from_dotascore(db_path)
    # except Exception as e:
    #     results["dotascore"] = 0

    return results
