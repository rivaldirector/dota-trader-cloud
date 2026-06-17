from datetime import datetime, timedelta, timezone
from config import settings
from storage.db import Database
from models.team_rating import build_team_ratings, predict_team_a_win

db = Database(settings.database_path)
ratings = build_team_ratings(db)

db.execute("""
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    match_external_id TEXT UNIQUE,
    match_name TEXT,
    begin_at TEXT,
    team_1_name TEXT,
    team_2_name TEXT,
    predicted_team TEXT,
    predicted_probability REAL,
    fair_odds REAL,
    confidence TEXT,
    status TEXT DEFAULT 'pending',
    winner_name TEXT,
    is_win INTEGER
);
""")

now = datetime.now(timezone.utc)
until = now + timedelta(hours=24)

rows = db.fetchall("""
SELECT external_id, name, begin_at, team_1_name, team_2_name
FROM matches
WHERE status='not_started'
AND team_1_name IS NOT NULL
AND team_2_name IS NOT NULL
""")

saved = 0

print("TODAY PREDICTIONS\n")

for r in rows:
    if not r["begin_at"]:
        continue

    begin = datetime.fromisoformat(r["begin_at"].replace("Z", "+00:00"))

    if not (now <= begin <= until):
        continue

    t1 = r["team_1_name"]
    t2 = r["team_2_name"]

    if t1 not in ratings or t2 not in ratings:
        continue

    ra = ratings[t1]
    rb = ratings[t2]

    p1 = predict_team_a_win(ra, rb)
    p2 = 1 - p1

    if p1 >= p2:
        pick = t1
        prob = p1
    else:
        pick = t2
        prob = p2

    fair_odds = 1 / prob
    min_matches = min(ra["matches"], rb["matches"])

    if min_matches >= 15:
        confidence = "HIGH"
    elif min_matches >= 8:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    db.execute("""
    INSERT OR REPLACE INTO predictions (
        match_external_id, match_name, begin_at,
        team_1_name, team_2_name,
        predicted_team, predicted_probability,
        fair_odds, confidence, status
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
    """, (
        r["external_id"],
        r["name"],
        r["begin_at"],
        t1,
        t2,
        pick,
        prob,
        fair_odds,
        confidence
    ))

    saved += 1

    print(f"{r['begin_at']} | {r['name']}")
    print(f"Pick: {pick} | Model: {prob*100:.2f}% | Fair odds: {fair_odds:.2f} | {confidence}")
    print()

print("Saved predictions:", saved)
