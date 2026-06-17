import sys
from config import settings
from storage.db import Database
from engine.paper_trader import PaperTrader
from reports.report import print_report
from models.team_rating import build_team_ratings, predict_team_a_win, find_team


def get_trader():
    db = Database(settings.database_path)
    return PaperTrader(
        db=db,
        start_bank=settings.start_bank,
        currency=settings.currency,
        min_edge=settings.min_edge,
        max_stake_pct=settings.max_stake_pct
    )


def run_report():
    print_report(get_trader().db)


def run_scan_upcoming():
    db = get_trader().db
    ratings = build_team_ratings(db)

    rows = db.fetchall("""
    SELECT name, team_1_name, team_2_name, league_name, begin_at
    FROM matches
    WHERE status='not_started'
    AND team_1_name IS NOT NULL
    AND team_2_name IS NOT NULL
    ORDER BY begin_at ASC
    LIMIT 50
    """)

    print("UPCOMING MODEL SCAN\n")

    for r in rows:
        t1 = r["team_1_name"]
        t2 = r["team_2_name"]

        if t1 not in ratings or t2 not in ratings:
            print(f"SKIP: {r['name']} | no rating")
            continue

        ra = ratings[t1]
        rb = ratings[t2]

        p1 = predict_team_a_win(ra, rb)
        p2 = 1 - p1

        fair_odds_1 = 1 / p1 if p1 > 0 else 999
        fair_odds_2 = 1 / p2 if p2 > 0 else 999

        min_matches = min(ra["matches"], rb["matches"])

        if min_matches >= 15:
            confidence = "HIGH"
        elif min_matches >= 8:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        print(f"{r['begin_at']} | {r['league_name']}")
        print(f"{t1} vs {t2}")
        print(f"  {t1}: {p1 * 100:.2f}% | fair odds {fair_odds_1:.2f}")
        print(f"  {t2}: {p2 * 100:.2f}% | fair odds {fair_odds_2:.2f}")
        print(f"  Elo: {ra['elo']:.0f} vs {rb['elo']:.0f}")
        print(f"  Form L5: {ra['last5']:.0%} vs {rb['last5']:.0%}")
        print(f"  Data: {ra['matches']} matches vs {rb['matches']} matches")
        print(f"  Confidence: {confidence}")
        print()


def run_predict(team_a_query, team_b_query):
    db = get_trader().db
    ratings = build_team_ratings(db)

    team_a = find_team(ratings, team_a_query)
    team_b = find_team(ratings, team_b_query)

    if not team_a:
        print(f"Team not found: {team_a_query}")
        return

    if not team_b:
        print(f"Team not found: {team_b_query}")
        return

    ra = ratings[team_a]
    rb = ratings[team_b]

    p_a = predict_team_a_win(ra, rb)
    p_b = 1 - p_a

    print(f"{team_a} vs {team_b}\n")
    print(f"{team_a}: {p_a * 100:.2f}%")
    print(f"{team_b}: {p_b * 100:.2f}%")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "report"

    if command == "report":
        run_report()
    elif command in ("scan", "scan_upcoming"):
        run_scan_upcoming()
    elif command == "predict":
        if len(sys.argv) < 4:
            print('Usage: python3 main.py predict "Team A" "Team B"')
            return
        run_predict(sys.argv[2], sys.argv[3])
    else:
        print("Available commands: report, scan, scan_upcoming, predict")


if __name__ == "__main__":
    main()
