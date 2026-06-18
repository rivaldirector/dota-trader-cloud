import os
import json
import urllib.request
import urllib.error

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.0",
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")


# -----------------------
# BASE CLIENT
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
# MATCH NORMALIZER
# -----------------------
def norm(s):
    if not s:
        return ""
    return (
        str(s)
        .lower()
        .replace("team", "")
        .replace("esports", "")
        .replace("e-sports", "")
        .replace(" ", "")
        .strip()
    )


# -----------------------
# MATCH LINKER (CRITICAL FIX)
# -----------------------
def link_match(pred, matches):
    p1 = norm(pred.get("team_1_name"))
    p2 = norm(pred.get("team_2_name"))

    best = None
    best_score = 0

    for m in matches:
        m1 = norm(m.get("team_1_name"))
        m2 = norm(m.get("team_2_name"))

        # exact forward match
        if p1 in m1 and p2 in m2:
            return m

        # reverse match
        if p1 in m2 and p2 in m1:
            return m

        score = (p1 in m1 or p1 in m2) + (p2 in m1 or p2 in m2)

        if score > best_score:
            best_score = score
            best = m

    if best_score >= 2:
        return best

    return None


# -----------------------
# ELO EDGE ANALYSIS (FIXED)
# -----------------------
@app.get("/analysis/elo-edge")
def elo_edge():
    predictions = sb("GET", "predictions?select=*&limit=10000")
    odds = sb("GET", "current_odds?select=*&limit=10000")
    matches = sb("GET", "matches?select=*&limit=10000")

    odds_by_match = {}
    for o in odds:
        mid = o.get("match_id")
        if mid:
            odds_by_match.setdefault(str(mid), []).append(o)

    bets = []
    matched = 0

    for p in predictions:
        match = link_match(p, matches)
        if not match:
            continue

        matched += 1
        mid = match["id"]

        match_odds = odds_by_match.get(str(mid))
        if not match_odds:
            continue

        o = match_odds[0]

        predicted = p.get("predicted_team")
        t1 = match.get("team_1_name")
        t2 = match.get("team_2_name")

        if predicted == t1:
            odd = o.get("team_1_odds")
        else:
            odd = o.get("team_2_odds")

        try:
            odd = float(odd)
        except:
            continue

        if odd <= 1:
            continue

        is_win = p.get("is_win", False)

        bets.append({
            "odd": odd,
            "win": bool(is_win)
        })

    if not bets:
        return {
            "analysis": "elo_edge_fixed",
            "matched": matched,
            "bets": 0,
            "roi": 0
        }

    profit = sum([(b["odd"] - 1) if b["win"] else -1 for b in bets])
    roi = (profit / len(bets)) * 100

    return {
        "analysis": "elo_edge_fixed",
        "matched": matched,
        "bets": len(bets),
        "roi": round(roi, 2)
    }


# -----------------------
# DEBUG
# -----------------------
@app.get("/debug/data-links")
def debug():
    return {
        "predictions": sb("GET", "predictions?select=*&limit=5"),
        "odds": sb("GET", "current_odds?select=*&limit=5"),
        "matches": sb("GET", "matches?select=*&limit=5"),
    }
