import json
import time
from config import settings
from storage.db import Database
from adapters.pandascore import PandaScoreClient

db = Database(settings.database_path)
client = PandaScoreClient(settings.pandascore_token, settings.pandascore_base_url)

pending = db.fetchall("""
SELECT match_external_id, predicted_team
FROM predictions
WHERE status='pending'
""")

pending_ids = {str(p["match_external_id"]): p for p in pending}

updated = 0

for page in range(1, 31):
    matches = client.get_past_dota_matches(limit=100, page=page)

    for item in matches:
        match_id = str(item.get("id"))

        if match_id not in pending_ids:
            continue

        winner = item.get("winner") or {}
        winner_name = winner.get("name")
        status = item.get("status")

        if status != "finished" or not winner_name:
            continue

        pred = pending_ids[match_id]
        is_win = 1 if pred["predicted_team"] == winner_name else 0

        db.execute("""
        UPDATE predictions
        SET status='settled', winner_name=?, is_win=?
        WHERE match_external_id=?
        """, (winner_name, is_win, match_id))

        updated += 1

    time.sleep(0.3)

print("Updated settled predictions:", updated)

rows = db.fetchall("""
SELECT confidence, COUNT(*) as total, SUM(is_win) as wins
FROM predictions
WHERE status='settled'
GROUP BY confidence
ORDER BY total DESC
""")

print("\nWINRATE BY CONFIDENCE\n")

for r in rows:
    total = r["total"]
    wins = r["wins"] or 0
    wr = wins / total if total else 0
    print(f"{r['confidence']}: {wins}/{total} = {wr:.1%}")

all_rows = db.fetchone("""
SELECT COUNT(*) as total, SUM(is_win) as wins
FROM predictions
WHERE status='settled'
""")

total = all_rows["total"]
wins = all_rows["wins"] or 0
wr = wins / total if total else 0

print(f"\nTOTAL: {wins}/{total} = {wr:.1%}")
