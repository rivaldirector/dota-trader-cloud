import os
import json
import urllib.request
import urllib.error

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.0",
    servers=[{"url": "https://dota-trader-cloud-production.up.railway.app", "description": "Production"}],
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
        raise HTTPException(status_code=500, detail={"supabase_path": path, "error": body})


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


@app.get("/tasks/latest")
def latest_tasks():
    return sb(
        "GET",
        "research_queue?select=id,created_at,priority,status,assigned_to,task&order=id.desc&limit=10",
    )


@app.get("/db/summary")
def db_summary():
    return {
        "matches_sample": sb("GET", "matches?select=*&limit=3"),
        "current_odds_sample": sb("GET", "current_odds?select=*&limit=3"),
        "predictions_sample": sb("GET", "predictions?select=*&limit=3"),
        "research_queue_sample": sb("GET", "research_queue?select=*&limit=3"),
    }


@app.get("/analysis/elo-edge")
def elo_edge_analysis():
    predictions = sb("GET", "predictions?select=*&limit=10000")
    odds = sb("GET", "current_odds?select=*&limit=10000")

    odds_by_match = {}
    for o in odds:
        mid = o.get("match_id")
        if mid is not None:
            odds_by_match.setdefault(str(mid), []).append(o)

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

    for p in predictions:
        mid = p.get("match_id")
        if mid is None:
            continue

        model_prob = p.get("predicted_probability")
        predicted_team = p.get("predicted_team")
        is_win = p.get("is_win")

        if model_prob is None or not predicted_team or is_win is None:
            continue

        try:
            model_prob = float(model_prob)
        except Exception:
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
            except Exception:
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
                "bookmaker": o.get("bookmaker"),
                "team": predicted_team,
                "odd": odd,
                "model_prob": model_prob,
                "implied_prob": implied_prob,
                "edge": edge,
                "won": won,
                "profit": profit,
            })

    result = {}

    for b, bets in bins.items():
        if not bets:
            result[b] = {"bets": 0, "wins": 0, "winrate": None, "roi": None, "avg_odds": None}
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
        "source": "predictions + current_odds",
        "predictions_loaded": len(predictions),
        "odds_loaded": len(odds),
        "bins": result,
    }

@app.get("/debug/data-links")
def debug_data_links():
    predictions = sb(
        "GET",
        "predictions?select=*&limit=10"
    )

    current_odds = sb(
        "GET",
        "current_odds?select=*&limit=10"
    )

    odds_snapshots = sb(
        "GET",
        "odds_snapshots?select=*&limit=10"
    )

    matches = sb(
        "GET",
        "matches?select=*&limit=10"
    )

    prediction_match_ids = [
        p.get("match_id")
        for p in predictions
    ]

    current_odds_match_ids = [
        o.get("match_id")
        for o in current_odds
    ]

    odds_snapshots_match_ids = [
        o.get("match_id")
        for o in odds_snapshots
    ]

    matches_ids = [
        m.get("id")
        for m in matches
    ]

    return {
        "predictions_sample": predictions,
        "current_odds_sample": current_odds,
        "odds_snapshots_sample": odds_snapshots,
        "matches_sample": matches,
        "ids": {
            "prediction_match_ids": prediction_match_ids,
            "current_odds_match_ids": current_odds_match_ids,
            "odds_snapshots_match_ids": odds_snapshots_match_ids,
            "matches_ids": matches_ids,
            "prediction_vs_current_odds_overlap": list(
                set(map(str, prediction_match_ids))
                &
                set(map(str, current_odds_match_ids))
            ),
            "prediction_vs_odds_snapshots_overlap": list(
                set(map(str, prediction_match_ids))
                &
                set(map(str, odds_snapshots_match_ids))
            ),
            "prediction_vs_matches_overlap": list(
                set(map(str, prediction_match_ids))
                &
                set(map(str, matches_ids))
            ),
        },
    }

@app.post("/sync/match-links")
def sync_match_links():
    matches = sb("GET", "matches?select=id,match_external_id,name&limit=10000")

    rows = []
    for m in matches:
        rows.append({
            "match_external_id": m.get("match_external_id"),
            "match_id": str(m.get("id")),
            "match_name": m.get("name"),
        })

    return sb("POST", "match_links", rows)
