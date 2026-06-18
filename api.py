import os
import json
import urllib.request
import urllib.error

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.1",
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")


# -----------------------
# BASE SUPABASE CLIENT
# -----------------------
def sb(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()

    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=data,
        method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )

    try:
        raw = urllib.request.urlopen(req).read().decode()
        return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=500, detail=e.read().decode())


# -----------------------
# HEALTH
# -----------------------
@app.get("/health")
def health():
    return {"ok": True}


# -----------------------
# DEBUG LINKS
# -----------------------
@app.get("/debug/data-links")
def debug_data_links():
    return {
        "predictions": sb("GET", "predictions?select=match_id&limit=5"),
        "current_odds": sb("GET", "current_odds?select=match_id&limit=5"),
        "matches": sb("GET", "matches?select=id,match_external_id&limit=5"),
    }


# -----------------------
# MATCH LINKS SYNC
# -----------------------
@app.post("/sync/match-links")
def sync_match_links():
    matches = sb("GET", "matches?select=id,match_external_id,name&limit=10000")

    rows = []
    for m in matches:
        if not m.get("match_external_id") or not m.get("id"):
            continue

        rows.append({
            "match_external_id": str(m["match_external_id"]),
            "match_id": str(m["id"]),
            "match_name": m.get("name"),
        })

    if not rows:
        return {"created": 0}

    return sb("POST", "match_links", rows)


# -----------------------
# ELO EDGE ANALYTICS (FIXED)
# -----------------------
@app.get("/analysis/elo-edge")
def elo_edge_analysis():

    predictions = sb("GET", "predictions?select=*&limit=10000")
    odds = sb("GET", "current_odds?select=*&limit=10000")
    links = sb("GET", "match_links?select=*&limit=10000")

    # -----------------------
    # BUILD LINK MAP
    # -----------------------
    external_to_internal = {}

    for l in links:
        if l.get("match_external_id") and l.get("match_id"):
            external_to_internal[str(l["match_external_id"])] = str(l["match_id"])

    # -----------------------
    # GROUP ODDS BY MATCH
    # -----------------------
    odds_by_match = {}
    for o in odds:
        mid = o.get("match_id")
        if mid:
            odds_by_match.setdefault(str(mid), []).append(o)

    # -----------------------
    # BINS
    # -----------------------
    bins = {
        "0-2%": [],
        "2-4%": [],
        "4-6%": [],
        "6-8%": [],
        "8-10%": [],
        "10%+": [],
    }

    def norm(x):
        return str(x or "").lower().strip()

    def to_bool(x):
        if isinstance(x, bool):
            return x
        if isinstance(x, str):
            return x.lower() in ["true", "1", "yes", "win", "won"]
        return bool(x)

    def edge_bin(edge):
        e = edge * 100
        if e < 0:
            return None
        if e < 2:
            return "0-2%"
        if e < 4:
            return "2-4%"
        if e < 6:
            return "4-6%"
        if e < 8:
            return "6-8%"
        if e < 10:
            return "8-10%"
        return "10%+"

    # -----------------------
    # MAIN LOOP
    # -----------------------
    for p in predictions:

        external_id = p.get("match_id")
        if not external_id:
            continue

        mid = external_to_internal.get(str(external_id))
        if not mid:
            continue

        model_prob = p.get("predicted_probability")
        predicted_team = p.get("predicted_team")
        is_win = p.get("is_win")

        if model_prob is None or not predicted_team or is_win is None:
            continue

        try:
            model_prob = float(model_prob)
        except:
            continue

        for o in odds_by_match.get(str(mid), []):

            odd = None

            if norm(predicted_team) == norm(o.get("team_1_name")):
                odd = o.get("team_1_odds")
            elif norm(predicted_team) == norm(o.get("team_2_name")):
                odd = o.get("team_2_odds")
            else:
                continue

            try:
                odd = float(odd)
            except:
                continue

            if odd <= 1:
                continue

            implied_prob = 1 / odd
            edge = model_prob - implied_prob

            b = edge_bin(edge)
            if not b:
                continue

            won = to_bool(is_win)
            profit = odd - 1 if won else -1

            bins[b].append({
                "match_id": mid,
                "odd": odd,
                "model_prob": model_prob,
                "implied_prob": implied_prob,
                "edge": edge,
                "won": won,
                "profit": profit,
            })

    # -----------------------
    # STATS
    # -----------------------
    result = {}

    for b, bets in bins.items():

        if not bets:
            result[b] = {
                "bets": 0,
                "wins": 0,
                "winrate": None,
                "roi": None,
                "avg_odds": None
            }
            continue

        wins = sum(1 for x in bets if x["won"])
        profit = sum(x["profit"] for x in bets)
        avg_odds = sum(x["odd"] for x in bets) / len(bets)

        result[b] = {
            "bets": len(bets),
            "wins": wins,
            "winrate": round(wins / len(bets) * 100, 2),
            "roi": round(profit / len(bets) * 100, 2),
            "avg_odds": round(avg_odds, 3),
        }

    return {
        "analysis": "elo_edge",
        "predictions_loaded": len(predictions),
        "odds_loaded": len(odds),
        "bins": result,
    }
