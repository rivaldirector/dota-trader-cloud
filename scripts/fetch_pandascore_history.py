#!/usr/bin/env python3
"""
fetch_pandascore_history.py — тянет ИСТОРИЧЕСКИЕ finished-матчи Dota2 из
PandaScore за последние N дней и складывает в отдельную локальную БД
data/pandascore_history.db — НЕ трогает betsapi_harvest.db.

Зачем: betsapi_harvest.db (BetsAPI) оказался сильно недообсчитан именно по
некоторым лигам/брэкетам (TI Quals, EPL — нашли 15 матчей вместо реальных 71
за неделю). PandaScore покрывает их полнее. Эта история используется ТОЛЬКО
для построения более точного Elo team-rating — коэффициентов тут нет и не
будет, PandaScore не odds-провайдер.

Запускать ИЗ ТЕРМИНАЛА (sandbox блокирует внешние домены). Безопасно для
betsapi_harvest.db / paper_trading.db — пишет только в новый отдельный файл.

Использование:
  python3 scripts/fetch_pandascore_history.py            # по умолчанию 60 дней назад
  python3 scripts/fetch_pandascore_history.py --days 120
"""
import os, json, sqlite3, datetime, argparse
import urllib.request, urllib.parse, urllib.error

_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_DIR, '../.env')
HIST_DB  = os.path.join(_DIR, '../data/pandascore_history.db')
THROTTLE_PATH = os.path.join(_DIR, '../storage/.last_history_fetch')

# Continuous-цикл вызывает этот скрипт каждые ~10 минут — полный 60-дневный
# повторный обход тут не нужен так часто (история обновляется медленно,
# в отличие от расписания/результатов недавних матчей). Троттлим жёстче.
THROTTLE_MINUTES = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS ps_matches (
    ps_id      INTEGER PRIMARY KEY,
    home_team  TEXT NOT NULL,
    away_team  TEXT NOT NULL,
    league     TEXT,
    start_time INTEGER NOT NULL,
    winner     TEXT,
    status     TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ps_start ON ps_matches(start_time);
"""


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
        print(f"  [WARN] HTTP {e.code} for {url}")
        return []
    except Exception as e:
        print(f"  [WARN] {e} for {url}")
        return []


def to_unix(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.datetime.strptime(iso_str, '%Y-%m-%dT%H:%M:%SZ')
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return None


def fetch_finished_range(token, since_dt, until_dt):
    base = "https://api.pandascore.co/dota2/matches"
    headers = {"Authorization": f"Bearer {token}"}
    rng = f"{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{until_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    all_matches, page = [], 1
    while page <= 50:
        params = {
            "filter[status]": "finished",
            "range[end_at]": rng,
            "per_page": "100",
            "page": str(page),
            "sort": "end_at",
        }
        url = base + "?" + urllib.parse.urlencode(params)
        data = get_json(url, headers)
        if not data:
            break
        all_matches.extend(data)
        print(f"   page {page}: +{len(data)} (всего {len(all_matches)})", flush=True)
        if len(data) < 100:
            break
        page += 1
    return all_matches


def throttled(force=False):
    if force or not os.path.exists(THROTTLE_PATH):
        return False
    try:
        with open(THROTTLE_PATH) as f:
            last = datetime.datetime.fromisoformat(f.read().strip())
        return (datetime.datetime.utcnow() - last) < datetime.timedelta(minutes=THROTTLE_MINUTES)
    except Exception:
        return False


def mark_fetched():
    os.makedirs(os.path.dirname(THROTTLE_PATH), exist_ok=True)
    with open(THROTTLE_PATH, 'w') as f:
        f.write(datetime.datetime.utcnow().isoformat())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=60,
                     help='Сколько дней истории назад тянуть (default: 60)')
    ap.add_argument('--force', action='store_true', help='Игнорировать троттлинг')
    args = ap.parse_args()

    if throttled(args.force):
        print(f"Пропуск: история обновлялась < {THROTTLE_MINUTES} мин назад (--force чтобы обойти).")
        return

    env = load_env()
    token = env.get('PANDASCORE_TOKEN')
    if not token:
        print("Нет PANDASCORE_TOKEN в .env")
        return

    now = datetime.datetime.utcnow()
    since_dt = now - datetime.timedelta(days=args.days)

    print(f"Тянем PandaScore finished Dota2-матчи: {since_dt} .. {now} ({args.days} дн.)")
    matches = fetch_finished_range(token, since_dt, now)
    print(f"\nВсего получено: {len(matches)} матчей")

    os.makedirs(os.path.dirname(HIST_DB), exist_ok=True)
    con = sqlite3.connect(HIST_DB)
    con.executescript(SCHEMA)

    now_iso = datetime.datetime.utcnow().isoformat()
    inserted, updated = 0, 0
    for m in matches:
        opps = m.get('opponents', [])
        if len(opps) != 2:
            continue
        home = opps[0].get('opponent', {}).get('name', '?')
        away = opps[1].get('opponent', {}).get('name', '?')
        league = (m.get('league') or {}).get('name', '?')
        st = to_unix(m.get('begin_at') or m.get('scheduled_at'))
        if st is None:
            continue
        winner = (m.get('winner') or {}).get('name') if m.get('winner') else None
        ps_id = m.get('id')

        cur = con.execute("SELECT 1 FROM ps_matches WHERE ps_id=?", (ps_id,)).fetchone()
        con.execute("""
            INSERT OR REPLACE INTO ps_matches
            (ps_id, home_team, away_team, league, start_time, winner, status, fetched_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (ps_id, home, away, f"DOTA2 - {league}", st, winner, 'finished', now_iso))
        if cur:
            updated += 1
        else:
            inserted += 1

    con.commit()
    total = con.execute("SELECT COUNT(*) FROM ps_matches").fetchone()[0]
    no_winner = con.execute("SELECT COUNT(*) FROM ps_matches WHERE winner IS NULL").fetchone()[0]
    con.close()

    print(f"\nНовых: {inserted}, обновлено: {updated}")
    print(f"Всего в pandascore_history.db: {total} (без winner: {no_winner})")
    print(f"\nЗапусти scripts/generate_dashboard.py — Elo теперь будет учитывать эту историю.")
    mark_fetched()


if __name__ == '__main__':
    main()
