#!/usr/bin/env python3
"""
check_pandascore_coverage.py — сколько Dota2-матчей PandaScore видит за период
17-23 июня (TI Quals + EPL + The International), и сравнение с тем что у нас
есть в betsapi_harvest.db. Ничего не пишет в БД — чисто диагностика.
"""
import os, json, urllib.request, urllib.parse, urllib.error, sqlite3

_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_DIR, '../.env')
HARVEST_DB = os.path.join(_DIR, '../storage/betsapi_harvest.db')


def load_env():
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


def get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {url}: {e.read()[:300]}")
        return []
    except Exception as e:
        print(f"  [WARN] {e} for {url}")
        return []


def fetch_all(token, since_dt, until_dt, status_filter=None):
    base = "https://api.pandascore.co/dota2/matches"
    headers = {"Authorization": f"Bearer {token}"}
    rng = f"{since_dt},{until_dt}"
    all_matches = []
    page = 1
    while page <= 30:
        params = {
            "range[scheduled_at]": rng,
            "per_page": "100",
            "page": str(page),
            "sort": "scheduled_at",
        }
        if status_filter:
            params["filter[status]"] = status_filter
        url = base + "?" + urllib.parse.urlencode(params)
        data = get_json(url, headers)
        if not data:
            break
        all_matches.extend(data)
        if len(data) < 100:
            break
        page += 1
    return all_matches


def main():
    env = load_env()
    token = env.get('PANDASCORE_TOKEN')
    if not token:
        print("Нет PANDASCORE_TOKEN")
        return

    since_dt = "2026-06-17T00:00:00Z"
    until_dt = "2026-06-23T23:59:59Z"

    print(f"Тянем PandaScore Dota2 матчи {since_dt} .. {until_dt} (ЛЮБОЙ статус)...")
    matches = fetch_all(token, since_dt, until_dt)
    print(f"PandaScore вернул: {len(matches)} матчей\n")

    by_league = {}
    for m in matches:
        lg = (m.get('league') or {}).get('name', '?')
        by_league.setdefault(lg, []).append(m)

    print(f"{'Лига':<40} {'Кол-во':>7}")
    print("-" * 50)
    for lg, ms in sorted(by_league.items(), key=lambda x: -len(x[1])):
        print(f"{lg:<40} {len(ms):>7}")

    # сравнение с harvest_db
    print(f"\n{'='*60}")
    print("СРАВНЕНИЕ С betsapi_harvest.db (DOTA2 матчи в этот период):")
    hcon = sqlite3.connect(HARVEST_DB)
    rows = hcon.execute("""
        SELECT league, COUNT(*) FROM raw_events
        WHERE league LIKE 'DOTA2%'
          AND CAST(start_time AS INTEGER) BETWEEN 1781654400 AND 1782259199
        GROUP BY league ORDER BY COUNT(*) DESC
    """).fetchall()
    hcon.close()
    for lg, cnt in rows:
        print(f"  {lg:<40} {cnt:>5}")

    # Покажем явно, каких матчей с TI Quals / EPL / International НЕТ у нас
    print(f"\n{'='*60}")
    print("Матчи из PandaScore, которых НЕТ (по именам команд) в нашей DB:")
    hcon = sqlite3.connect(HARVEST_DB)
    our_teams = set()
    for h, a in hcon.execute("""
        SELECT home_team, away_team FROM raw_events
        WHERE league LIKE 'DOTA2%'
          AND CAST(start_time AS INTEGER) BETWEEN 1781654400 AND 1782259199
    """).fetchall():
        our_teams.add((h, a))
    hcon.close()

    missing = []
    for m in matches:
        opps = m.get('opponents', [])
        if len(opps) != 2:
            continue
        names = [o.get('opponent', {}).get('name', '?') for o in opps]
        pair1 = (names[0], names[1])
        pair2 = (names[1], names[0])
        if pair1 not in our_teams and pair2 not in our_teams:
            missing.append((m.get('scheduled_at'), names[0], names[1],
                             (m.get('league') or {}).get('name', '?'),
                             m.get('status')))

    print(f"Найдено отсутствующих: {len(missing)}")
    for sched, h, a, lg, status in sorted(missing, key=lambda x: x[0] or ''):
        print(f"  {sched}  {h} vs {a}  [{lg}]  status={status}")


if __name__ == '__main__':
    main()
