#!/usr/bin/env python3
"""
tournament_build_features.py
Шаг 1: Строим tournament_blind_features — только pre-match данные.

Запуск:
    PYTHONPATH=. python3 scripts/tournament_build_features.py
"""
from __future__ import annotations
import sqlite3, os, sys, datetime, json, math

HARVEST_DB  = os.path.join(os.path.dirname(__file__), "../storage/betsapi_harvest.db")
TOURN_DB    = os.path.join(os.path.dirname(__file__), "../data/model_tournament.db")

TRAIN_END   = 1735689599   # 2024-12-31 23:59:59 UTC
VAL_END     = 1767225599   # 2025-12-31 23:59:59 UTC
TEST_END    = 1781654399   # 2026-06-15 23:59:59 UTC

START_ELO   = 1000.0
K_FACTOR    = 32.0

FORBIDDEN_COLS = {'winner','score','ss','close_home','close_away',
                  'close_prob','profit','clv','result','settled'}

PREFERRED_BM_ORDER = ['PinnacleSports','Bet365','GGBet','MelBet','YSB88']


def elo_expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

def novig_prob(h_odds: float, a_odds: float):
    if not h_odds or not a_odds or h_odds <= 1 or a_odds <= 1:
        return None, None
    raw_h = 1.0 / h_odds
    raw_a = 1.0 / a_odds
    total = raw_h + raw_a
    return raw_h / total, raw_a / total

def derive_winner(home, away, winner_col, score_col):
    if winner_col and str(winner_col).strip() not in ('draw', '', 'null', 'None'):
        wc = str(winner_col).strip()
        if wc == home: return 'home'
        if wc == away: return 'away'
    if score_col:
        s = str(score_col).strip().lower()
        if s == 'home': return 'home'
        if s == 'away': return 'away'
        if s in ('', '0-0', '1-1', '2-2', 'draw', 'null'): return None
        parts = s.split('-')
        if len(parts) == 2:
            try:
                h, a = int(parts[0]), int(parts[1])
                if h > a: return 'home'
                if a > h: return 'away'
            except: pass
    return None

def split_label(ts: int) -> str:
    if ts <= TRAIN_END:  return 'TRAIN'
    if ts <= VAL_END:    return 'VAL'
    if ts <= TEST_END:   return 'TEST'
    return 'FUTURE'


class RollingH2H:
    def __init__(self):
        self._data: dict = {}

    def _key(self, t1, t2):
        return tuple(sorted([t1, t2]))

    def get(self, home, away):
        k = self._key(home, away)
        d = self._data.get(k)
        if not d or d['total'] == 0:
            return 0, 0.5, 0.5, 0.0
        t1 = k[0]
        w_home = d['w1'] if t1 == home else d['w2']
        w_away = d['w2'] if t1 == home else d['w1']
        total = d['total']
        wr_h = w_home / total
        wr_a = w_away / total
        return total, wr_h, wr_a, wr_h - wr_a

    def update(self, home, away, winner):
        k = self._key(home, away)
        if k not in self._data:
            self._data[k] = {'w1': 0, 'w2': 0, 'total': 0}
        d = self._data[k]
        d['total'] += 1
        if winner == 'home':
            if k[0] == home: d['w1'] += 1
            else: d['w2'] += 1
        else:
            if k[0] == away: d['w1'] += 1
            else: d['w2'] += 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS tournament_blind_features (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL,
    match_date       TEXT NOT NULL,
    split            TEXT NOT NULL,
    division         TEXT NOT NULL,
    league           TEXT,
    home_team        TEXT,
    away_team        TEXT,
    market           TEXT DEFAULT '151_1',
    bookmaker        TEXT,
    start_time       INTEGER,
    open_home        REAL,
    open_away        REAL,
    market_prob_home REAL,
    market_prob_away REAL,
    elo_home         REAL,
    elo_away         REAL,
    elo_diff         REAL,
    elo_prob_home    REAL,
    edge_home        REAL,
    h2h_n            INTEGER DEFAULT 0,
    h2h_wr_home      REAL DEFAULT 0.5,
    h2h_wr_away      REAL DEFAULT 0.5,
    h2h_delta        REAL DEFAULT 0.0,
    adj_prob_home    REAL,
    pre_match_pts    INTEGER DEFAULT 0,
    first_open_prob  REAL,
    latest_pre_prob  REAL,
    pre_match_move   REAL,
    UNIQUE(event_id, bookmaker)
);

CREATE TABLE IF NOT EXISTS tournament_strategy_registry (
    strategy_name    TEXT PRIMARY KEY,
    description      TEXT,
    was_tuned_on     TEXT,
    allowed_splits   TEXT,
    is_oracle        INTEGER DEFAULT 0,
    is_posthoc       INTEGER DEFAULT 0,
    is_valid_for_test INTEGER DEFAULT 1,
    division_filter  TEXT DEFAULT 'A,B,C',
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS tournament_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def build_features():
    os.makedirs(os.path.dirname(TOURN_DB), exist_ok=True)

    tcon = sqlite3.connect(TOURN_DB)
    tcon.executescript(
        "DROP TABLE IF EXISTS tournament_blind_features;"
        "DROP TABLE IF EXISTS tournament_strategy_registry;"
        "DROP TABLE IF EXISTS tournament_meta;"
    )
    tcon.executescript(SCHEMA)
    tcon.commit()

    hcon = sqlite3.connect(HARVEST_DB)
    hcur = hcon.cursor()

    # ── 1. Все Dota2 матчи с котировками, хронологически ─────────────────────
    print("Загружаем матчи...", flush=True)
    all_events = hcur.execute("""
        SELECT DISTINCT re.event_id, re.home_team, re.away_team, re.league,
               re.start_time, re.score, re.winner
        FROM raw_events re
        JOIN odds_summary os ON re.event_id = os.event_id
            AND os.market = '151_1'
            AND os.open_home IS NOT NULL AND os.open_away IS NOT NULL
            AND os.close_home IS NOT NULL AND os.close_away IS NOT NULL
            AND os.open_home > 1.0 AND os.open_away > 1.0
        WHERE re.league LIKE 'DOTA2%'
        AND re.status = 'ended'
        AND CAST(re.start_time AS INTEGER) BETWEEN 1640995200 AND 1781654399
        ORDER BY CAST(re.start_time AS INTEGER) ASC
    """).fetchall()
    print(f"  Уникальных матчей: {len(all_events):,}", flush=True)

    # ── 2. Pre-match history ──────────────────────────────────────────────────
    print("Загружаем pre-match history...", flush=True)
    hist_rows = hcur.execute("""
        SELECT oh.event_id, oh.add_time, oh.home_od, oh.away_od
        FROM odds_history oh
        JOIN raw_events re ON re.event_id = oh.event_id
        WHERE re.league LIKE 'DOTA2%'
        AND oh.add_time IS NOT NULL AND oh.add_time != ''
        AND CAST(oh.add_time AS INTEGER) < CAST(re.start_time AS INTEGER)
        AND oh.home_od IS NOT NULL AND oh.home_od > 1.0
        AND oh.away_od IS NOT NULL AND oh.away_od > 1.0
        ORDER BY oh.event_id, CAST(oh.add_time AS INTEGER) ASC
    """).fetchall()
    hist_by_event: dict[str, list] = {}
    for eid, at, ho, ao in hist_rows:
        hist_by_event.setdefault(str(eid), []).append((int(at), ho, ao))
    print(f"  Pre-match points: {len(hist_rows):,} для {len(hist_by_event):,} матчей", flush=True)

    # ── 3. Котировки по матчу (все букмекеры) ────────────────────────────────
    print("Загружаем odds_summary...", flush=True)
    odds_by_event: dict[str, dict] = {}
    for row in hcur.execute("""
        SELECT event_id, bookmaker, open_home, open_away
        FROM odds_summary
        WHERE market = '151_1'
        AND open_home > 1.0 AND open_away > 1.0
    """).fetchall():
        eid, bm, oh, oa = row
        odds_by_event.setdefault(str(eid), {})[bm] = (oh, oa)

    # ── 4. Rolling Elo + H2H + вставка ───────────────────────────────────────
    elo: dict[str, float] = {}
    h2h = RollingH2H()
    tcur = tcon.cursor()

    counts = {'A': 0, 'B': 0, 'C': 0}
    skipped = 0

    print("Строим rolling features...", flush=True)
    for i, (eid, home, away, league, start_time, score, winner_col) in enumerate(all_events):
        st = int(start_time) if start_time else 0
        split = split_label(st)
        if split == 'FUTURE':
            continue

        match_date = datetime.datetime.fromtimestamp(st, tz=datetime.timezone.utc).strftime("%Y-%m-%d")

        # Rolling Elo ПЕРЕД обновлением
        elo_h = elo.get(home, START_ELO)
        elo_a = elo.get(away, START_ELO)
        elo_diff = abs(elo_h - elo_a)
        elo_prob_h = elo_expected(elo_h, elo_a)

        # H2H ПЕРЕД обновлением
        h2h_n, h2h_wr_h, h2h_wr_a, h2h_delta = h2h.get(home, away)

        # Adjusted prob
        if h2h_n >= 3:
            adj_h = (elo_prob_h + h2h_wr_h) / 2.0
        else:
            adj_h = elo_prob_h

        # Pre-match movement
        hist = hist_by_event.get(str(eid), [])
        pre_pts = len(hist)
        first_open_prob = latest_pre_prob = pre_match_move = None
        if hist:
            fh, fa = hist[0][1], hist[0][2]
            fp_h, _ = novig_prob(fh, fa)
            first_open_prob = fp_h
            lh, la = hist[-1][1], hist[-1][2]
            lp_h, _ = novig_prob(lh, la)
            latest_pre_prob = lp_h
            if fp_h is not None and lp_h is not None:
                pre_match_move = lp_h - fp_h

        # Котировки этого матча
        bm_odds = odds_by_event.get(str(eid), {})

        # Выбираем букмекера — предпочитаем Pinnacle
        bm_order = PREFERRED_BM_ORDER + [b for b in bm_odds if b not in PREFERRED_BM_ORDER]
        for bm in bm_order:
            if bm not in bm_odds:
                continue
            oh, oa = bm_odds[bm]
            mkt_h, mkt_a = novig_prob(oh, oa)
            if mkt_h is None:
                continue
            edge_h = elo_prob_h - mkt_h

            # Division
            if bm == 'PinnacleSports':
                if pre_pts >= 5 and pre_match_move is not None:
                    div = 'C'
                else:
                    div = 'B'
            else:
                div = 'A'

            try:
                tcur.execute("""
                    INSERT OR IGNORE INTO tournament_blind_features
                    (event_id, match_date, split, division, league,
                     home_team, away_team, market, bookmaker, start_time,
                     open_home, open_away, market_prob_home, market_prob_away,
                     elo_home, elo_away, elo_diff, elo_prob_home,
                     edge_home, h2h_n, h2h_wr_home, h2h_wr_away, h2h_delta,
                     adj_prob_home, pre_match_pts,
                     first_open_prob, latest_pre_prob, pre_match_move)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (str(eid), match_date, split, div, league,
                      home, away, '151_1', bm, st,
                      oh, oa, mkt_h, mkt_a,
                      elo_h, elo_a, elo_diff, elo_prob_h,
                      edge_h, h2h_n, h2h_wr_h, h2h_wr_a, h2h_delta,
                      adj_h, pre_pts, first_open_prob, latest_pre_prob, pre_match_move))
                counts[div] += 1
            except Exception:
                pass
            break  # Один букмекер на матч (лучший доступный)

        # Обновляем rolling Elo/H2H ПОСЛЕ вставки
        winner = derive_winner(home, away, winner_col, score)
        if winner:
            exp_h = elo_expected(elo_h, elo_a)
            actual_h = 1.0 if winner == 'home' else 0.0
            elo[home] = elo_h + K_FACTOR * (actual_h - exp_h)
            elo[away] = elo_a + K_FACTOR * ((1 - actual_h) - (1 - exp_h))
            h2h.update(home, away, winner)
        else:
            skipped += 1

        if (i + 1) % 2000 == 0:
            tcon.commit()
            print(f"  [{i+1:,}/{len(all_events):,}] A={counts['A']:,} B={counts['B']:,} C={counts['C']:,}", flush=True)

    tcon.commit()

    # ── 5. Strategy registry ──────────────────────────────────────────────────
    strategies = [
        ('M00','No Bet Baseline','N/A','ALL',0,0,1,'A,B,C','Never bets — baseline'),
        ('M01','Market Favorite','N/A','ALL',0,0,1,'A,B,C','Bet favorite odds < 2.0'),
        ('M02','Market Fav 60-70','N/A','ALL',0,0,1,'A,B,C','Bet fav market_prob 60-70%'),
        ('M03','Elo Value','N/A','ALL',0,0,1,'A,B,C','elo_prob - market_prob > 0'),
        ('M04','H2H Value','N/A','ALL',0,0,1,'A,B,C','adj_prob - market_prob > 0'),
        ('M05','Rule C Frozen','FROZEN','ALL',0,0,1,'A,B,C','edge>0 AND elo_diff>=75 AND odds<2.0 AND mkt_prob 60-70%'),
        ('M06','Rule C Plus','POSTHOC','ALL',0,1,0,'A,B,C','edge>=0.07 AND elo_diff>=75 AND odds<2.0 AND mkt_prob 60-70%'),
        ('M07','Elo Strong 150+','N/A','ALL',0,0,1,'A,B,C','edge>0 AND elo_diff>=150 AND odds<2.0'),
        ('M08','H2H Positive','N/A','ALL',0,0,1,'A,B,C','h2h_delta>0.02 AND edge>0 AND odds<2.0'),
        ('M09','Conservative Fund','N/A','ALL',0,0,1,'A,B,C','edge>0 AND odds<1.7 AND mkt_prob 60-75% AND elo_diff>=75'),
        ('M10','Aggressive Fund','N/A','ALL',0,0,1,'A,B,C','edge>0.05 AND odds<2.2 AND elo_diff>=50'),
        ('M11','DreamLeague Spec','N/A','ALL',0,0,1,'A,B,C','DreamLeague AND edge>0'),
        ('M12','EPL Specialist','N/A','ALL',0,0,1,'A,B,C','EPL AND edge>0'),
        ('M13','PGL Specialist','N/A','ALL',0,0,1,'A,B,C','PGL AND edge>0'),
        ('M14','BM Disagreement','N/A','ALL',0,0,1,'A,B','Bookmaker prob gap > 5%'),
        ('M15','Line Move Early','TRAIN,VAL','TEST',0,0,1,'C','pre_match_move>0.03 (tuned TRAIN/VAL only)'),
        ('M16','Closing Oracle','LEAKY','ALL',1,0,0,'A,B,C','LEAKY uses close odds — benchmark only'),
    ]
    tcur.executemany("INSERT OR REPLACE INTO tournament_strategy_registry VALUES (?,?,?,?,?,?,?,?,?)", strategies)

    now = datetime.datetime.utcnow().isoformat()
    for k, v in [('built_at', now), ('div_a', str(counts['A'])),
                 ('div_b', str(counts['B'])), ('div_c', str(counts['C'])),
                 ('skipped_no_result', str(skipped))]:
        tcur.execute("INSERT OR REPLACE INTO tournament_meta VALUES (?,?)", (k, v))
    tcon.commit()

    # ── 6. Leakage assertion ──────────────────────────────────────────────────
    cols = {r[1] for r in tcur.execute("PRAGMA table_info(tournament_blind_features)").fetchall()}
    leaks = cols & FORBIDDEN_COLS
    assert not leaks, f"LEAKAGE: {leaks}"

    total = sum(counts.values())
    print(f"\n{'='*55}")
    print(f"BLIND FEATURES ГОТОВЫ  (anti-leakage: ✅ PASS)")
    print(f"  Division A : {counts['A']:,}")
    print(f"  Division B : {counts['B']:,}")
    print(f"  Division C : {counts['C']:,}")
    print(f"  Total      : {total:,}")
    print(f"  Skipped (no result): {skipped:,}")
    print(f"\nСплиты:")
    for row in tcur.execute("""
        SELECT split, division, COUNT(*) FROM tournament_blind_features
        GROUP BY split, division ORDER BY split, division
    """).fetchall():
        print(f"  {row[0]:<6} Div-{row[1]} : {row[2]:,}")
    print(f"{'='*55}")

    tcon.close()
    hcon.close()


if __name__ == "__main__":
    build_features()
