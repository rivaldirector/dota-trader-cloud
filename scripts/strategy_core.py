"""
strategy_core.py — общая логика Strategy Tournament.
Содержит: DB-схема, 12 стратегий, bank-менеджмент, утилиты.
"""
from __future__ import annotations
import json, sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from math import pow
from pathlib import Path

ROOT     = Path(__file__).parent.parent
ELO_DB   = ROOT / "storage" / "dota_research.sqlite3"
TOUR_DB  = ROOT / "storage" / "strategy_tournament.db"

STARTING_BANK  = 1000.0
STAKE_PCT      = 0.02          # 2% от текущего банка
START_ELO      = 1500.0
K_FACTOR       = 32
PREFERRED_BM   = ["PinnacleSports", "Bet365", "GGBet", "MelBet", "YSB88"]

# ── DB Schema ─────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS strategy_bankrolls (
    strategy_name     TEXT PRIMARY KEY,
    starting_bank_usd REAL DEFAULT 1000.0,
    current_bank_usd  REAL DEFAULT 1000.0,
    total_staked_usd  REAL DEFAULT 0.0,
    total_profit_usd  REAL DEFAULT 0.0,
    roi_pct           REAL DEFAULT 0.0,
    max_drawdown_usd  REAL DEFAULT 0.0,
    max_drawdown_pct  REAL DEFAULT 0.0,
    bets_count        INTEGER DEFAULT 0,
    wins              INTEGER DEFAULT 0,
    losses            INTEGER DEFAULT 0,
    pushes            INTEGER DEFAULT 0,
    last_updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS strategy_daily_predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT,
    prediction_date     TEXT,
    strategy_name       TEXT,
    event_id            TEXT,
    league              TEXT,
    team_1              TEXT,
    team_2              TEXT,
    start_time          INTEGER,
    market              TEXT,
    model_pick          TEXT,
    odds                REAL,
    market_prob         REAL,
    model_prob          REAL,
    edge                REAL,
    confidence          TEXT,
    reason_code         TEXT,
    strategy_bank_before REAL,
    stake_usd           REAL DEFAULT 0.0,
    stake_pct           REAL DEFAULT 0.0,
    expected_profit_usd REAL DEFAULT 0.0,
    bet_status          TEXT DEFAULT 'NO_BET',
    no_bet_reason       TEXT,
    result              TEXT,
    profit_usd          REAL,
    close_odds          REAL,
    clv                 REAL,
    settled_at          TEXT,
    raw_json            TEXT,
    UNIQUE(prediction_date, strategy_name, event_id)
);

CREATE INDEX IF NOT EXISTS idx_sdp_date     ON strategy_daily_predictions(prediction_date);
CREATE INDEX IF NOT EXISTS idx_sdp_strategy ON strategy_daily_predictions(strategy_name);
CREATE INDEX IF NOT EXISTS idx_sdp_event    ON strategy_daily_predictions(event_id);
CREATE INDEX IF NOT EXISTS idx_sdp_status   ON strategy_daily_predictions(bet_status);
"""

STRATEGIES = [
    "Rule_C",
    "Rule_C_plus",
    "Rule_Elo150",
    "Rule_H2H",
    "Rule_Favorite_60_70",
    "Rule_CLV_TopEdge",
    "Rule_DreamLeague",
    "Rule_EPL",
    "Rule_TotalMaps_Under",
    "Rule_TotalMaps_Over",
    "Rule_Handicap_Favorite",
    "Rule_MarketFavorite",
]


# ── DB helpers ────────────────────────────────────────────────────────────────

def open_tournament_db() -> sqlite3.Connection:
    TOUR_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TOUR_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def ensure_bankrolls(conn: sqlite3.Connection):
    """Создать записи банков для всех стратегий если их нет."""
    ts = now_iso()
    for name in STRATEGIES:
        conn.execute("""
            INSERT OR IGNORE INTO strategy_bankrolls
              (strategy_name, starting_bank_usd, current_bank_usd, last_updated_at)
            VALUES(?, ?, ?, ?)
        """, (name, STARTING_BANK, STARTING_BANK, ts))
    conn.commit()


def get_bank(conn: sqlite3.Connection, strategy: str) -> float:
    r = conn.execute(
        "SELECT current_bank_usd FROM strategy_bankrolls WHERE strategy_name=?",
        (strategy,)
    ).fetchone()
    return r["current_bank_usd"] if r else STARTING_BANK


def update_bank(conn: sqlite3.Connection, strategy: str, profit: float,
                staked: float, won: bool | None):
    """Обновить банк стратегии после settle."""
    row = conn.execute(
        "SELECT * FROM strategy_bankrolls WHERE strategy_name=?", (strategy,)
    ).fetchone()
    if not row: return

    new_bank    = row["current_bank_usd"] + profit
    new_staked  = row["total_staked_usd"] + staked
    new_profit  = row["total_profit_usd"] + profit
    new_roi     = (new_bank - row["starting_bank_usd"]) / row["starting_bank_usd"] * 100
    new_bets    = row["bets_count"] + (1 if staked > 0 else 0)
    new_wins    = row["wins"] + (1 if won is True else 0)
    new_losses  = row["losses"] + (1 if won is False else 0)

    # Max drawdown
    peak = row["starting_bank_usd"]   # упрощённо — старт как пик
    dd_usd = max(0.0, row["max_drawdown_usd"], peak - new_bank)
    dd_pct = max(0.0, row["max_drawdown_pct"], dd_usd / peak * 100)

    conn.execute("""
        UPDATE strategy_bankrolls SET
          current_bank_usd=?, total_staked_usd=?, total_profit_usd=?,
          roi_pct=?, bets_count=?, wins=?, losses=?,
          max_drawdown_usd=?, max_drawdown_pct=?, last_updated_at=?
        WHERE strategy_name=?
    """, (new_bank, new_staked, new_profit, new_roi, new_bets,
          new_wins, new_losses, dd_usd, dd_pct, now_iso(), strategy))
    conn.commit()


# ── Elo ───────────────────────────────────────────────────────────────────────

def elo_exp(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


def build_elo() -> tuple[dict, dict]:
    conn = sqlite3.connect(ELO_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT team_1_name, team_2_name, winner_name
        FROM matches WHERE status='finished'
          AND team_1_name IS NOT NULL AND team_2_name IS NOT NULL AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()
    conn.close()
    elo, games = {}, {}
    for r in rows:
        t1, t2, w = r[0], r[1], r[2]
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        ea = elo_exp(e1, e2); s1 = 1 if w == t1 else 0
        elo[t1]  = e1 + K_FACTOR*(s1-ea)
        elo[t2]  = e2 + K_FACTOR*((1-s1)-(1-ea))
        games[t1] = games.get(t1, 0) + 1
        games[t2] = games.get(t2, 0) + 1
    return elo, games


def build_h2h() -> dict[tuple, dict]:
    """Возвращает dict {(t1_norm, t2_norm): {wins_t1, wins_t2, total}}."""
    conn = sqlite3.connect(ELO_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT team_1_name, team_2_name, winner_name
        FROM matches WHERE status='finished'
          AND team_1_name IS NOT NULL AND team_2_name IS NOT NULL AND winner_name IS NOT NULL
    """).fetchall()
    conn.close()
    h2h: dict = defaultdict(lambda: {"w1": 0, "w2": 0, "total": 0})
    for r in rows:
        t1, t2, w = r[0], r[1], r[2]
        key = tuple(sorted([t1, t2]))
        h2h[key]["total"] += 1
        if w == key[0]: h2h[key]["w1"] += 1
        else:           h2h[key]["w2"] += 1
    return dict(h2h)


# ── Team fuzzy match ──────────────────────────────────────────────────────────

def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def best_match(name: str, candidates: list, thr: float = 0.6) -> str | None:
    best, sc = None, 0.0
    for c in candidates:
        s = fuzzy(name, c)
        if s > sc: best, sc = c, s
    return best if sc >= thr else None


# ── Odds utils ────────────────────────────────────────────────────────────────

def novig(h, a):
    if not h or not a or h <= 1 or a <= 1: return None, None
    t = 1/h + 1/a
    return (1/h)/t, (1/a)/t

def safe_float(v) -> float | None:
    try: return float(v) if v and str(v) not in ("-","") else None
    except: return None


# ── Misc ──────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Match data container ──────────────────────────────────────────────────────

class MatchData:
    """Всё что знаем о матче для принятия решения стратегией."""
    def __init__(self, event_id, home, away, league, start_time,
                 elo_h, elo_a, elo_diff, model_prob,
                 odds_151_1: dict | None = None,
                 odds_151_2: dict | None = None,
                 odds_151_3: dict | None = None,
                 h2h: dict | None = None):
        self.event_id   = event_id
        self.home       = home
        self.away       = away
        self.league     = league
        self.start_time = start_time
        self.elo_h      = elo_h
        self.elo_a      = elo_a
        self.elo_diff   = elo_diff
        self.model_prob = model_prob   # вероятность победы home
        self.h2h        = h2h or {}    # {"w_home": N, "w_away": N, "total": N}

        # 151_1 Match Winner
        self.odds_h    = odds_151_1.get("home_od") if odds_151_1 else None
        self.odds_a    = odds_151_1.get("away_od") if odds_151_1 else None
        self.bm_151_1  = odds_151_1.get("bookmaker") if odds_151_1 else None
        p1h, p1a       = novig(self.odds_h, self.odds_a)
        self.mkt_prob  = p1h   # market prob home
        self.edge      = round(model_prob - p1h, 4) if p1h else None

        # 151_2 Handicap
        self.odds_151_2 = odds_151_2   # {"home_od", "away_od", "handicap", "bookmaker"}

        # 151_3 Total Maps
        self.odds_151_3 = odds_151_3   # {"over_od", "under_od", "bookmaker"}


# ── Strategy result ───────────────────────────────────────────────────────────

class StrategyResult:
    def __init__(self, bet: bool, pick: str | None, odds: float | None,
                 market: str, mkt_prob: float | None, model_prob: float | None,
                 edge: float | None, confidence: str, reason_code: str,
                 no_bet_reason: str | None = None):
        self.bet         = bet
        self.pick        = pick
        self.odds        = odds
        self.market      = market
        self.mkt_prob    = mkt_prob
        self.model_prob  = model_prob
        self.edge        = edge
        self.confidence  = confidence
        self.reason_code = reason_code
        self.no_bet_reason = no_bet_reason

    def status(self) -> str:
        return "BET" if self.bet else "NO_BET"


def NO_BET(reason: str, market: str = "151_1") -> StrategyResult:
    return StrategyResult(False, None, None, market, None, None, None,
                          "low", "no_signal", reason)


# ── 12 Стратегий ─────────────────────────────────────────────────────────────

def strategy_Rule_C(m: MatchData) -> StrategyResult:
    """FROZEN Rule C: elo_diff≥75, edge>0, odds<2.0, mkt_prob 60-70%"""
    if m.odds_h is None:
        return NO_BET("market_unavailable")
    if m.edge is None or m.edge <= 0:
        return NO_BET(f"edge={m.edge or 0:+.3f}")
    if m.elo_diff < 75:
        return NO_BET(f"elo_diff={m.elo_diff:.0f}<75")
    if m.odds_h >= 2.0:
        return NO_BET(f"odds={m.odds_h:.3f}>=2.0")
    if m.mkt_prob is None or not (0.60 <= m.mkt_prob <= 0.70):
        return NO_BET(f"mkt={m.mkt_prob:.3f}_outside_60-70%")
    conf = "high" if m.elo_diff >= 100 else "medium"
    return StrategyResult(True, "home", m.odds_h, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, conf, "rule_c_signal")


def strategy_Rule_C_plus(m: MatchData) -> StrategyResult:
    """Rule C + строже: edge≥0.05, elo_diff≥100"""
    if m.odds_h is None:
        return NO_BET("market_unavailable")
    if m.edge is None or m.edge < 0.05:
        return NO_BET(f"edge={m.edge or 0:+.3f}<0.05")
    if m.elo_diff < 100:
        return NO_BET(f"elo_diff={m.elo_diff:.0f}<100")
    if m.odds_h >= 2.0:
        return NO_BET(f"odds={m.odds_h:.3f}>=2.0")
    if m.mkt_prob is None or not (0.60 <= m.mkt_prob <= 0.70):
        return NO_BET(f"mkt={m.mkt_prob:.3f}_outside_60-70%")
    return StrategyResult(True, "home", m.odds_h, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "high", "rule_c_plus_signal")


def strategy_Rule_Elo150(m: MatchData) -> StrategyResult:
    """Сильное Elo расхождение ≥150, любые mkt_prob, edge>0"""
    if m.odds_h is None:
        return NO_BET("market_unavailable")
    if m.elo_diff < 150:
        return NO_BET(f"elo_diff={m.elo_diff:.0f}<150")
    if m.edge is None or m.edge <= 0:
        return NO_BET(f"edge={m.edge or 0:+.3f}<=0")
    pick  = "home" if m.edge > 0 else "away"
    odds  = m.odds_h if pick == "home" else m.odds_a
    return StrategyResult(True, pick, odds, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "high", "elo150_signal")


def strategy_Rule_H2H(m: MatchData) -> StrategyResult:
    """H2H win rate >60%, минимум 3 матча, edge>0"""
    if not m.h2h or m.h2h.get("total", 0) < 3:
        return NO_BET("no_h2h_data")
    if m.odds_h is None:
        return NO_BET("market_unavailable")
    total   = m.h2h["total"]
    w_home  = m.h2h.get("w_home", 0)
    w_away  = m.h2h.get("w_away", 0)
    h2h_wr  = w_home / total
    if h2h_wr > 0.60 and (m.edge or 0) > 0:
        return StrategyResult(True, "home", m.odds_h, "151_1",
                              m.mkt_prob, m.model_prob, m.edge, "medium",
                              f"h2h_wr={h2h_wr:.0%}")
    elif (1 - h2h_wr) > 0.60 and (m.edge or 0) < 0:
        edge_a = -(m.edge or 0)
        return StrategyResult(True, "away", m.odds_a, "151_1",
                              1 - (m.mkt_prob or 0), 1 - m.model_prob,
                              edge_a, "medium", f"h2h_wr_away={(1-h2h_wr):.0%}")
    return NO_BET(f"h2h_wr={h2h_wr:.0%}_below_threshold")


def strategy_Rule_Favorite_60_70(m: MatchData) -> StrategyResult:
    """Market favourite 60-70%, edge>0. Без Elo фильтра."""
    if m.odds_h is None or m.mkt_prob is None:
        return NO_BET("market_unavailable")
    if not (0.60 <= m.mkt_prob <= 0.70):
        return NO_BET(f"mkt={m.mkt_prob:.3f}_outside_60-70%")
    if m.edge is None or m.edge <= 0:
        return NO_BET(f"edge={m.edge or 0:+.3f}<=0")
    return StrategyResult(True, "home", m.odds_h, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "medium", "fav_60_70_signal")


def strategy_Rule_CLV_TopEdge(m: MatchData) -> StrategyResult:
    """Placeholder — топ-1 edge за день выбирается после обработки всех матчей."""
    # Логика отбора топа реализована в daily_strategy_run.py post-pass
    if m.odds_h is None or m.edge is None or m.edge <= 0:
        return NO_BET("edge_not_positive")
    return StrategyResult(True, "home", m.odds_h, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "medium", "top_edge_candidate")


def strategy_Rule_DreamLeague(m: MatchData) -> StrategyResult:
    """Только матчи DreamLeague, edge>0"""
    league = (m.league or "").lower()
    if "dreamleague" not in league and "dream league" not in league:
        return NO_BET("wrong_league_not_dreamleague")
    if m.odds_h is None:
        return NO_BET("market_unavailable")
    if m.edge is None or m.edge <= 0:
        return NO_BET(f"edge={m.edge or 0:+.3f}<=0")
    pick = "home" if m.edge > 0 else "away"
    odds = m.odds_h if pick == "home" else m.odds_a
    return StrategyResult(True, pick, odds, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "medium", "dreamleague_signal")


def strategy_Rule_EPL(m: MatchData) -> StrategyResult:
    """Только матчи European Pro League, edge>0"""
    league = (m.league or "").lower()
    if "european pro league" not in league and "epl" not in league:
        return NO_BET("wrong_league_not_epl")
    if m.odds_h is None:
        return NO_BET("market_unavailable")
    if m.edge is None or m.edge <= 0:
        return NO_BET(f"edge={m.edge or 0:+.3f}<=0")
    pick = "home" if m.edge > 0 else "away"
    odds = m.odds_h if pick == "home" else m.odds_a
    return StrategyResult(True, pick, odds, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "medium", "epl_signal")


def strategy_Rule_TotalMaps_Under(m: MatchData) -> StrategyResult:
    """Тотал карт Under 2.5, mkt_prob_under > 55%, model подтверждает"""
    od = m.odds_151_3
    if not od:
        return NO_BET("market_151_3_unavailable")
    under_od = od.get("under_od")
    over_od  = od.get("over_od")
    if not under_od or not over_od:
        return NO_BET("total_maps_odds_missing")
    mkt_under, mkt_over = novig(under_od, over_od)
    if not mkt_under or mkt_under <= 0.55:
        mu_s = f"{mkt_under:.3f}" if mkt_under else "?"
        return NO_BET(f"mkt_under={mu_s}_below_55%")
    return StrategyResult(True, "under", under_od, "151_3",
                          mkt_under, None, None, "medium", "total_maps_under_signal")


def strategy_Rule_TotalMaps_Over(m: MatchData) -> StrategyResult:
    """Тотал карт Over 2.5, mkt_prob_over > 55%"""
    od = m.odds_151_3
    if not od:
        return NO_BET("market_151_3_unavailable")
    under_od = od.get("under_od")
    over_od  = od.get("over_od")
    if not under_od or not over_od:
        return NO_BET("total_maps_odds_missing")
    mkt_under, mkt_over = novig(under_od, over_od)
    if not mkt_over or mkt_over <= 0.55:
        mo_s = f"{mkt_over:.3f}" if mkt_over else "?"
        return NO_BET(f"mkt_over={mo_s}_below_55%")
    return StrategyResult(True, "over", over_od, "151_3",
                          mkt_over, None, None, "medium", "total_maps_over_signal")


def strategy_Rule_Handicap_Favorite(m: MatchData) -> StrategyResult:
    """Гандикап ±1.5: ставим на сильного фаворита, elo_diff≥100, mkt_prob > 65%"""
    od = m.odds_151_2
    if not od:
        return NO_BET("market_151_2_unavailable")
    if m.elo_diff < 100:
        return NO_BET(f"elo_diff={m.elo_diff:.0f}<100")
    hcap_h = od.get("home_od"); hcap_a = od.get("away_od")
    if not hcap_h or not hcap_a:
        return NO_BET("handicap_odds_missing")
    mkt_h, mkt_a = novig(hcap_h, hcap_a)
    if not mkt_h:
        return NO_BET("handicap_novig_failed")
    if m.elo_h > m.elo_a and mkt_h > 0.65:
        return StrategyResult(True, "home", hcap_h, "151_2",
                              mkt_h, m.model_prob, None, "medium", "handicap_fav_home")
    elif m.elo_a > m.elo_h and mkt_a > 0.65:
        return StrategyResult(True, "away", hcap_a, "151_2",
                              mkt_a, 1-m.model_prob, None, "medium", "handicap_fav_away")
    return NO_BET(f"handicap_mkt_below_65%")


def strategy_Rule_MarketFavorite(m: MatchData) -> StrategyResult:
    """Market prob > 62%, odds < 1.80, Elo подтверждает (model_prob > mkt_prob - 0.03)"""
    if m.odds_h is None or m.mkt_prob is None:
        return NO_BET("market_unavailable")
    if m.mkt_prob <= 0.62:
        return NO_BET(f"mkt={m.mkt_prob:.3f}<=62%")
    if m.odds_h >= 1.80:
        return NO_BET(f"odds={m.odds_h:.3f}>=1.80")
    # Elo не должен сильно расходиться
    if m.model_prob < m.mkt_prob - 0.08:
        return NO_BET(f"model={m.model_prob:.3f}_contradicts_market")
    return StrategyResult(True, "home", m.odds_h, "151_1",
                          m.mkt_prob, m.model_prob, m.edge, "medium", "market_fav_signal")


# ── CLV TopEdge post-pass ────────────────────────────────────────────────────

def apply_clv_top_edge(flat: list) -> list:
    """
    flat: [(match, strategy_name, sr), ...]
    Rule_CLV_TopEdge: из всех матчей оставляем ставку только на 1 с наибольшим edge.
    Остальные CLV_TopEdge → NO_BET.
    """
    candidates = [
        (i, sr.edge)
        for i, (_, s, sr) in enumerate(flat)
        if s == "Rule_CLV_TopEdge" and sr.bet and sr.edge is not None and sr.edge > 0
    ]
    if not candidates:
        return flat

    best_i = max(candidates, key=lambda x: x[1])[0]
    out = []
    for i, (m, s, sr) in enumerate(flat):
        if s == "Rule_CLV_TopEdge" and i != best_i and sr.bet:
            sr = NO_BET("not_top_edge_today")
        out.append((m, s, sr))
    return out


# ── Strategy dispatcher ───────────────────────────────────────────────────────

STRATEGY_FN = {
    "Rule_C":               strategy_Rule_C,
    "Rule_C_plus":          strategy_Rule_C_plus,
    "Rule_Elo150":          strategy_Rule_Elo150,
    "Rule_H2H":             strategy_Rule_H2H,
    "Rule_Favorite_60_70":  strategy_Rule_Favorite_60_70,
    "Rule_CLV_TopEdge":     strategy_Rule_CLV_TopEdge,
    "Rule_DreamLeague":     strategy_Rule_DreamLeague,
    "Rule_EPL":             strategy_Rule_EPL,
    "Rule_TotalMaps_Under": strategy_Rule_TotalMaps_Under,
    "Rule_TotalMaps_Over":  strategy_Rule_TotalMaps_Over,
    "Rule_Handicap_Favorite": strategy_Rule_Handicap_Favorite,
    "Rule_MarketFavorite":  strategy_Rule_MarketFavorite,
}

def run_strategy(name: str, match: MatchData) -> StrategyResult:
    fn = STRATEGY_FN.get(name)
    if fn is None:
        return NO_BET("unknown_strategy")
    try:
        return fn(match)
    except Exception as e:
        return NO_BET(f"error:{e}")
