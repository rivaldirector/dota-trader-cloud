from collections import defaultdict
from math import pow
from config import settings
from storage.db import Database

db = Database(settings.database_path)

K = 32
START_ELO = 1500
MIN_PROB_TO_PICK = 0.50

def expected_score(ra, rb):
    return 1 / (1 + pow(10, (rb - ra) / 400))

def update_elo(ra, rb, result_a):
    ea = expected_score(ra, rb)
    eb = 1 - ea
    new_ra = ra + K * (result_a - ea)
    new_rb = rb + K * ((1 - result_a) - eb)
    return new_ra, new_rb

matches = db.fetchall("""
SELECT external_id, name, begin_at, status, team_1_name, team_2_name, winner_name
FROM matches
WHERE status='finished'
AND begin_at IS NOT NULL
AND team_1_name IS NOT NULL
AND team_2_name IS NOT NULL
AND winner_name IS NOT NULL
ORDER BY begin_at ASC
""")

by_day = defaultdict(list)

for m in matches:
    day = m["begin_at"][:10]
    by_day[day].append(m)

elos = defaultdict(lambda: START_ELO)
team_games = defaultdict(int)

total = 0
wins = 0

by_conf = {
    "HIGH": [0, 0],
    "MEDIUM": [0, 0],
    "LOW": [0, 0],
}

print("DAILY BACKTEST\n")

for day in sorted(by_day.keys()):
    day_matches = by_day[day]

    day_total = 0
    day_wins = 0

    predictions = []

    # 1. Сначала предсказываем день, не обновляя Elo матчами этого дня
    for m in day_matches:
        t1 = m["team_1_name"]
        t2 = m["team_2_name"]
        winner = m["winner_name"]

        if t1 not in elos or t2 not in elos:
            continue

        p1 = expected_score(elos[t1], elos[t2])
        p2 = 1 - p1

        if p1 >= p2:
            pick = t1
            prob = p1
        else:
            pick = t2
            prob = p2

        min_games = min(team_games[t1], team_games[t2])

        if min_games >= 15:
            confidence = "HIGH"
        elif min_games >= 8:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        is_win = pick == winner

        predictions.append((m["name"], pick, winner, prob, confidence, is_win))

    # 2. Считаем результат дня
    for name, pick, winner, prob, confidence, is_win in predictions:
        total += 1
        day_total += 1

        if is_win:
            wins += 1
            day_wins += 1

        by_conf[confidence][1] += 1
        if is_win:
            by_conf[confidence][0] += 1

    # 3. Теперь обновляем Elo уже реальными результатами этого дня
    for m in day_matches:
        t1 = m["team_1_name"]
        t2 = m["team_2_name"]
        winner = m["winner_name"]

        result_t1 = 1 if winner == t1 else 0

        elos[t1], elos[t2] = update_elo(elos[t1], elos[t2], result_t1)

        team_games[t1] += 1
        team_games[t2] += 1

    if day_total:
        print(f"{day}: {day_wins}/{day_total} = {day_wins/day_total:.1%}")

print("\nTOTAL BACKTEST")
print(f"{wins}/{total} = {wins/total:.1%}" if total else "No predictions")

print("\nBY CONFIDENCE")
for conf, (w, t) in by_conf.items():
    if t:
        print(f"{conf}: {w}/{t} = {w/t:.1%}")
    else:
        print(f"{conf}: 0/0")

print("\nFINAL TOP ELO")
for team, elo in sorted(elos.items(), key=lambda x: x[1], reverse=True)[:30]:
    print(f"{team:30} {elo:.1f} | games={team_games[team]}")
