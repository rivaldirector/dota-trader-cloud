#!/usr/bin/env python3
"""
test_odds_api.py — проверка ODDS_API_KEY (the-odds-api.com формат) как
потенциального источника коэффициентов на Dota2, независимого от BetsAPI.

Просто печатает что вернул сервис — без записи в БД.
Запускать ИЗ ТЕРМИНАЛА (sandbox блокирует внешние домены).
"""
import os, json, urllib.request, urllib.error

_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_DIR, '../.env')


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


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None, str(e)


def main():
    env = load_env()
    key = env.get('ODDS_API_KEY')
    if not key:
        print("Нет ODDS_API_KEY в .env")
        return

    print("="*60)
    print("the-odds-api.com — список доступных спортов/лиг")
    print("="*60)
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={key}"
    status, body = get(url)
    print(f"GET {url.replace(key,'***')}")
    print(f"status={status}")
    print(f"body[:3000]={body[:3000]}")

    # Если есть esports/dota2 — пробуем достать odds
    if status == 200:
        try:
            sports = json.loads(body)
            dota_keys = [s['key'] for s in sports
                         if 'dota' in s.get('key','').lower()
                         or 'esports' in s.get('group','').lower()]
            print(f"\nНайдено возможных Dota2/esports ключей: {dota_keys}")
            for sk in dota_keys[:3]:
                url2 = f"https://api.the-odds-api.com/v4/sports/{sk}/odds/?apiKey={key}&regions=eu&markets=h2h"
                s2, b2 = get(url2)
                print(f"\n  GET sport={sk}")
                print(f"  status={s2}")
                print(f"  body[:1500]={b2[:1500]}")
        except Exception as e:
            print(f"Не смог распарсить список спортов: {e}")


if __name__ == '__main__':
    main()
