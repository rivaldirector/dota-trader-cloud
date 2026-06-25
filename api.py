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

# -------------------
# ENV (CRITICAL)
# -------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Missing Supabase env variables")


# -------------------
# SUPABASE CLIENT
# -------------------
def sb(method, path, payload=None):
    try:
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

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------
# HEALTH
# -------------------
@app.get("/health")
def health():
    return {"ok": True}


# -------------------
# TASKS
# -------------------
class TaskRequest(BaseModel):
    task: str
    priority: int = 1000
    assigned_to: str = "GPT"


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


# -------------------
# DB DEBUG
# -------------------
@app.get("/db/summary")
def db_summary():
    return {
        "matches": sb("GET", "matches?select=*&limit=3"),
        "odds": sb("GET", "current_odds?select=*&limit=3"),
        "predictions": sb("GET", "predictions?select=*&limit=3"),
        "queue": sb("GET", "research_queue?select=*&limit=3"),
    }


# -------------------
# ELO EDGE (FIXED SAFE VERSION)
# -------------------
@app.get("/analysis/elo-edge")
def elo_edge():
    predictions = sb("GET", "predictions?select=*&limit=1000")
    odds = sb("GET", "current_odds?select=*&limit=1000")

    if not predictions or not odds:
        return {
            "analysis": "elo_edge",
            "error": "no_data",
            "predictions": len(predictions),
            "odds": len(odds),
        }

    odds_map = {}
    for o in odds:
        mid = o.get("match_id")
        if mid:
            odds_map.setdefault(str(mid), []).append(o)

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

    def edge_bin(e):
        e = e * 100
        if e < 2: return "0-2%"
        if e < 4: return "2-4%"
        if e < 6: return "4-6%"
        if e < 8: return "6-8%"
        if e < 10: return "8-10%"
        return "10%+"

    for p in predictions:
        mid = p.get("match_id")
        if not mid:
            continue

        model_prob = p.get("predicted_probability")
        team = p.get("predicted_team")
        is_win = p.get("is_win")

        if model_prob is None or team is None or is_win is None:
            continue

        try:
            model_prob = float(model_prob)
        except:
            continue

        for o in odds_map.get(str(mid), []):
            odd = None

            if norm(team) == norm(o.get("team_1_name")):
                odd = o.get("team_1_odds")
            elif norm(team) == norm(o.get("team_2_name")):
                odd = o.get("team_2_odds")
            else:
                continue

            try:
                odd = float(odd)
            except:
                continue

            if odd <= 1:
                continue

            implied = 1 / odd
            edge = model_prob - implied

            b = edge_bin(edge)

            bins[b].append({
                "match_id": mid,
                "team": team,
                "odd": odd,
                "model_prob": model_prob,
                "implied": implied,
                "edge": edge,
                "win": bool(is_win),
                "profit": (odd - 1) if is_win else -1
            })

    result = {}
    for k, v in bins.items():
        if not v:
            result[k] = {"bets": 0, "roi": 0}
            continue

        bets = len(v)
        wins = sum(1 for x in v if x["win"])
        profit = sum(x["profit"] for x in v)

        result[k] = {
            "bets": bets,
            "winrate": round(wins / bets * 100, 2),
            "roi": round(profit / bets * 100, 2),
        }

    return {
        "analysis": "elo_edge_fixed",
        "predictions": len(predictions),
        "odds": len(odds),
        "bins": result
    }


# -------------------
# DEBUG LINKS
# -------------------
@app.get("/debug/data-links")
def debug_links():
    return {
        "predictions": sb("GET", "predictions?select=*&limit=5"),
        "odds": sb("GET", "current_odds?select=*&limit=5"),
        "matches": sb("GET", "matches?select=*&limit=5"),
    }
