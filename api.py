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
