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

    if not raw:
        return []

    return json.loads(raw)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/tasks")
def create_task(body: TaskRequest):
    rows = sb(
        "POST",
        "research_queue",
        [
            {
                "priority": body.priority,
                "status": "todo",
                "assigned_to": body.assigned_to,
                "task": body.task,
            }
        ],
    )

    return {
        "created": True,
        "task": rows[0] if rows else None,
    }


@app.get("/tasks/latest")
def latest_tasks():
    return sb(
        "GET",
        "research_queue?select=id,created_at,priority,status,assigned_to,task&order=id.desc&limit=10",
    )


@app.get("/db/summary")
def db_summary():
    return {
        "matches_sample": sb(
            "GET",
            "matches?select=id,begin_at,league_name,team_1_name,team_2_name&limit=3",
        ),
        "current_odds_sample": sb(
            "GET",
            "current_odds?select=*&limit=3",
        ),
        "predictions_sample": sb(
            "GET",
            "predictions?select=*&limit=3",
        ),
        "research_queue_sample": sb(
            "GET",
            "research_queue?select=*&limit=3",
        ),
    }


@app.get("/analysis/elo-edge")
def elo_edge_analysis():
    predictions = sb(
        "GET",
        "predictions?select=match_id,team_1_prob,team_2_prob&limit=10000",
    )

    odds = sb(
        "GET",
        "current_odds?select=match_id,bookmaker,team_1_name,team_2_name,team_1_odds,team_2_odds&limit=10000",
    )

    matches = sb(
        "GET",
        "matches?select=id,winner_id,winner_type,opponents,status&limit=10000",
    )

    pred_by_match = {
        p["match_id"]: p
        for p in predictions
        if p.get("match_id")
    }

    match_by_id = {
        m["id"]: m
        for m in matches
        if m.get("id")
    }

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

    def get_winner_name(match):
        winner_id = match.get("winner_id")
        opponents = match.get("opponents") or []

        for item in opponents:
            opponent = item.get("opponent") or {}
            if opponent.get("id") == winner_id:
                return opponent.get("name")

        return None

    def same_team(a, b):
        if not a or not b:
            return False

        return str(a).lower().strip() == str(b).lower().strip()

    for o in odds:
        match_id = o.get("match_id")
        pred = pred_by_match.get(match_id)
        match = match_by_id.get(match_id)

        if not pred or not match:
            continue

        winner_name = get_winner_name(match)

        if not winner_name:
            continue

        for side in [1, 2]:
            model_prob = pred.get(f"team_{side}_prob")
            odd = o.get(f"team_{side}_odds")
            team_name = o.get(f"team_{side}_name")

            if not model_prob or not odd or odd <= 1:
                continue

            implied_prob = 1 / odd
            edge = model_prob - implied_prob
            b = edge_bin(edge)

            if not b:
                continue

            won = same_team(winner_name, team_name)
            profit = odd - 1 if won else -1

            bins[b].append(
                {
                    "match_id": match_id,
                    "bookmaker": o.get("bookmaker"),
                    "team": team_name,
                    "winner": winner_name,
                    "odd": odd,
                    "model_prob": model_prob,
                    "implied_prob": implied_prob,
                    "edge": edge,
                    "won": won,
                    "profit": profit,
                }
            )

    result = {}

    for b, bets in bins.items():
        if not bets:
            result[b] = {
                "bets": 0,
                "winrate": None,
                "roi": None,
                "avg_odds": None,
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
        "source": "current_odds",
        "bins": result,
    }
