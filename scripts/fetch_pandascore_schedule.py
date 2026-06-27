#!/usr/bin/env python3
"""
fetch_pandascore_schedule.py — тянет ПОЛНОЕ расписание Dota2-матчей из
PandaScore (not_started + running) и кэширует локально в JSON.

Зачем отдельно от betsapi_harvest.db: PandaScore не даёт коэффициентов,
только расписание/результаты. Это просто заполняет дыру в "какие матчи
вообще существуют" — Elo-прогноз тогда строится по ПОЛНОМУ списку, а не
только по тем 15 матчам, что успел заскрейпить мёртвый BetsAPI.

Кэш: storage/pandascore_schedule_cache.json
Запускать ИЗ ТЕРМИНАЛА (sandbox блокирует внешние домены).
Безопасно — ничего не пишет в betsapi_harvest.db или paper_trading.db.
"""
import os, json, datetime, argparse, urllib.request, urllib.parse, urllib.error

_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_DIR, '../.env')
CACHE_PATH = os.path.join(_DIR, '../storage/pandascore_schedule_cache.json')
THROTTLE_PATH = os.path.join(_DIR, '../storage/.last_schedule_fetch')

WINDOW_DAYS_BACK = 7    # сколько дней назад тоже подтягиваем (для Elo-истории/сверки)
WINDOW_DAYS_FWD = 10    # сколько дней вперёд

# Скрипт теперь вызывается из continuous-цикла каждые ~10 минут (см.
# daily_paper_cycle.sh) — троттлим реальный вызов API, чтобы не дёргать
# PandaScore чаще, чем расписание реально может измениться.
THROTTLE_MINUTES = 15


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


def fetch_all(token, since_dt, until_dt):
    base = "https://api.pandascore.co/dota2/matches"
    headers = {"Authorization": f"Bearer {token}"}
    rng = f"{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{until_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    all_matches, page = [], 1
    while page <= 30:
        params = {
            "range[scheduled_at]": rng,
            "per_page": "100",
            "page": str(page),
            "sort": "scheduled_at",
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


def to_unix(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.datetime.strptime(iso_str, '%Y-%m-%dT%H:%M:%SZ')
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return None


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
    ap.add_argument('--force', action='store_true', help='Игнорировать троттлинг')
    args = ap.parse_args()

    if throttled(args.force):
        print(f"Пропуск: расписание обновлялось < {THROTTLE_MINUTES} мин назад (--force чтобы обойти).")
        return

    env = load_env()
    token = env.get('PANDASCORE_TOKEN')
    if not token:
        print("Нет PANDASCORE_TOKEN в .env")
        return

    now = datetime.datetime.utcnow()
    since_dt = now - datetime.timedelta(days=WINDOW_DAYS_BACK)
    until_dt = now + datetime.timedelta(days=WINDOW_DAYS_FWD)

    print(f"Тянем PandaScore Dota2 расписание {since_dt} .. {until_dt}...")
    matches = fetch_all(token, since_dt, until_dt)
    print(f"Получено: {len(matches)} матчей")

    cache = []
    for m in matches:
        opps = m.get('opponents', [])
        if len(opps) != 2:
            continue
        home = opps[0].get('opponent', {}).get('name', '?')
        away = opps[1].get('opponent', {}).get('name', '?')
        league = (m.get('league') or {}).get('name', '?')
        status = m.get('status')
        st = to_unix(m.get('scheduled_at') or m.get('begin_at'))
        winner = (m.get('winner') or {}).get('name') if m.get('winner') else None
        if st is None:
            continue
        cache.append({
            'ps_id': m.get('id'),
            'home_team': home,
            'away_team': away,
            'league': f"DOTA2 - {league}",
            'start_time': st,
            'status': status,   # not_started / running / finished / canceled / postponed
            'winner': winner,
        })

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            'fetched_at': now.isoformat(),
            'matches': cache,
        }, f, ensure_ascii=False, indent=2)

    print(f"Сохранено {len(cache)} матчей в {CACHE_PATH}")
    by_status = {}
    for c in cache:
        by_status[c['status']] = by_status.get(c['status'], 0) + 1
    print(f"По статусам: {by_status}")
    mark_fetched()


if __name__ == '__main__':
    main()
