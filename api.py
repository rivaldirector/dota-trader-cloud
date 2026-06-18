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


class TaskRequest(BaseModel):
    task: str
    priority: int = 1000
    assigned_to: str = "GPT"


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
        body = e.read().decode()
        raise HTTPException(status_code=500, detail=body)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/tasks")
def create_task(body: TaskRequest):
    rows = sb("POST", "research_queue", [{
        "priority": body.priority,
        "status": "todo",
        "assigned_to": body.assigned_to,
        "task": body.task,
    }])
    return {"created": True, "task": rows[0] if rows else None}


@app.get("/db/summary")
def db_summary():
    return {
        "matches": sb("GET", "matches?select=*&limit=3"),
        "current_odds": sb("GET", "current_odds?select=*&limit=3"),
        "predictions": sb("GET", "predictions?select=*&limit=3"),
        "odds_snapshots": sb("GET", "odds_snapshots?select=*&limit=3"),
    }


@app.get("/analysis/elo-edge")
def elo_edge_analysis():

    predictions = sb("GET", "predictions?select=*&limit=10000")
    odds = sb("GET", "current_odds?select=*&limit=10000")

    def norm(x):
        return str(x or "").lower().strip()

    def norm_id(x):
        return str(x).strip()

    def valid_odds(x):
        try:
            x = float(x)
            return 1.01 <= x <= 10
        except:
            return False

    odds_by_match = {}
    for o in odds:
        mid = o.get("match_id")
        if not mid:
            continue
        mid = norm_id(mid)
        odds_by_match.setdefault(mid, []).append(o)

    bins = {
        "0-2%": [],
        "2-4%": [],
        "4-6%": [],
        "6-8%": [],
        "8-10%": [],
        "10%+": [],
    }

    def edge_bin(e):
        e = e * 100
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

    matched = 0

    for p in predictions:

        mid = p.get("match_id")
        if not mid:
            continue

        mid = norm_id(mid)

        model_prob = p.get("predicted_probability")
        team = p.get("predicted_team")
        is_win = p.get("is_win")

        if model_prob is None or not team or is_win is None:
            continue

        try:
            model_prob = float(model_prob)
        except:
            continue

        if mid not in odds_by_match:
            continue

        for o in odds_by_match[mid]:

            odd = None

            if norm(team) == norm(o.get("team_1_name")):
                odd = o.get("team_1_odds")
            elif norm(team) == norm(o.get("team_2_name")):
                odd = o.get("team_2_odds")
            else:
                continue

            if not valid_odds(odd):
                continue

            odd = float(odd)

            implied = 1 / odd
            edge = model_prob - implied

            b = edge_bin(edge)
            if not b:
                continue

            won = bool(is_win)
            profit = odd - 1 if won else -1

            bins[b].append({
                "match_id": mid,
                "team": team,
                "odd": odd,
                "model_prob": model_prob,
                "implied_prob": implied,
                "edge": edge,
                "won": won,
                "profit": profit,
            })

            matched += 1

    result = {}

    for k, bets in bins.items():

        if not bets:
            result[k] = {
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

        result[k] = {
            "bets": len(bets),
            "wins": wins,
            "winrate": round(wins / len(bets) * 100, 2),
            "roi": round(profit / len(bets) * 100, 2),
            "avg_odds": round(avg_odds, 3),
        }

    return {
        "analysis": "elo_edge_v3",
        "predictions": len(predictions),
        "odds": len(odds),
        "matched_pairs": matched,
        "bins": result,
    }


@app.get("/debug/data-links")
def debug():
    return {
        "predictions": sb("GET", "predictions?select=*&limit=5"),
        "odds": sb("GET", "current_odds?select=*&limit=5"),
        "matches": sb("GET", "matches?select=*&limit=5"),
    }
