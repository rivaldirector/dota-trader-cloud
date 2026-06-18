import os
import json
import urllib.request
import urllib.error
from rapidfuzz import fuzz

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.2",
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


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


def norm(s: str) -> str:
    if not s:
        return ""
    return (
        str(s)
        .lower()
        .replace("team", "")
        .replace("-", " ")
        .replace("_", " ")
        .replace(".", " ")
        .strip()
    )


def link_prediction_to_odds(pred, odds_list):
    pred_team = (pred.get("raw", {}).get("predicted_team") or "").lower().strip()

    best = None
    best_score = 0

    for o in odds_list:
        t1 = (o.get("team_1_name") or "").lower().strip()
        t2 = (o.get("team_2_name") or "").lower().strip()

        s1 = fuzz.token_sort_ratio(pred_team, t1)
        s2 = fuzz.token_sort_ratio(pred_team, t2)

        if s1 > best_score:
            best_score = s1
            best = o

        if s2 > best_score:
            best_score = s2
            best = o

    if best_score < 80:
        return None

    return best


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/analysis/elo_edge")
def elo_edge():
    predictions = sb("GET", "predictions?select=*&limit=200")
    odds = sb("GET", "odds?select=*&limit=500")

    bets = []

    for p in predictions:
        match = link_prediction_to_odds(p, odds)

        if match:
            bets.append({
                "prediction": p,
                "odds": match
            })

    wins = 0
    profit = 0

    for b in bets:
        o = b["odds"]
        odds_val = o.get("team_2_odds") or o.get("team_1_odds") or 1

        profit += (odds_val - 1)
        wins += 1

    roi = (profit / len(bets)) * 100 if bets else 0

    return {
        "analysis": "elo_edge_fixed",
        "matched": len(bets),
        "bets": len(bets),
        "roi": round(roi, 2)
    }


@app.get("/debug/data-links")
def debug():
    return {
        "predictions": sb("GET", "predictions?select=*&limit=5"),
        "odds": sb("GET", "current_odds?select=*&limit=5"),
        "matches": sb("GET", "matches?select=*&limit=5"),
    }
