#!/usr/bin/env python3
"""
test_alt_settle_apis.py — диагностика альтернативных источников результатов
(PandaScore, DotaScore) как запасного канала сеттлинга, пока BetsAPI токен мёртв.

Просто печатает что вернул каждый сервис — без записи в БД.
Запускать ИЗ ТЕРМИНАЛА (не через sandbox — там блокируется прокси).
"""
import os, json, urllib.request, urllib.parse, urllib.error

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

def get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None, str(e)


def test_pandascore(env):
    print("\n" + "="*60)
    print("PANDASCORE")
    print("="*60)
    token = env.get('PANDASCORE_TOKEN')
    base = env.get('PANDASCORE_BASE_URL', 'https://api.pandascore.co')
    if not token:
        print("  Нет PANDASCORE_TOKEN в .env")
        return
    url = f"{base}/dota2/matches?per_page=5&sort=-end_at&filter[status]=finished"
    status, body = get(url, headers={"Authorization": f"Bearer {token}"})
    print(f"  GET {url}")
    print(f"  status={status}")
    print(f"  body[:1500]={body[:1500]}")


def test_dotascore(env):
    print("\n" + "="*60)
    print("DOTASCORE")
    print("="*60)
    key = env.get('DOTASCORE_API_KEY')
    base = env.get('DOTASCORE_BASE_URL', 'https://api.dotascore.live')
    if not key:
        print("  Нет DOTASCORE_API_KEY в .env")
        return
    # Пробуем несколько распространённых паттернов — структура API неизвестна
    candidates = [
        f"{base}/matches?api_key={key}",
        f"{base}/v1/matches?api_key={key}",
        f"{base}/matches",
        f"{base}/v1/matches",
    ]
    for url in candidates:
        headers = {"Authorization": f"Bearer {key}", "X-Api-Key": key}
        status, body = get(url, headers=headers)
        print(f"\n  GET {url}")
        print(f"  status={status}")
        print(f"  body[:800]={body[:800]}")
        if status == 200:
            print("  --> рабочий эндпоинт найден, останавливаюсь")
            break


if __name__ == '__main__':
    env = load_env()
    test_pandascore(env)
    test_dotascore(env)
    print("\nГотово. Скопируй весь вывод и пришли — разберём какой сервис рабочий.")
