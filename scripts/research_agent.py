#!/usr/bin/env python3
"""
Research Agent — читает research_queue из Supabase, выполняет анализ,
записывает результаты обратно.

Запуск:
    cd ~/Downloads/dota_trader_v2
    python3 scripts/research_agent.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

URL = os.getenv("SUPABASE_URL", "")
KEY = os.getenv("SUPABASE_ANON_KEY", "")

if not URL or not KEY:
    print("ERROR: SUPABASE_URL / SUPABASE_ANON_KEY не найдены в .env")
    sys.exit(1)

HEADERS = {
    "apikey":        KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

HARVEST_DB = ROOT / "storage" / "betsapi_harvest.db"
MAIN_DB    = ROOT / "storage" / "dota_trader.sqlite3"


# ── Supabase helpers ──────────────────────────────────────────────────────────

def sb_get(table: str, params: str = "") -> list:
    r = requests.get(f"{URL}/rest/v1/{table}?{params}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_post(table: str, data: dict) -> dict:
    r = requests.post(f"{URL}/rest/v1/{table}", headers=HEADERS,
                      json=data, timeout=15)
    r.raise_for_status()
    d = r.json()
    return d[0] if isinstance(d, list) and d else d


def sb_patch(table: str, filter_str: str, data: dict) -> list:
    r = requests.patch(f"{URL}/rest/v1/{table}?{filter_str}", headers=HEADERS,
                       json=data, timeout=15)
    r.raise_for_status()
    return r.json()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Local DB helpers ──────────────────────────────────────────────────────────

def harvest_conn():
    if not HARVEST_DB.exists():
        return None
    c = sqlite3.connect(HARVEST_DB)
    c.row_factory = sqlite3.Row
    return c


def main_conn():
    if not MAIN_DB.exists():
        return None
    c = sqlite3.connect(MAIN_DB)
    c.row_factory = sqlite3.Row
    return c


# ── Analysis functions ────────────────────────────────────────────────────────

def analyze_bookmaker_coverage(task: dict) -> dict:
    """Какие букмекеры покрывают Dota 2 и насколько."""
    conn = harvest_conn()
    if not conn:
        return {"error": "betsapi_harvest.db not found"}

    rows = conn.execute("""
        SELECT bookmaker,
               COUNT(*)                                      AS total_lines,
               COUNT(DISTINCT event_id)                      AS events,
               AVG(close_home)                               AS avg_home_odds,
               AVG(close_away)                               AS avg_away_odds,
               SUM(CASE WHEN ABS(open_home-close_home)>0.01
                        THEN 1 ELSE 0 END)                  AS lines_moved,
               AVG(ABS(open_home-close_home))                AS avg_movement
        FROM odds_summary
        GROUP BY bookmaker
        ORDER BY events DESC
    """).fetchall()

    bm_data = [dict(r) for r in rows]

    total_events = conn.execute(
        "SELECT COUNT(DISTINCT event_id) FROM odds_summary"
    ).fetchone()[0]

    # Overround per bookmaker (средний)
    for bm in bm_data:
        bm["avg_overround"] = round(
            (1 / bm["avg_home_odds"] + 1 / bm["avg_away_odds"])
            if bm["avg_home_odds"] and bm["avg_away_odds"] else 0, 4
        )
        bm["move_rate_pct"] = round(
            bm["lines_moved"] / bm["total_lines"] * 100
            if bm["total_lines"] else 0, 1
        )

    conn.close()

    return {
        "total_unique_events": total_events,
        "bookmakers_count":    len(bm_data),
        "bookmakers":          bm_data,
        "key_finding": (
            f"{len(bm_data)} букмекеров найдено. "
            f"Лидер по покрытию: {bm_data[0]['bookmaker'] if bm_data else 'N/A'} "
            f"({bm_data[0]['events'] if bm_data else 0} событий). "
            f"Всего уникальных матчей с odds: {total_events}."
        ),
    }


def analyze_line_movement(task: dict) -> dict:
    """Анализ движения линий: где умные деньги и насколько значимо."""
    conn = harvest_conn()
    if not conn:
        return {"error": "betsapi_harvest.db not found"}

    # Общая статистика движения
    stats = conn.execute("""
        SELECT
            COUNT(*)                                           AS total_rows,
            COUNT(DISTINCT event_id)                           AS events,
            SUM(CASE WHEN ABS(open_home-close_home)>0.01
                     THEN 1 ELSE 0 END)                       AS moved,
            AVG(ABS(open_home-close_home))                    AS avg_move,
            MAX(ABS(open_home-close_home))                    AS max_move,
            -- Направление: линия пошла на home (home стал фаворитом)
            SUM(CASE WHEN close_home < open_home THEN 1 ELSE 0 END) AS home_shortened,
            SUM(CASE WHEN close_away < open_away THEN 1 ELSE 0 END) AS away_shortened
        FROM odds_summary
        WHERE open_home IS NOT NULL AND close_home IS NOT NULL
    """).fetchone()

    # Топ-10 самых волатильных матчей
    top_moves = conn.execute("""
        SELECT os.event_id, re.home_team, re.away_team, re.league,
               os.bookmaker,
               os.open_home, os.close_home,
               os.open_away, os.close_away,
               ABS(os.open_home - os.close_home) AS home_move,
               ABS(os.open_away - os.close_away) AS away_move
        FROM odds_summary os
        JOIN raw_events re ON os.event_id = re.event_id
        WHERE re.sport_tag = 'dota2'
        ORDER BY (ABS(os.open_home - os.close_home) +
                  ABS(os.open_away - os.close_away)) DESC
        LIMIT 10
    """).fetchall()

    # Pinnacle vs остальные — расхождение
    pinnacle_diff = conn.execute("""
        SELECT
            p.event_id,
            p.close_home AS pin_home,
            p.close_away AS pin_away,
            AVG(o.close_home) AS avg_home,
            AVG(o.close_away) AS avg_away,
            ABS(p.close_home - AVG(o.close_home)) AS diff_home
        FROM odds_summary p
        JOIN odds_summary o ON p.event_id = o.event_id AND o.bookmaker != 'Pinnacle' AND o.bookmaker != 'PinnacleSports'
        WHERE p.bookmaker IN ('Pinnacle','PinnacleSports')
          AND p.close_home IS NOT NULL
        GROUP BY p.event_id
        ORDER BY diff_home DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    moved_pct = round(stats["moved"] / stats["total_rows"] * 100, 1) if stats["total_rows"] else 0

    return {
        "total_lines":       stats["total_rows"],
        "total_events":      stats["events"],
        "lines_moved":       stats["moved"],
        "moved_pct":         moved_pct,
        "avg_movement":      round(stats["avg_move"] or 0, 4),
        "max_movement":      round(stats["max_move"] or 0, 4),
        "home_shortened":    stats["home_shortened"],
        "away_shortened":    stats["away_shortened"],
        "top_volatile_matches": [dict(r) for r in top_moves],
        "pinnacle_vs_market": [dict(r) for r in pinnacle_diff],
        "key_finding": (
            f"{moved_pct}% линий показали движение >0.01. "
            f"Avg движение: {round(stats['avg_move'] or 0, 3)}. "
            f"Home shortened (home стал фаворитом): {stats['home_shortened']} раз, "
            f"Away shortened: {stats['away_shortened']} раз."
        ),
    }


def analyze_value_detection(task: dict) -> dict:
    """Матчи где рынок (Pinnacle close) сильно расходился с implied prob от соотношения odds."""
    conn = harvest_conn()
    if not conn:
        return {"error": "betsapi_harvest.db not found"}

    # Для каждого матча берём Pinnacle close и считаем implied prob
    # Затем смотрим на winner — совпало ли с тем, на кого была поставлена линия
    rows = conn.execute("""
        SELECT
            os.event_id,
            re.home_team,
            re.away_team,
            re.league,
            re.winner,
            re.score,
            os.bookmaker,
            os.open_home,
            os.open_away,
            os.close_home,
            os.close_away,
            -- Implied probability (no-vig)
            ROUND(1.0/os.close_home / (1.0/os.close_home + 1.0/os.close_away), 4) AS implied_home_prob,
            ROUND(1.0/os.close_away / (1.0/os.close_home + 1.0/os.close_away), 4) AS implied_away_prob,
            -- Line movement direction
            ROUND(os.open_home - os.close_home, 3) AS home_move,
            ROUND(os.open_away - os.close_away, 3) AS away_move
        FROM odds_summary os
        JOIN raw_events re ON os.event_id = re.event_id
        WHERE os.bookmaker IN ('Pinnacle', 'PinnacleSports')
          AND os.close_home IS NOT NULL AND os.close_away IS NOT NULL
          AND os.close_home > 1.0 AND os.close_away > 1.0
          AND re.winner IS NOT NULL
          AND re.sport_tag = 'dota2'
        ORDER BY os.event_id
    """).fetchall()

    if not rows:
        conn.close()
        return {"error": "No Pinnacle rows with winner data found"}

    total = len(rows)
    # Фаворит по рынку выиграл
    fav_wins = sum(1 for r in rows if (
        (r["implied_home_prob"] > 0.5 and r["winner"] == "1") or
        (r["implied_away_prob"] > 0.5 and r["winner"] == "2")
    ))
    # Underdog wins
    dog_wins = total - fav_wins

    # Большие аутсайдеры (implied < 25%) которые выиграли
    big_upsets = [dict(r) for r in rows
                  if r["winner"] == "1" and r["implied_home_prob"] < 0.25
                  or r["winner"] == "2" and r["implied_away_prob"] < 0.25]

    # Матчи где линия сильно двигалась (>0.3) и движение правильно предсказало победителя
    sharp_correct = [dict(r) for r in rows
                     if r["home_move"] > 0.3 and r["winner"] == "2"  # home shortened → away won
                     or r["away_move"] > 0.3 and r["winner"] == "1"]  # away shortened → home won

    # Overround Pinnacle
    overrounds = [(1/r["close_home"] + 1/r["close_away"]) for r in rows
                  if r["close_home"] and r["close_away"]]
    avg_overround = round(sum(overrounds)/len(overrounds), 4) if overrounds else 0

    conn.close()

    fav_win_rate = round(fav_wins / total * 100, 1) if total else 0

    return {
        "total_pinnacle_matches": total,
        "favorite_win_rate_pct":  fav_win_rate,
        "underdog_wins":          dog_wins,
        "big_upsets_count":       len(big_upsets),
        "big_upsets_sample":      big_upsets[:5],
        "sharp_money_correct":    len(sharp_correct),
        "sharp_money_correct_pct": round(len(sharp_correct) / total * 100, 1) if total else 0,
        "avg_pinnacle_overround": avg_overround,
        "key_finding": (
            f"Pinnacle: {total} матчей с winner. "
            f"Фаворит выиграл в {fav_win_rate}% случаев. "
            f"Больших сюрпризов (андердог <25%): {len(big_upsets)}. "
            f"Sharp money (движение >0.3) угадало победителя: {len(sharp_correct)} раз "
            f"({round(len(sharp_correct)/total*100,1) if total else 0}%). "
            f"Avg overround Pinnacle: {avg_overround}."
        ),
    }


def analyze_dataset_summary(task: dict) -> dict:
    """Общая сводка собранного датасета."""
    conn = harvest_conn()
    if not conn:
        return {"error": "betsapi_harvest.db not found"}

    events_by_sport = dict(conn.execute(
        "SELECT sport_tag, COUNT(*) FROM raw_events WHERE status='ended' GROUP BY sport_tag"
    ).fetchall())

    dota_date_range = conn.execute("""
        SELECT MIN(start_time) as oldest, MAX(start_time) as newest
        FROM raw_events WHERE sport_tag='dota2' AND status='ended'
    """).fetchone()

    odds_summary_count = conn.execute(
        "SELECT COUNT(*) FROM odds_summary"
    ).fetchone()[0]

    unique_matches_with_odds = conn.execute(
        "SELECT COUNT(DISTINCT event_id) FROM odds_summary"
    ).fetchone()[0]

    unique_bm = conn.execute(
        "SELECT COUNT(DISTINCT bookmaker) FROM odds_summary"
    ).fetchone()[0]

    bm_list = [r[0] for r in conn.execute(
        "SELECT bookmaker FROM odds_summary GROUP BY bookmaker ORDER BY COUNT(*) DESC"
    ).fetchall()]

    hist_pts = conn.execute("SELECT COUNT(*) FROM odds_history").fetchone()[0]

    api_calls = conn.execute("SELECT COUNT(*) FROM api_log").fetchone()[0]

    from datetime import datetime
    oldest = datetime.fromtimestamp(int(dota_date_range["oldest"])).strftime("%Y-%m-%d") if dota_date_range["oldest"] else "N/A"
    newest = datetime.fromtimestamp(int(dota_date_range["newest"])).strftime("%Y-%m-%d") if dota_date_range["newest"] else "N/A"

    db_size_mb = round(HARVEST_DB.stat().st_size / 1024 / 1024, 1)

    conn.close()

    return {
        "events_by_sport":          events_by_sport,
        "dota2_date_range":         {"oldest": oldest, "newest": newest},
        "odds_summary_rows":        odds_summary_count,
        "unique_matches_with_odds": unique_matches_with_odds,
        "unique_bookmakers":        unique_bm,
        "bookmakers":               bm_list,
        "odds_history_points":      hist_pts,
        "api_calls_total":          api_calls,
        "db_size_mb":               db_size_mb,
        "key_finding": (
            f"Датасет: {events_by_sport.get('dota2',0):,} Dota2 матчей ({oldest}→{newest}), "
            f"{unique_matches_with_odds:,} с odds от {unique_bm} букмекеров, "
            f"{odds_summary_count:,} строк odds_summary, "
            f"{hist_pts:,} movement points. DB: {db_size_mb} MB."
        ),
    }


# ── Task dispatcher ───────────────────────────────────────────────────────────

ANALYZERS = {
    "bookmaker_coverage":  analyze_bookmaker_coverage,
    "line_movement":       analyze_line_movement,
    "dataset_summary":     analyze_dataset_summary,
    "value_detection":     analyze_value_detection,
}


def run_task(task: dict) -> dict:
    """Выбирает и запускает нужный анализатор по task_type."""
    task_type = task.get("task_type", "")
    fn = ANALYZERS.get(task_type)

    if fn:
        print(f"  Running analyzer: {task_type}")
        return fn(task)

    # Неизвестный тип — возвращаем сводку как fallback
    print(f"  Unknown task_type '{task_type}', running dataset_summary as fallback")
    return analyze_dataset_summary(task)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print("Research Agent — Dota Trader Supabase")
    print(f"{'='*60}\n")

    # 1. Читаем research_queue
    print("[1] Reading research_queue...")
    try:
        queue = sb_get("research_queue",
                       "order=priority.desc&status=eq.todo&limit=20")
    except Exception as e:
        print(f"  ERROR reading queue: {e}")
        sys.exit(1)

    print(f"  Found {len(queue)} tasks with status=todo\n")

    if not queue:
        print("  Queue is empty — nothing to do.")
        all_tasks = sb_get("research_queue", "order=priority.desc&limit=50")
        print(f"  All tasks ({len(all_tasks)}):")
        for t in all_tasks:
            print(f"    [{t.get('priority',0):>3}] {t.get('status','?'):<10} {str(t.get('task',''))[:60]}")
        return

    # Все задачи в очереди
    print("  Current queue:")
    for t in queue:
        print(f"    [{t.get('priority',0):>3}] {t.get('status','?'):<10} {str(t.get('task',''))[:70]}")

    # 2. Берём задачу с максимальным priority
    task = queue[0]
    # Определяем task_type из поля task (первое слово до ':')
    task_text = task.get("task", "")
    task_type = task_text.split(":")[0].strip()
    task["task_type"] = task_type

    print(f"\n[2] Selected task:")
    print(f"    ID:       {task.get('id')}")
    print(f"    type:     {task_type}")
    print(f"    priority: {task.get('priority')}")
    print(f"    task:     {task_text}")

    # 3. Выполняем анализ
    print(f"\n[3] Running analysis...")
    try:
        result = run_task(task)
        print(f"  Key finding: {result.get('key_finding', 'N/A')}")
    except Exception as e:
        result = {"error": str(e)}
        print(f"  ERROR: {e}")

    # 4. Записываем результат в поле result таблицы research_queue
    print(f"\n[4] Writing result to research_queue.result...")
    result_text = result.get("key_finding", "") + "\n\n" + json.dumps(result, ensure_ascii=False, default=str, indent=2)
    try:
        sb_patch("research_queue", f"id=eq.{task['id']}",
                 {"status": "done", "result": result_text[:10000]})
        print("  Result saved, status → done.")
    except Exception as e:
        print(f"  ERROR saving result: {e}")

    # 5. Следующая задача
    print(f"\n[5] Next task suggestion:")
    next_map = {
        "dataset_summary":    "bookmaker_coverage: Какие букмекеры покрывают Dota2, overround, движение",
        "bookmaker_coverage": "line_movement: Анализ sharp money — open→close, Pinnacle vs soft books",
        "line_movement":      "value_detection: Матчи где Elo расходился с рынком и был прав",
        "value_detection":    "calibration_check: Калибровка Elo — насколько predicted prob совпадает с реальной win rate",
    }
    next_task_text = next_map.get(task_type, "dataset_summary: Обновлённая сводка датасета")
    next_priority  = max(10, task.get("priority", 50) - 10)

    try:
        existing = sb_get("research_queue", f"status=eq.todo&task=eq.{next_task_text}")
        if existing:
            print(f"  Already in queue: '{next_task_text[:50]}'")
        else:
            sb_post("research_queue", {
                "priority": next_priority, "status": "todo",
                "assigned_to": "claude", "task": next_task_text
            })
            print(f"  Created: '{next_task_text[:60]}' (priority={next_priority})")
    except Exception as e:
        print(f"  ERROR creating next task: {e}")

    # Итог
    print(f"\n{'='*60}")
    print("REPORT")
    print(f"{'='*60}")
    print(f"  1. Проверил:   research_queue ({len(queue)} todo задач)")
    print(f"  2. SQL:        SELECT * FROM research_queue WHERE status='todo' ORDER BY priority DESC")
    print(f"  3. Вывод:      {result.get('key_finding', 'см. result_json')}")
    print(f"  4. Записано:   research_results + research_queue.status='done'")
    print(f"  5. Следующее:  '{next_task_text[:60]}' для GPT/Claude")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
