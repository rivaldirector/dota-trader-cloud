import os, json, sqlite3, requests
from dotenv import load_dotenv

load_dotenv(".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
DB_PATH = os.getenv("DATABASE_PATH", "storage/dota_trader.sqlite3")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("ERROR: SUPABASE_URL или SUPABASE_ANON_KEY не найдены")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal"
}

def load_json(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return {"raw_text": str(s)}

def clean_dt(v):
    if not v:
        return None
    v = str(v)
    if "_" in v:
        v = v.split("_")[0]
    return v

def post(table, rows, conflict="id"):
    if not rows:
        print(f"{table}: 0 rows")
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}"
    for i in range(0, len(rows), 1000):
        batch = rows[i:i+1000]
        r = requests.post(url, headers=headers, data=json.dumps(batch, default=str), timeout=60)
        if r.status_code not in (200, 201, 204):
            print("ERROR", table, r.status_code)
            print(r.text[:1500])
            raise SystemExit(1)
        print(f"{table}: uploaded {i + len(batch)}/{len(rows)}")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

matches = []
for r in conn.execute("select * from matches"):
    matches.append({
        "id": str(r["external_id"] or r["id"]),
        "source": r["source"],
        "name": r["name"],
        "league_name": r["league_name"],
        "team_1_name": r["team_1_name"],
        "team_2_name": r["team_2_name"],
        "begin_at": clean_dt(r["begin_at"]),
        "status": r["status"],
        "raw": load_json(r["raw_json"]),
    })

predictions = []
for r in conn.execute("select * from predictions"):
    predictions.append({
        "match_id": str(r["match_external_id"]) if r["match_external_id"] else None,
        "model_version": "local_v2",
        "team_1_prob": r["predicted_probability"] if r["predicted_team"] == r["team_1_name"] else None,
        "team_2_prob": r["predicted_probability"] if r["predicted_team"] == r["team_2_name"] else None,
        "fair_odds_1": r["fair_odds"] if r["predicted_team"] == r["team_1_name"] else None,
        "fair_odds_2": r["fair_odds"] if r["predicted_team"] == r["team_2_name"] else None,
        "confidence": r["confidence"],
        "created_at": clean_dt(r["created_at"]),
        "raw": dict(r),
    })

odds = []
for r in conn.execute("select * from odds_snapshots"):
    odds.append({
        "match_id": str(r["match_external_id"]) if r["match_external_id"] else None,
        "bookmaker": r["bookmaker"] or r["source"],
        "market": "winner",
        "team_name": r["team_1_name"],
        "odds": r["team_1_odds"],
        "collected_at": clean_dt(r["captured_at"]),
        "raw": dict(r),
    })
    odds.append({
        "match_id": str(r["match_external_id"]) if r["match_external_id"] else None,
        "bookmaker": r["bookmaker"] or r["source"],
        "market": "winner",
        "team_name": r["team_2_name"],
        "odds": r["team_2_odds"],
        "collected_at": clean_dt(r["captured_at"]),
        "raw": dict(r),
    })

bets = []
for r in conn.execute("select * from bets"):
    bets.append({
        "match_id": None,
        "strategy_name": r["source"],
        "selection": r["selection"],
        "odds": r["odds"],
        "stake": r["stake"],
        "status": r["status"],
        "pnl": r["profit"],
        "placed_at": clean_dt(r["created_at"]),
        "raw": dict(r),
    })

print("LOCAL")
print("matches:", len(matches))
print("predictions:", len(predictions))
print("odds_snapshots:", len(odds))
print("paper_bets:", len(bets))

post("matches", matches, "id")
post("predictions", predictions, "id")
post("odds_snapshots", odds, "id")
post("paper_bets", bets, "id")

print("DONE: Supabase sync complete")
