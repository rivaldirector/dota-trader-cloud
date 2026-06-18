import os, json, urllib.request
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.0",
    servers=[
        {
            "url": "https://dota-trader-cloud-production.up.railway.app",
            "description": "Production"
        }
    ]
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ORCHESTRATOR_API_KEY = os.getenv("ORCHESTRATOR_API_KEY")

class TaskRequest(BaseModel):
    task: str
    priority: int = 1000
    assigned_to: str = "GPT"

def check_auth(
    authorization: str | None = None,
    x_api_key: str | None = None,
):
    key = None

    if x_api_key:
        key = x_api_key

    elif authorization:
        if authorization.startswith("Bearer "):
            key = authorization.replace("Bearer ", "").strip()
        else:
            key = authorization.strip()

    if not ORCHESTRATOR_API_KEY or key != ORCHESTRATOR_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

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
    return json.loads(urllib.request.urlopen(req).read().decode() or "[]")

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/tasks")
def create_task(
    body: TaskRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    check_auth(authorization=authorization, x_api_key=x_api_key)

    rows = sb("POST", "research_queue", [{
        "priority": body.priority,
        "status": "todo",
        "assigned_to": body.assigned_to,
        "task": body.task,
    }])

    return {"created": True, "task": rows[0] if rows else None}

@app.get("/tasks/latest")
def latest_tasks(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    check_auth(authorization=authorization, x_api_key=x_api_key)

    return sb(
        "GET",
        "research_queue?select=id,created_at,priority,status,assigned_to,task,result&order=id.desc&limit=10"
    )

@app.get("/db/summary")
def db_summary(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    check_auth(authorization=authorization, x_api_key=x_api_key)

    return {
        "matches_sample": sb("GET", "matches?select=*&limit=3"),
        "current_odds_sample": sb("GET", "current_odds?select=*&limit=3"),
        "predictions_sample": sb("GET", "predictions?select=*&limit=3"),
    }
