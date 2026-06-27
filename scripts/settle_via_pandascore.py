#!/usr/bin/env python3
"""
settle_via_pandascore.py — резервный сеттлинг paper_bets через PandaScore API,
НЕЗАВИСИМО от сломанного BetsAPI токена.

Логика:
  1. Берём все pending (settled=0) матчи из paper_trading.db
  2. Тянем finished Dota2-матчи из PandaScore за нужный диапазон дат
  3. Сопоставляем по именам команд (fuzzy) + близости времени начала
  4. Если нашли winner — урегулируем ВСЕ ставки на этот event_id

НЕ трогает betsapi_harvest.db — работает только с paper_trading.db.
Запускать из терминала (sandbox блокирует внешние домены).

Использование:
  python3 scripts/settle_via_pandascore.py            # реальный прогон
  python3 scripts/settle_via_pandascore.py --dry-run  # только показать сопоставления
"""
import os, re, json, sqlite3, datetime, argparse
import urllib.request, urllib.parse, urllib.error

_DIR     = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_DIR, '../.env')
PAPER_DB = os.path.join(_DIR, '../data/paper_trading.db')


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


def normalize(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {url}")
        return []
    except Exception as e:
        print(f"  [WARN] {e} for {url}")
        return []


def fetch_finished_matches(token, since_dt, until_dt):
    """Пагинация по PandaScore /dota2/matches, finished, в диапазоне дат."""
    base = "https://api.pandascore.co/dota2/matches"
    headers = {"Authorization": f"Bearer {token}"}
    rng = f"{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{until_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    all_matches = []
    page = 1
    while page <= 20:
        params = {
            "filter[status]": "finished",
            "range[end_at]": rng,
            "per_page": "100",
            "page": str(page),
            "sort": "-end_at",
        }
        url = base + "?" + urllib.parse.urlencode(params)
        data = get_json(url, headers)
        if not data:
            break
        all_matches.extend(data)
        if len(data) < 100:
            break
        page += 1
    return all_matches


def match_score(pending_home, pending_away, ps_match):
    opps = ps_match.get('opponents', [])
    names = []
    for o in opps:
        opp = o.get('opponent') or {}
        names.append(opp.get('name', ''))
    if len(names) != 2:
        return 0.0, None
    nh, na = normalize(pending_home), normalize(pending_away)
    n0, n1 = normalize(names[0]), normalize(names[1])

    def sim(a, b):
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.8
        return 0.0

    # ориентация (home,away) против (n0,n1) или (n1,n0)
    s_direct = sim(nh, n0) + sim(na, n1)
    s_cross  = sim(nh, n1) + sim(na, n0)
    if s_direct >= s_cross:
        return s_direct, ('direct', names)
    return s_cross, ('cross', names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                     help='Только показать сопоставления, не писать в БД')
    args = ap.parse_args()

    env = load_env()
    token = env.get('PANDASCORE_TOKEN')
    if not token:
        print("Нет PANDASCORE_TOKEN в .env")
        return

    if not os.path.exists(PAPER_DB):
        print("paper_trading.db не найдена.")
        return

    pcon = sqlite3.connect(PAPER_DB, timeout=10)
    pending = pcon.execute("""
        SELECT DISTINCT event_id, home_team, away_team, start_time
        FROM paper_bets WHERE settled = 0
        ORDER BY start_time
    """).fetchall()

    if not pending:
        print("Нет неурегулированных ставок.")
        return

    print(f"Pending матчей: {len(pending)}")
    min_st = min(r[3] for r in pending)
    max_st = max(r[3] for r in pending)
    since_dt = datetime.datetime.utcfromtimestamp(min_st) - datetime.timedelta(hours=6)
    until_dt = min(datetime.datetime.utcnow(),
                    datetime.datetime.utcfromtimestamp(max_st) + datetime.timedelta(hours=12))

    print(f"Тянем PandaScore finished-матчи: {since_dt} .. {until_dt}")
    ps_matches = fetch_finished_matches(token, since_dt, until_dt)
    print(f"PandaScore вернул {len(ps_matches)} finished-матчей за период")

    settled_n = 0
    matched_n = 0
    now_ts = datetime.datetime.utcnow().isoformat()

    for eid, home, away, st in pending:
        best_score, best_info = 0.0, None
        best_ps = None
        for ps in ps_matches:
            score, info = match_score(home, away, ps)
            if score > best_score:
                best_score, best_info, best_ps = score, info, ps

        if best_score < 1.5 or not best_ps:
            print(f"  ? {home} vs {away}  ({datetime.datetime.utcfromtimestamp(st)})  — не нашли совпадение (best_score={best_score:.1f})")
            continue

        winner = best_ps.get('winner') or {}
        winner_name = winner.get('name', '') or ''
        orientation, ps_names = best_info
        nw = normalize(winner_name)

        # КРИТИЧНО: пустой winner_name означает "результат неизвестен/форфейт без
        # явного победителя" — раньше пустая строка ложно матчилась как подстрока
        # ЛЮБОГО имени (т.к. '' in 'x' == True), что давало фиктивную победу home.
        if not nw:
            print(f"  ? {home} vs {away} — нашли матч (PandaScore id={best_ps.get('id')}), "
                  f"но winner пустой — пропускаем (forfeit/no-result)")
            continue

        nh, na = normalize(home), normalize(away)
        if nw == nh or nh in nw or nw in nh:
            winner_side = 'home'
        elif nw == na or na in nw or nw in na:
            winner_side = 'away'
        else:
            print(f"  ? {home} vs {away} — нашли матч (PandaScore: {ps_names}, winner={winner_name}), но не смогли определить сторону")
            continue

        matched_n += 1
        print(f"  ✓ {home} vs {away}  →  winner={winner_name} ({winner_side})  [PandaScore match_id={best_ps.get('id')}]")

        if args.dry_run:
            continue

        bets = pcon.execute(
            "SELECT id, bet_team, odds, stake_usd FROM paper_bets WHERE event_id=? AND settled=0",
            (eid,)
        ).fetchall()
        for row_id, bet_team, odds, stake in bets:
            if bet_team == winner_side:
                outcome, pnl = 'win', round((odds - 1.0) * stake, 2)
            else:
                outcome, pnl = 'loss', -stake
            pcon.execute(
                "UPDATE paper_bets SET settled=1, outcome=?, pnl=?, settled_ts=? WHERE id=?",
                (outcome, pnl, now_ts, row_id)
            )
            settled_n += 1

    if not args.dry_run:
        pcon.commit()
    pcon.close()

    print(f"\n{'─'*50}")
    print(f"Найдено совпадений: {matched_n}/{len(pending)}")
    if args.dry_run:
        print("DRY-RUN — ничего не записано в БД.")
    else:
        print(f"Урегулировано ставок: {settled_n}")
        print("Запусти scripts/generate_dashboard.py чтобы обновить дэшборд.")


if __name__ == '__main__':
    main()
