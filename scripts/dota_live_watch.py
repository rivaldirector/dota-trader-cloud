#!/usr/bin/env python3
"""
Dota Live Watch — снэпшот текущих live pro-матчей Dota2 (длительность, килы)
через бесплатный публичный эндпоинт OpenDota. Не требует BetsAPI/токена —
отдельный источник, чисто игровая статистика (не коэффициенты).

OpenDota /api/live отдаёт ВСЕ текущие live-игры (включая обычные пабы).
Фильтруем по league_id != 0 — это и есть pro/tournament матчи.

Контур live-сигналов (Contour A) — полностью отдельно от "Dota evening
settle" / Rule C / Elo (тот контур pre-match, на Elo+market odds, требует
BetsAPI, ничего общего с live-килами не имеет — проверено в коде
strategy_core.py). Здесь свой банк-less сигнальный слой:

  1. dota_live_matches    — последний снэпшот на матч (для "что идёт сейчас")
  2. dota_live_kill_log   — append-only история (нужна для будущего
                            бэктеста — пока НИ ОДНОГО матча с логом
                            килов-по-времени не накоплено)
  3. live_signals         — флаг "возможно стоит ставить" по простому,
                            НЕВАЛИДИРОВАННОМУ правилу (EXPERIMENTAL):
                            kill_diff >= 8 при game_time <= 900s, или
                            kill_diff >= 12 в любой момент. Без коэффициентов
                            — чисто информационный сигнал, без расчёта edge.
                            Один сигнал на матч (дебounce по unique(match_id,
                            rule_code)), не на каждый опрос.

Run:
    python3 scripts/dota_live_watch.py

GitHub Actions: каждые 15 минут (короткий cron, отдельный от daily_pipeline).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
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

OPENDOTA_LIVE_URL = "https://api.opendota.com/api/live"

# EXPERIMENTAL live-signal rule — не бэктестено (нет исторических данных
# килы-по-времени), чисто стартовая эвристика. Без коэффициентов/edge,
# только флаг "посмотри на этот матч".
EARLY_KILL_DIFF_THRESHOLD = 8
EARLY_GAME_TIME_CUTOFF_SEC = 900     # 15 минут
ANYTIME_KILL_DIFF_THRESHOLD = 12
RULE_CODE = "live_kill_diff_v0_experimental"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers=SB_HEADERS, json=rows, timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] upsert {table}: {r.status_code} {r.text[:200]}")


def sb_insert(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    headers = {**SB_HEADERS, "Prefer": "return=minimal"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=headers, json=rows, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  [SB ERROR] insert {table}: {r.status_code} {r.text[:200]}")


def check_live_signal(radiant_score: int, dire_score: int, game_time_sec: int) -> str | None:
    """EXPERIMENTAL — простая невалидированная эвристика на килах/времени.
    Возвращает текст причины срабатывания или None."""
    diff = abs((radiant_score or 0) - (dire_score or 0))
    gt = game_time_sec or 0
    if gt <= EARLY_GAME_TIME_CUTOFF_SEC and diff >= EARLY_KILL_DIFF_THRESHOLD:
        return f"ранний разрыв по килам {diff} за {gt//60} мин (порог {EARLY_KILL_DIFF_THRESHOLD} за <{EARLY_GAME_TIME_CUTOFF_SEC//60} мин)"
    if diff >= ANYTIME_KILL_DIFF_THRESHOLD:
        return f"разрыв по килам {diff} (порог {ANYTIME_KILL_DIFF_THRESHOLD})"
    return None


def main():
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        print("ERROR: missing SUPABASE_URL / SUPABASE_ANON_KEY")
        sys.exit(1)

    try:
        r = requests.get(OPENDOTA_LIVE_URL, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as ex:
        print(f"ERROR: OpenDota live недоступен: {ex}")
        sys.exit(1)

    pro_matches = [m for m in data if m.get("league_id", 0)]
    print(f"Всего live игр: {len(data)}  |  pro (league_id!=0): {len(pro_matches)}")

    now = now_iso()
    rows, log_rows, signal_rows = [], [], []
    for m in pro_matches:
        match_id = str(m.get("match_id"))
        league_id = m.get("league_id")
        team_r = m.get("team_name_radiant") or None
        team_d = m.get("team_name_dire") or None
        r_score = m.get("radiant_score")
        d_score = m.get("dire_score")
        game_time = m.get("game_time")

        rows.append({
            "match_id": match_id, "league_id": league_id,
            "team_radiant": team_r, "team_dire": team_d,
            "radiant_score": r_score, "dire_score": d_score,
            "game_time_sec": game_time, "delay_sec": m.get("delay"),
            "last_seen_at": now,
        })
        log_rows.append({
            "match_id": match_id, "league_id": league_id,
            "team_radiant": team_r, "team_dire": team_d,
            "radiant_score": r_score, "dire_score": d_score,
            "game_time_sec": game_time, "checked_at": now,
        })

        mins, secs = divmod(int(game_time or 0), 60)
        line = (f"  [{league_id}] {team_r or '?'} vs {team_d or '?'}  "
                f"{mins:02d}:{secs:02d}  килы {r_score}:{d_score}")

        reason = check_live_signal(r_score, d_score, game_time)
        if reason:
            leading = "radiant" if (r_score or 0) > (d_score or 0) else "dire"
            signal_rows.append({
                "match_id": match_id, "league_id": league_id,
                "team_radiant": team_r, "team_dire": team_d,
                "radiant_score": r_score, "dire_score": d_score,
                "kill_diff": abs((r_score or 0) - (d_score or 0)),
                "leading_side": leading, "game_time_sec": game_time,
                "rule_code": RULE_CODE, "rule_note": reason,
            })
            line += f"  ⚡ LIVE SIGNAL (experimental): {reason}"
        print(line)

    sb_upsert("dota_live_matches", rows, on_conflict="match_id")
    sb_insert("dota_live_kill_log", log_rows)
    print(f"\nЗаписано в dota_live_matches: {len(rows)}  |  лог: {len(log_rows)}")

    if signal_rows:
        # on_conflict=(match_id, rule_code) + ignore-duplicates — повторное
        # срабатывание того же правила на том же матче тихо пропускается (дебаунс).
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/live_signals?on_conflict=match_id,rule_code",
            headers={**SB_HEADERS, "Prefer": "resolution=ignore-duplicates,return=representation"},
            json=signal_rows, timeout=30,
        )
        new_signals = r.json() if r.status_code in (200, 201) else []
        if r.status_code not in (200, 201):
            print(f"  [SB ERROR] live_signals: {r.status_code} {r.text[:200]}")
        elif new_signals:
            print(f"\n⚡ Новых live-сигналов: {len(new_signals)}")
            for s in new_signals:
                print(f"   {s['team_radiant']} vs {s['team_dire']}: {s['rule_note']}")


if __name__ == "__main__":
    main()
