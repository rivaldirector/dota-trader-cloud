from config import settings
from storage.db import Database

db = Database(settings.database_path)

rows = db.fetchall("""
SELECT
    team_1_name,
    team_2_name
FROM matches
""")

games = {}

for r in rows:
    games[r["team_1_name"]] = games.get(r["team_1_name"], 0) + 1
    games[r["team_2_name"]] = games.get(r["team_2_name"], 0) + 1

counts = list(games.values())

print("Teams:", len(counts))
print("Avg games:", sum(counts)/len(counts))
print("Teams <5 games:", len([x for x in counts if x < 5]))
print("Teams <10 games:", len([x for x in counts if x < 10]))
