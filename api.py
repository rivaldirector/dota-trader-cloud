def normalize(s: str):
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


def find_match_by_names(pred, matches):
    best = None
    best_score = 0

    p1 = normalize(pred.get("team_1_name"))
    p2 = normalize(pred.get("team_2_name"))

    for m in matches:
        m1 = normalize(m.get("team_1_name"))
        m2 = normalize(m.get("team_2_name"))

        # forward match
        if (p1 in m1 and p2 in m2):
            return m

        # reverse match
        if (p1 in m2 and p2 in m1):
            return m

        score = (
            (p1 in m1 or p1 in m2) +
            (p2 in m1 or p2 in m2)
        )

        if score > best_score:
            best_score = score
            best = m

    if best_score >= 2:
        return best

    return None


def analyze_elo_edge(predictions, odds, matches):
    matched_pairs = 0
    bets = []

    for p in predictions:
        match = find_match_by_names(p, matches)

        if not match:
            continue

        matched_pairs += 1

        match_id = match["id"]

        match_odds = [o for o in odds if str(o.get("match_id")) == str(match_id)]

        if not match_odds:
            continue

        best_odds = match_odds[0]

        predicted = p.get("predicted_team")
        t1 = match.get("team_1_name")
        t2 = match.get("team_2_name")

        if predicted == t1:
            odds_val = best_odds.get("team_1_odds")
        else:
            odds_val = best_odds.get("team_2_odds")

        if not odds_val:
            continue

        bets.append({
            "match_id": match_id,
            "predicted": predicted,
            "odds": odds_val
        })

    profit = sum([(b["odds"] - 1) for b in bets])
    roi = (profit / len(bets)) * 100 if bets else 0

    return {
        "analysis": "elo_edge_v2_fixed",
        "matched_pairs": matched_pairs,
        "bets": len(bets),
        "roi": round(roi, 2),
        "debug": {
            "predictions": len(predictions),
            "odds": len(odds),
            "matches": len(matches)
        }
    }
