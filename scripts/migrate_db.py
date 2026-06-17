#!/usr/bin/env python3
"""
DB migration: пересоздать odds_snapshots с новой схемой.
Безопасно: таблица была пустой (0 строк).

Запуск:
    python3 scripts/migrate_db.py
"""
import sys
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

DB_PATH = PROJECT_ROOT / settings.database_path


def migrate():
    conn = sqlite3.connect(DB_PATH)

    # Проверить сколько строк в старой таблице
    try:
        count = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        print(f"Existing rows in odds_snapshots: {count}")
        if count > 0:
            print("WARNING: table has data — backing up before migration")
            conn.execute("ALTER TABLE odds_snapshots RENAME TO odds_snapshots_backup")
            conn.commit()
            print("Backup created: odds_snapshots_backup")
        else:
            conn.execute("DROP TABLE IF EXISTS odds_snapshots")
            conn.commit()
            print("Old empty table dropped")
    except Exception as e:
        print(f"Note: {e}")

    # Создать новую таблицу
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS odds_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        match_external_id   TEXT,
        match_name          TEXT NOT NULL,
        match_start_at      TEXT,
        source              TEXT NOT NULL,
        bookmaker           TEXT NOT NULL,
        team_1_name         TEXT,
        team_2_name         TEXT,
        team_1_odds         REAL,
        team_2_odds         REAL,
        team_1_implied_prob REAL,
        team_2_implied_prob REAL,
        overround           REAL,
        raw_json            TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_odds_match  ON odds_snapshots(match_external_id);
    CREATE INDEX IF NOT EXISTS idx_odds_time   ON odds_snapshots(captured_at);
    CREATE INDEX IF NOT EXISTS idx_odds_source ON odds_snapshots(source, bookmaker);
    """)
    conn.commit()

    # Проверить
    cols = [r[1] for r in conn.execute("PRAGMA table_info(odds_snapshots)").fetchall()]
    print(f"\nNew odds_snapshots columns ({len(cols)}):")
    for c in cols:
        print(f"  {c}")

    count_new = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    print(f"\nRows in new table: {count_new}")
    print("\nMigration complete. Run probe again:")
    print("  python3 scripts/collect_odds.py --probe")

    conn.close()


if __name__ == "__main__":
    migrate()
