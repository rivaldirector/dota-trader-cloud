"""
_paper_core.py — shared model + DB logic for paper trading system.
Frozen. Do not modify.
"""
from __future__ import annotations

import re, sqlite3, random
from collections import defaultdict
from datetime import datetime, timezone
from math import pow as mpow
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────
START_ELO  = 1500.0
MIN_GAMES  = 3
H2H_MAX_W  = 0.40
H2H_CONF_N = 5.0
N_BOOT     = 5_000
BOOT_SEED  = 42

PREF_BM = ["Bet365","Pinnacle","PinnacleSports","GGBet","10Bet",
           "188Bet","FonBet","MelBet","CashPoint","888Sport"]

# Frozen Rule C
def RULE_C(edge_adj, elo_diff, bet_odds, market_prob):
    return (edge_adj > 0
            and elo_diff >= 75
            and bet_odds < 2.0
            and 0.60 <= market_prob < 0.70)

# Historical prior for Bayesian WR (n=24, wins=21 from historical Rule C)
PRIOR_WINS   = 22   # alpha = wins + 1
PRIOR_LOSSES = 4    # beta  = losses + 1

# ── DB path ───────────────────────────────────────────────────────────────────
import sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
from config import settings

MAIN_DB  = _ROOT / settings.database_path
PAPER_DB = _ROOT / "data" / "paper_trades.db"

# ── Paper DB init ─────────────────────────────────────────────────────────────

def get_paper_conn() -> sqlite3.Connection:
    PAPER_DB.parent.mkdir(parents=True, exist_ok=True)
    # Remove stale journal/wal files that can cause disk I/O errors
    for suffix in ["-journal", "-wal", "-shm"]:
        stale = Path(str(PAPER_DB) + suffix)
        try:
            if stale.exists():
                stale.unlink()
        except Exception:
            pass
    # If DB file is empty/corrupt, remove and recreate
    if PAPER_DB.exists() and PAPER_DB.stat().st_size == 0:
        try:
            PAPER_DB.unlink()
        except Exception:
            pass
    try:
        conn = sqlite3.connect(str(PAPER_DB))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        return conn
    except sqlite3.OperationalError:
        # DB is corrupt — try to remove and recreate
        try:
            PAPER_DB.unlink()
        except Exception:
            pass
        conn = sqlite3.connect(str(PAPER_DB))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        return conn

def _init_schema(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS paper_trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT    NOT NULL,
        match_id      TEXT    NOT NULL,
        league        TEXT,
        team_1        TEXT,
        team_2        TEXT,
        bet_team      TEXT    NOT NULL,
        opponent_team TEXT,
        start_time    TEXT,
        bookmaker     TEXT,
        open_odds     REAL,
        current_odds  REAL,
        market_prob   REAL,
        elo_prob      REAL,
        adj_prob      REAL,
        edge_adj      REAL,
        elo_diff      REAL,
        h2h_n         INTEGER,
        h2h_delta     REAL,
        status        TEXT    DEFAULT 'PENDING',
        result        TEXT,
        close_odds    REAL,
        clv           REAL,
        profit_flat   REAL,
        raw_json      TEXT,
        UNIQUE(match_id, bet_team)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS odds_movements (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id    INTEGER REFERENCES paper_trades(id),
        captured_at TEXT,
        bookmaker   TEXT,
        odds        REAL,
        market_prob REAL
    )
    """)
    conn.commit()

# ── String helpers ────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def team_match(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

def novig(h: float, a: float):
    if not h or not a or h <= 1 or a <= 1: return None, None
    ph, pa = 1/h, 1/a; s = ph+pa; return ph/s, pa/s

def get_mp(m_t1, m_t2, o_t1, o_t2, h, a):
    if team_match(m_t1,o_t1) and team_match(m_t2,o_t2):
        p1,p2 = novig(h,a); return p1,p2,True
    if team_match(m_t1,o_t2) and team_match(m_t2,o_t1):
        p2,p1 = novig(h,a); return p1,p2,True
    return None,None,False

def elo_prob(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + mpow(10.0, (rb-ra)/400.0))

# ── Model: build Elo + H2H state from all finished matches ───────────────────

def build_model_state(main_conn) -> dict:
    """
    Returns dict with current elo ratings, games played, h2h history.
    Uses ALL finished matches in main DB (frozen algorithm).
    """
    from models.team_rating import _tier_k, _time_weight

    matches = main_conn.execute("""
        SELECT external_id, league_name, begin_at,
               team_1_name, team_2_name, winner_name
        FROM matches
        WHERE status='finished'
          AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL
          AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()

    now_dt = datetime.now(timezone.utc)
    elo    = defaultdict(lambda: START_ELO)
    games  = defaultdict(int)
    h2h    = defaultdict(list)   # key -> [(ts, key0_won)]

    for r in matches:
        t1,t2,win = r["team_1_name"],r["team_2_name"],r["winner_name"]
        ln = r["league_name"] or ""; bat = r["begin_at"] or ""
        result = 1 if win==t1 else 0

        # H2H before update
        key = (min(t1,t2), max(t1,t2))
        # Elo update
        k   = _tier_k(ln); w = _time_weight(bat, now_dt, 365); kef = k*w
        e1,e2 = elo[t1],elo[t2]
        ex  = elo_prob(e1,e2)
        elo[t1] = e1 + kef*(result - ex)
        elo[t2] = e2 + kef*((1-result) - (1-ex))
        games[t1] += 1; games[t2] += 1
        try: ts = datetime.fromisoformat(bat.replace("Z","+00:00")).timestamp()
        except: ts = now_dt.timestamp()
        h2h[key].append((ts, win==key[0]))

    return {"elo": dict(elo), "games": dict(games), "h2h": dict(h2h)}


def evaluate_match(t1, t2, mp_open, now_ts, state) -> dict:
    """
    Compute all model metrics for a match using frozen model state.
    Returns dict with elo_diff, elo_prob, adj_prob, h2h_n, h2h_delta, edge_adj.
    """
    elo   = state["elo"]
    games = state["games"]
    h2h   = state["h2h"]

    e1 = elo.get(t1, START_ELO)
    e2 = elo.get(t2, START_ELO)
    g1 = games.get(t1, 0)
    g2 = games.get(t2, 0)

    if g1 < MIN_GAMES or g2 < MIN_GAMES:
        return None   # not enough games

    ep    = elo_prob(e1, e2)
    ediff = abs(e1 - e2)

    # H2H
    key = (min(t1,t2), max(t1,t2))
    h2e = h2h.get(key, [])
    if h2e:
        ww=wt=0.0
        for (ts, k0w) in h2e:
            w = 0.5**((now_ts-ts)/365/86400); wt += w
            if (k0w and t1==key[0]) or (not k0w and t1==key[1]): ww += w
        h2h_n  = len(h2e)
        h2h_wr = ww/wt if wt>0 else 0.5
    else:
        h2h_n=0; h2h_wr=0.5

    hc  = min(h2h_n/H2H_CONF_N, 1.0)
    adj = ep*(1-H2H_MAX_W*hc) + h2h_wr*(H2H_MAX_W*hc)
    h2h_delta = adj - ep
    edge_adj  = adj - mp_open   # mp_open is prob of t1

    return dict(
        elo_diff=ediff, elo_prob=ep, adj_prob=adj,
        h2h_n=h2h_n, h2h_delta=h2h_delta, edge_adj=edge_adj,
    )


def best_odds_for_match(main_conn, eid: str):
    """
    Returns (bookmaker, t1_open, t2_open, t1_snap_name, t2_snap_name, t1_close, t2_close)
    using preferred bookmaker from odds_snapshots.
    Returns None if no odds found.
    """
    snaps_raw = main_conn.execute("""
        SELECT bookmaker, captured_at, team_1_name, team_2_name,
               team_1_odds, team_2_odds
        FROM odds_snapshots
        WHERE match_external_id=?
          AND team_1_odds IS NOT NULL AND team_2_odds IS NOT NULL
          AND team_1_odds > 1 AND team_2_odds > 1
    """, (eid,)).fetchall()

    if not snaps_raw: return None

    by_bm = defaultdict(dict)
    for s in snaps_raw:
        tag = "open" if s["captured_at"].endswith("_open") else "close"
        by_bm[s["bookmaker"]][tag] = {
            "h": s["team_1_odds"], "a": s["team_2_odds"],
            "t1": s["team_1_name"], "t2": s["team_2_name"],
        }

    bm = next((b for b in PREF_BM if b in by_bm and "open" in by_bm[b]), None)
    if not bm: bm = next((b for b,d in by_bm.items() if "open" in d), None)
    if not bm: return None

    op = by_bm[bm]["open"]
    cl = by_bm[bm].get("close", op)
    return dict(bm=bm, oh=op["h"], oa=op["a"], ch=cl["h"], ca=cl["a"],
                t1=op["t1"], t2=op["t2"])


# ── Stats ─────────────────────────────────────────────────────────────────────

def bootstrap_ci(vals, n=N_BOOT, seed=BOOT_SEED):
    if len(vals) < 2: return float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(vals,k=len(vals)))/len(vals) for _ in range(n))
    return means[int(0.025*n)], means[int(0.975*n)]

def p_positive(vals, n=N_BOOT, seed=BOOT_SEED):
    if not vals: return float("nan")
    rng = random.Random(seed)
    means = [sum(rng.choices(vals,k=len(vals)))/len(vals) for _ in range(n)]
    return sum(1 for m in means if m > 0) / n

def bayesian_wr(new_wins, new_n):
    """Beta posterior given historical prior + new observations."""
    alpha = PRIOR_WINS  + new_wins
    beta  = PRIOR_LOSSES + (new_n - new_wins)
    mean  = alpha / (alpha + beta)
    std   = (alpha*beta / ((alpha+beta)**2*(alpha+beta+1)))**0.5
    return mean, max(0, mean-1.96*std), min(1, mean+1.96*std)

def compute_stats(trades: list) -> dict | None:
    """trades: list of sqlite3.Row or dict from paper_trades."""
    settled = [t for t in trades if t["status"] in ("WON","LOST")]
    if not settled: return None
    n = len(settled)
    wins = sum(1 for t in settled if t["status"]=="WON")
    profits = [t["profit_flat"] for t in settled]
    clvs    = [t["clv"] or 0.0 for t in settled]
    roi     = sum(profits)/n
    wr      = wins/n
    clvpos  = sum(1 for c in clvs if c>0)/n
    avg_clv = sum(clvs)/n
    lo, hi  = bootstrap_ci(profits)
    p_pos   = p_positive(profits)
    bay_mean, bay_lo, bay_hi = bayesian_wr(wins, n)
    # max drawdown
    cum=peak=mdd=0.0
    for p in profits:
        cum+=p
        if cum>peak: peak=cum
        dd=peak-cum
        if dd>mdd: mdd=dd
    # longest losing streak
    ls=mls=0
    for p in profits:
        if p < 0: ls+=1; mls=max(mls,ls)
        else: ls=0
    return dict(
        n=n, wins=wins, wr=wr, roi=roi, lo=lo, hi=hi,
        clvpos=clvpos, avg_clv=avg_clv,
        p_edge_pos=p_pos,
        bay_mean=bay_mean, bay_lo=bay_lo, bay_hi=bay_hi,
        max_dd=mdd, max_ls=mls,
        profits=profits,
    )

def signal_status(s: dict) -> str:
    if s["n"] < 10: return "⚪ WAIT"
    if s["clvpos"] >= 0.60 and s["avg_clv"] > 0.02 and s["roi"] > 0 and s["p_edge_pos"] >= 0.70:
        return "🟢 GREEN"
    if s["clvpos"] < 0.50 or s["avg_clv"] < 0 or s["max_ls"] >= 5:
        return "🔴 RED"
    return "🟡 YELLOW"
