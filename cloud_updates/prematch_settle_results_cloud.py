#!/usr/bin/env python3
"""
prematch_settle_results_cloud.py — донабор СВОЕЙ статистики результатов.

Идея: prematch_free_predict.py пишет прогноз на КАЖДЫЙ матч из расписания
Liquipedia, даже если у нас нет Эло-истории по командам (has_elo_data=false,
"нет данных" на дэшборде). Раньше эти матчи просто пропадали из вида после
того, как проходили. Теперь — после того, как матч точно закончился (ждём
3ч буфера, как и elo_auto_settle.py), мы ищем реальный исход через
бесплатный OpenDota /api/proMatches (тот же бесплатный источник, что уже
использует elo_auto_settle.py для сеттлинга автономной машины) и:

  1) пишем actual_winner обратно в prematch_model_picks (чисто информационно
     для дэшборда — видно, кто победил, даже если мы не ставили);
  2) upsert'им результат в elo_own_history — ОТДЕЛЬНУЮ таблицу, которую
     build_elo_from_supabase() в elo_auto_bet.py/prematch_free_predict.py
     подмешивает в Эло-расчёт ТРЕТЬИМ источником (после BetsAPI+PandaScore).

Зачем: так покрытие по новым/малоизвестным командам (типа 4ikibamboni,
HULIGANI — см. чат) растёт само со временем из бесплатных источников,
без необходимости платного API. Через несколько матчей у таких команд
появится своя Эло-история, и "нет данных" по ним исчезнет естественным
образом.

Run:
    python3 scripts/prematch_settle_results_cloud.py

GitHub Actions: раз в ~3ч (см. prematch_settle_results_pipeline.yml).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

OPENDOTA_PROMATCHES_URL = "https://api.opendota.com/api/proMatches"
SETTLE_BUFFER_SEC = 3 * 3600       # ждём минимум 3ч после старта (матч точно закончился)
LOOKBACK_DAYS = 30                 # не пытаемся сеттлить совсем древние пики (OpenDota их и не покажет)
FUZZY_MIN_SUM = 1.3                # как в elo_auto_settle.py


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_team_name(name: str | None) -> str:
    if not name:
        return name or "?"
    return name.split(" (page does not exist)")[0].strip()


def sb_get(table: str, qs: str) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
                      headers={**SB_HEADERS, "Prefer": "return=representation"}, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_patch(table: str, qs: str, payload: dict) -> None:
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=SB_HEADERS, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] patch {table}: {r.status_code} {r.text[:200]}")


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                       headers=SB_HEADERS, json=rows, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] upsert {table}: {r.status_code} {r.text[:200]}")


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()


def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    cutoff_recent = (now - timedelta(seconds=SETTLE_BUFFER_SEC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_old = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    pending = sb_get(
        "prematch_model_picks",
        f"actual_winner=is.null&starts_at=lte.{cutoff_recent}&starts_at=gte.{cutoff_old}&"
        f"select=match_hash,team_1,team_2,league_name,starts_at&order=starts_at.desc&limit=200",
    )
    print(f"Пиков без известного исхода (старше {SETTLE_BUFFER_SEC // 3600}ч, "
          f"в пределах {LOOKBACK_DAYS}д): {len(pending)}")
    if not pending:
        return

    try:
        r = requests.get(OPENDOTA_PROMATCHES_URL, timeout=20)
        r.raise_for_status()
        pro_matches = r.json()
    except Exception as ex:
        print(f"ERROR: OpenDota proMatches недоступен: {ex}")
        sys.exit(1)
    print(f"OpenDota proMatches вернул: {len(pro_matches)} матчей")

    found_n = 0
    own_rows = []
    for p in pending:
        t1, t2 = clean_team_name(p.get("team_1")), clean_team_name(p.get("team_2"))
        try:
            st_dt = datetime.fromisoformat(p["starts_at"].replace("Z", "+00:00"))
            st = int(st_dt.timestamp())
        except Exception:
            continue

        best, best_score, best_orient = None, 0.0, "direct"
        for pm in pro_matches:
            rn, dn = pm.get("radiant_name"), pm.get("dire_name")
            if not rn or not dn:
                continue
            pm_st = pm.get("start_time", 0) + (pm.get("duration") or 0)
            if abs(pm_st - st) > 6 * 3600:
                continue
            s_direct = fuzzy(t1, rn) + fuzzy(t2, dn)
            s_cross = fuzzy(t1, dn) + fuzzy(t2, rn)
            score, orient = (s_direct, "direct") if s_direct >= s_cross else (s_cross, "cross")
            if score > best_score:
                best, best_score, best_orient = pm, score, orient

        if best_score < FUZZY_MIN_SUM or not best:
            print(f"  ? {t1} vs {t2} — нет совпадения в OpenDota пока (score={best_score:.2f})")
            continue

        radiant_win = bool(best.get("radiant_win"))
        t1_won = radiant_win if best_orient == "direct" else (not radiant_win)
        winner = t1 if t1_won else t2

        sb_patch("prematch_model_picks", f"match_hash=eq.{p['match_hash']}", {
            "actual_winner": winner, "result_checked_at": now_iso(),
        })
        own_rows.append({
            "match_hash": p["match_hash"],
            "home_team": t1, "away_team": t2,
            "league": p.get("league_name"),
            "start_time": st,
            "winner": winner,
            "source": "opendota_settle",
            "settled_at": now_iso(),
        })
        found_n += 1
        print(f"  ✓ {t1} vs {t2} -> победил {winner}")

    sb_upsert("elo_own_history", own_rows, on_conflict="match_hash")
    print(f"\nНайдено исходов: {found_n}/{len(pending)} (записано в elo_own_history: {len(own_rows)})")


if __name__ == "__main__":
    main()
