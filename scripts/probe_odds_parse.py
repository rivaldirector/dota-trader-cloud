#!/usr/bin/env python3
"""
Тест парсинга odds_summary после фикса.
Берёт 1 ended Dota 2 событие и показывает что _extract_moneyline вернул.

Запуск:
    PYTHONPATH=. python3 scripts/probe_odds_parse.py
"""
import sys, json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.betsapi import BetsAPIClient, _extract_moneyline, _is_dota2

client = BetsAPIClient()

# Ищем Dota 2 событие в ended
eid = None
match_name = ""
for page in range(1, 20):
    data = client.get_ended(page)
    events = data.get("results", [])
    dota = [e for e in events if _is_dota2(e)]
    if dota:
        e = dota[0]
        eid = str(e["id"])
        match_name = f"{e.get('home',{}).get('name','?')} vs {e.get('away',{}).get('name','?')}"
        print(f"Нашли Dota 2 матч на странице {page}: {match_name} (id={eid})")
        break
    print(f"  Страница {page}: нет Dota 2")

if not eid:
    print("Dota 2 матчей не найдено в первых 20 страницах")
    sys.exit(1)

# Получаем odds summary
print(f"\nЗапрашиваем odds/summary для {eid}...")
summary = client.get_odds_summary(eid)
results = summary.get("results", {})
print(f"Букмекеров в ответе: {len(results)}")

# Показываем raw структуру первого букмекера
for bm_name, bm_data in list(results.items())[:1]:
    odds = bm_data.get("odds", {})
    print(f"\n[RAW] {bm_name}.odds keys: {list(odds.keys())}")
    for period, period_data in odds.items():
        print(f"  [{period}] markets: {list(period_data.keys())[:5]}")
        for mk, mk_data in list(period_data.items())[:1]:
            print(f"    {mk}: {json.dumps(mk_data)[:300]}")

# Парсим
bms = _extract_moneyline(summary)
print(f"\n_extract_moneyline → {len(bms)} букмекеров:")
if bms:
    for bm in bms:
        print(f"  {bm['bookmaker']:15} open={bm['open_home']:.3f}/{bm['open_away']:.3f}  close={bm['close_home']:.3f}/{bm['close_away']:.3f}")
    print("\n✓ Парсинг работает! Запускай backfill:")
    print("  PYTHONPATH=. python3 scripts/fetch_betsapi_history.py --pages 200")
else:
    print("✗ Парсинг вернул 0 букмекеров. Смотри raw структуру выше.")
    print("\nПолный ответ:")
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:2000])
