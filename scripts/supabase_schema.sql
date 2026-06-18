-- ============================================================
-- Dota Trader — Supabase Schema
-- Run in SQL Editor: https://supabase.com/dashboard/project/xplqjpftwvtbxmpsddor/sql
-- ============================================================

-- Raw events (матчи)
CREATE TABLE IF NOT EXISTS raw_events (
    event_id    TEXT PRIMARY KEY,
    sport_id    INTEGER,
    sport_tag   TEXT,
    league      TEXT,
    home_team   TEXT,
    away_team   TEXT,
    start_time  BIGINT,
    status      TEXT,
    score       TEXT,
    winner      TEXT,
    raw_json    TEXT,
    fetched_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_re_sport    ON raw_events(sport_tag);
CREATE INDEX IF NOT EXISTS idx_re_status   ON raw_events(status);
CREATE INDEX IF NOT EXISTS idx_re_start    ON raw_events(start_time);
CREATE INDEX IF NOT EXISTS idx_re_league   ON raw_events(league);

-- Odds summary (open/close per bookmaker per match)
CREATE TABLE IF NOT EXISTS odds_summary (
    id          BIGSERIAL PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES raw_events(event_id) ON DELETE CASCADE,
    bookmaker   TEXT NOT NULL,
    market      TEXT DEFAULT '151_1',
    open_home   REAL,
    open_away   REAL,
    close_home  REAL,
    close_away  REAL,
    raw_json    TEXT,
    fetched_at  TEXT,
    UNIQUE(event_id, bookmaker, market)
);

CREATE INDEX IF NOT EXISTS idx_os_event ON odds_summary(event_id);
CREATE INDEX IF NOT EXISTS idx_os_bm    ON odds_summary(bookmaker);

-- Live snapshots (текущие котировки на предстоящие матчи)
CREATE TABLE IF NOT EXISTS live_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    captured_at         TEXT NOT NULL,
    event_id            TEXT NOT NULL,
    league              TEXT,
    home_team           TEXT,
    away_team           TEXT,
    start_time          BIGINT,
    seconds_to_start    INTEGER,
    bookmaker           TEXT NOT NULL,
    market              TEXT DEFAULT '151_1',
    home_odds           REAL,
    away_odds           REAL,
    open_home           REAL,
    open_away           REAL,
    raw_json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ls_event ON live_snapshots(event_id);
CREATE INDEX IF NOT EXISTS idx_ls_time  ON live_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_ls_bm    ON live_snapshots(bookmaker);

-- Upcoming matches (предстоящие матчи для сигналов)
CREATE TABLE IF NOT EXISTS upcoming_events (
    event_id    TEXT PRIMARY KEY,
    sport_tag   TEXT,
    league      TEXT,
    home_team   TEXT,
    away_team   TEXT,
    start_time  BIGINT,
    raw_json    TEXT,
    fetched_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_ue_start ON upcoming_events(start_time);

-- Отключаем RLS на всех таблицах (как на research_queue)
ALTER TABLE raw_events       DISABLE ROW LEVEL SECURITY;
ALTER TABLE odds_summary     DISABLE ROW LEVEL SECURITY;
ALTER TABLE live_snapshots   DISABLE ROW LEVEL SECURITY;
ALTER TABLE upcoming_events  DISABLE ROW LEVEL SECURITY;
