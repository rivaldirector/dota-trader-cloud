from collections import defaultdict
from math import pow
from config import settings
from storage.db import Database

db = Database(settings.database_path)

K = 32
START_ELO = 1500

def expected_score(ra, rb):
    return 1 / (1 + pow(10, (rb - ra) / 400))

def update_elo(ra, rb, result_a):
    ea = expected_score(ra, rb)
    eb = 1 - ea
    return (
        ra + K * (result_a - ea),
        rb + K * ((1 - result_a) - eb)
    )

matches = db.fetchall("""
SELECT begin_at, team_1_name, team_2_name, winner_name
FROM matches
WHERE status='finished'
AND winner_name IS NOT NULL
ORDER BY begin_at ASC
""")

for min_games_filter in [0, 3, 5, 8, 10, 15]:
    elos = defaultdict(lambda: START_ELO)
    games = defaultdict(int)

    total = 0
    wins = 0

    for m in matches:
        t1 = m["team_1_name"]
        t2 = m["team_2_name"]
        winner = m["winner_name"]

        p1 = expected_score(elos[t1], elos[t2])
        pick = t1 if p1 >= 0.5 else t2

        if games[t1] >= min_games_filter and games[t2] >= min_games_filter:
            total += 1
            if pick == winner:
                wins += 1

        result_a = 1 if winner == t1 else 0
        elos[t1], elos[t2] = update_elo(elos[t1], elos[t2], result_a)

        games[t1] += 1
        games[t2] += 1

    wr = wins / total if total else 0
    print(f"min_games >= {min_games_filter:2}: {wins}/{total} = {wr:.1%}")
