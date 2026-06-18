import os
import json
import urllib.request

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.0",
    servers=[
        {
            "url": "https://dota-trader-cloud-production.up.railway.app",
            "description": "Production",
        }
    ],
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

    raw = urllib.request.urlopen(req).read().decode()
    return json.loads(raw) if raw else []


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
    predictions = sb(
        "GET",
        "predictions?select=match_id,predicted_probability,is_win,fair_odds_1,fair_odds_2,confidence,predicted_team,winner_name&limit=10000",
    )

    odds = sb(
        "GET",
        "current_odds?select=match_id,bookmaker,team_1_name,team_2_name,team_1_odds,team_2_odds&limit=10000",
    )

    odds_by_match = {}
    for o in odds:
        mid = o.get("match_id")
        if not mid:
            continue
        odds_by_match.setdefault(mid, []).append(o)

    bins = {
        "0-2%": [],
        "2-4%": [],
        "4-6%": [],
        "6-8%": [],
        "8-10%": [],
        "10%+": [],
    }

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

    def norm(x):
        return str(x or "").lower().strip()

    for p in predictions:
        mid = p.get("match_id")
        model_prob = p.get("predicted_probability")
        predicted_team = p.get("predicted_team")
        is_win = p.get("is_win")

        if not mid or not model_prob or is_win is None:
            continue

        for o in odds_by_match.get(mid, []):
            odd = None

            if norm(predicted_team) == norm(o.get("team_1_name")):
                odd = o.get("team_1_odds")
            elif norm(predicted_team) == norm(o.get("team_2_name")):
                odd = o.get("team_2_odds")

            if not odd or odd <= 1:
                continue

            implied_prob = 1 / odd
            edge = model_prob - implied_prob
            b = edge_bin(edge)

            if not b:
                continue

            profit = odd - 1 if is_win else -1

            bins[b].append({
                "match_id": mid,
                "bookmaker": o.get("bookmaker"),
                "team": predicted_team,
                "odd": odd,
                "model_prob": model_prob,
                "implied_prob": implied_prob,
                "edge": edge,
                "won": bool(is_win),
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
        "bins": result,
    }
