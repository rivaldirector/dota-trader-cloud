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
SELECT
    external_id,
    name,
    begin_at,
    status,
    team_1_name,
    team_2_name,
    winner_name,
    tournament_name
FROM matches
WHERE status='finished'
AND winner_name IS NOT NULL
ORDER BY begin_at ASC
""")

elos = defaultdict(lambda: START_ELO)

prob_buckets = defaultdict(lambda: [0, 0])
tournament_stats = defaultdict(lambda: [0, 0])

for m in matches:
    t1 = m["team_1_name"]
    t2 = m["team_2_name"]
    winner = m["winner_name"]

    p1 = expected_score(elos[t1], elos[t2])
    p2 = 1 - p1

    if p1 >= p2:
        pick = t1
        prob = p1
    else:
        pick = t2
        prob = p2

    win = int(pick == winner)

    if prob >= 0.75:
        bucket = "75%+"
    elif prob >= 0.65:
        bucket = "65-75%"
    elif prob >= 0.55:
        bucket = "55-65%"
    else:
        bucket = "<55%"

    prob_buckets[bucket][0] += win
    prob_buckets[bucket][1] += 1

    tournament = m["tournament_name"] or "Unknown"
    tournament_stats[tournament][0] += win
    tournament_stats[tournament][1] += 1

    result_a = 1 if winner == t1 else 0
    elos[t1], elos[t2] = update_elo(
        elos[t1],
        elos[t2],
        result_a
    )

print("\nWINRATE BY PROBABILITY\n")

for bucket, (wins, total) in sorted(prob_buckets.items()):
    wr = wins / total if total else 0
    print(f"{bucket:10} {wins}/{total} = {wr:.1%}")

print("\nBEST TOURNAMENTS (min 10 matches)\n")

rows = []

for tournament, (wins, total) in tournament_stats.items():
    if total >= 10:
        rows.append((wins / total, wins, total, tournament))

rows.sort(reverse=True)

for wr, wins, total, tournament in rows[:20]:
    print(f"{wr:.1%} | {wins}/{total} | {tournament}")

print("\nWORST TOURNAMENTS (min 10 matches)\n")

for wr, wins, total, tournament in rows[-20:]:
    print(f"{wr:.1%} | {wins}/{total} | {tournament}")
