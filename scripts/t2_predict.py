#!/usr/bin/env python3
"""
Terminal 2 — Prediction Watcher
Следит за upcoming матчами, делает предикты, ставит paper bets, settle после матча.
Работает в цикле. Ctrl+C для остановки.

Usage:
    python3 scripts/t2_predict.py
    python3 scripts/t2_predict.py --interval 600   # опрос каждые 10 мин (default)
"""
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from math import pow
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

TOKEN    = os.getenv("BETSAPI_TOKEN", "")
BASE     = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")
ELO_DB   = ROOT / "storage" / "dota_research.sqlite3"
PRED_DB  = ROOT / "storage" / "predictions.db"
SPORT_ID = 151

START_ELO      = 1500.0
K_FACTOR       = 32
STARTING_BANK  = 1000.0
KELLY_FRACTION = 0.25
MIN_STAKE      = 1.0
PREFERRED_BM   = ["PinnacleSports", "Bet365", "GGBet", "MelBet", "YSB88"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT UNIQUE,
    home_team   TEXT,
    away_team   TEXT,
    league      TEXT,
    start_time  INTEGER,
    elo_home    REAL, elo_away REAL, elo_diff REAL,
    model_prob  REAL,
    bookmaker   TEXT,
    open_odds_h REAL, open_odds_a REAL,
    mkt_prob    REAL, edge REAL,
    rule_c      INTEGER DEFAULT 0,
    pick        TEXT, pick_odds REAL, stake REAL,
    close_odds  REAL, clv REAL,
    result      TEXT, correct INTEGER, profit REAL,
    created_at  TEXT, settled_at TEXT
);
CREATE TABLE IF NOT EXISTS bankroll (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT, event_id TEXT,
    action   TEXT, amount REAL, balance REAL, note TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ts_str():
    return datetime.now().strftime("%H:%M:%S")

def api_get(path, params={}, pause=2.1):
    time.sleep(pause)
    r = requests.get(f"{BASE}{path}", params={"token": TOKEN, **params}, timeout=15)
    if r.status_code == 429:
        print(f"  [429] Rate limit, ждём 60s...", flush=True)
        time.sleep(60)
        return None
    r.raise_for_status()
    d = r.json()
    return d if d.get("success") else None

def novig(h, a):
    if not h or not a: return None, None
    t = 1/h + 1/a
    return (1/h)/t, (1/a)/t

def elo_exp(ra, rb):
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))

def fuzzy(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def best_match(name, candidates, thr=0.6):
    best, sc = None, 0.0
    for c in candidates:
        s = fuzzy(name, c)
        if s > sc: best, sc = c, s
    return best if sc >= thr else None

def kelly_stake(prob, odds, bank):
    if odds <= 1 or prob <= 0: return 0.0
    f = (prob * odds - 1) / (odds - 1)
    return round(max(MIN_STAKE, max(0.0, f) * KELLY_FRACTION * bank), 2)

def open_db():
    PRED_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(PRED_DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.commit()
    return c

def get_balance(db):
    r = db.execute("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
    if r: return r["balance"]
    db.execute("INSERT INTO bankroll(ts,action,amount,balance,note) VALUES(?,?,?,?,?)",
               (now_iso(), "init", STARTING_BANK, STARTING_BANK, f"Старт {STARTING_BANK}"))
    db.commit()
    return STARTING_BANK

def build_elo():
    conn = sqlite3.connect(ELO_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT team_1_name, team_2_name, winner_name FROM matches
        WHERE status='finished' AND team_1_name IS NOT NULL
          AND team_2_name IS NOT NULL AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()
    conn.close()
    elo, games = {}, {}
    for r in rows:
        t1, t2, w = r[0], r[1], r[2]
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        ea = elo_exp(e1, e2); s1 = 1 if w == t1 else 0
        elo[t1] = e1 + K_FACTOR*(s1-ea); elo[t2] = e2 + K_FACTOR*((1-s1)-(1-ea))
        games[t1] = games.get(t1,0)+1;   games[t2] = games.get(t2,0)+1
    return elo, games


def scan_upcoming(db, elo, known, balance):
    """Проверяет upcoming матчи. Возвращает (новые сигналы, новый баланс)."""
    data = api_get("/v3/events/upcoming", {"sport_id": SPORT_ID})
    if not data: return 0, balance

    events = [e for e in data.get("results", [])
              if "dota" in e.get("league", {}).get("name", "").lower()]

    new_signals = 0
    existing = {r[0] for r in db.execute("SELECT event_id FROM predictions").fetchall()}

    for e in events:
        eid  = str(e.get("id"))
        if eid in existing: continue   # уже обработан

        home   = e.get("home", {}).get("name", "?")
        away   = e.get("away", {}).get("name", "?")
        league = e.get("league", {}).get("name", "")
        start  = int(e.get("time", 0))
        s_str  = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%d.%m %H:%M") if start else "?"

        hm = best_match(home, known); am = best_match(away, known)
        elo_h = elo.get(hm, START_ELO) if hm else START_ELO
        elo_a = elo.get(am, START_ELO) if am else START_ELO
        prob_h = elo_exp(elo_h, elo_a)
        diff   = abs(elo_h - elo_a)
        known_flag = hm is not None and am is not None

        odds_data = api_get("/v2/event/odds/summary", {"event_id": eid})
        bm_name, oh, oa = None, None, None
        if odds_data:
            for pref in PREFERRED_BM:
                bd = (odds_data.get("results") or {}).get(pref)
                if not bd: continue
                od = (bd.get("odds") or {})
                end = od.get("end") or od.get("start") or {}
                m1 = end.get("151_1") or {}
                try: oh=float(m1["home_od"]); oa=float(m1["away_od"]); bm_name=pref; break
                except: pass

        mkt_h, _ = novig(oh, oa)
        edge = round(prob_h - mkt_h, 4) if mkt_h and known_flag else None

        rule_c = bool(
            known_flag and edge is not None and edge > 0
            and diff >= 75 and oh is not None and oh < 2.0
            and mkt_h is not None and 0.60 <= mkt_h <= 0.70
        )
        stake = kelly_stake(prob_h, oh, balance) if rule_c else 0.0
        pick  = ("home" if (edge or 0) > 0 else "away") if known_flag and edge is not None else None
        pick_odds = (oh if pick=="home" else oa) if pick else None

        db.execute("""
            INSERT OR IGNORE INTO predictions
              (event_id,home_team,away_team,league,start_time,
               elo_home,elo_away,elo_diff,model_prob,
               bookmaker,open_odds_h,open_odds_a,mkt_prob,edge,
               rule_c,pick,pick_odds,stake,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (eid,home,away,league,start,
              round(elo_h,1),round(elo_a,1),round(diff,1),round(prob_h,4),
              bm_name,oh,oa,round(mkt_h,4) if mkt_h else None,edge,
              int(rule_c),pick,pick_odds,stake,now_iso()))

        if rule_c and stake > 0:
            balance -= stake
            db.execute(
                "INSERT INTO bankroll(ts,event_id,action,amount,balance,note) VALUES(?,?,?,?,?,?)",
                (now_iso(),eid,"bet",-stake,balance,f"Rule C: {home} vs {away} @ {oh}"))
            new_signals += 1
            print(f"\n  ★ RULE C  [{s_str}] {home} vs {away}", flush=True)
            print(f"    Edge={edge:+.4f}  Elo_diff={diff:.0f}  Odds={oh}  [{bm_name}]")
            print(f"    Ставка: {stake:.2f}  →  банк: {balance:.2f}\n", flush=True)
        else:
            rc_miss = []
            if not known_flag:          rc_miss.append("нет в Elo")
            elif edge is None:          rc_miss.append("нет odds")
            elif edge <= 0:             rc_miss.append(f"edge={edge:+.3f}")
            elif diff < 75:             rc_miss.append(f"elo_diff={diff:.0f}<75")
            elif oh and oh >= 2.0:      rc_miss.append(f"odds={oh}≥2.0")
            elif mkt_h and not (0.60<=mkt_h<=0.70): rc_miss.append(f"mkt={mkt_h:.3f} вне 60-70%")
            miss_str = ", ".join(rc_miss) if rc_miss else "ok"
            print(f"  [{s_str}] {home} vs {away}  edge={edge or '?'}  [{miss_str}]", flush=True)

        db.commit()

    return new_signals, balance


def settle_past(db, balance):
    """Settle матчи которые уже завершились."""
    now_ts = time.time()
    pending = db.execute("""
        SELECT * FROM predictions
        WHERE settled_at IS NULL AND start_time < ?
    """, (now_ts - 3600,)).fetchall()   # закончились >1ч назад

    for p in pending:
        eid = p["event_id"]
        hist = api_get("/v2/event/odds", {"event_id": eid, "since_time": "0"})
        result = None
        if hist:
            r = hist.get("results", {})
            if isinstance(r, dict):
                score = r.get("score", "")
                if score and score != "0-0":
                    try:
                        sh, sa = map(int, str(score).split("-"))
                        result = "home" if sh > sa else "away"
                    except: pass

        if result is None: continue   # матч ещё не завершён

        # Closing odds
        od = api_get("/v2/event/odds/summary", {"event_id": eid})
        close_h = None
        if od:
            for pref in PREFERRED_BM:
                bd = (od.get("results") or {}).get(pref)
                if not bd: continue
                end = ((bd.get("odds") or {}).get("end") or (bd.get("odds") or {}).get("start") or {})
                m1 = end.get("151_1") or {}
                try: close_h=float(m1["home_od"]); break
                except: pass

        clv = None
        if p["mkt_prob"] and close_h:
            try: clv = round(p["mkt_prob"] - 1/close_h, 4)
            except: pass

        correct, profit, action, amount = None, None, None, 0.0
        if p["pick"] and p["stake"]:
            correct = int(result == p["pick"])
            stake   = p["stake"]
            if correct:
                profit = round(stake * (p["pick_odds"] - 1), 2)
                balance += stake + profit
                action, amount = "win", stake + profit
            else:
                profit  = -stake
                action, amount = "loss", 0.0
            db.execute(
                "INSERT INTO bankroll(ts,event_id,action,amount,balance,note) VALUES(?,?,?,?,?,?)",
                (now_iso(),eid,action,amount,balance,
                 f"{'WIN' if correct else 'LOSS'}: {p['home_team']} vs {p['away_team']}"))

        db.execute("""
            UPDATE predictions
            SET close_odds=?,clv=?,result=?,correct=?,profit=?,settled_at=?
            WHERE event_id=?
        """, (close_h,clv,result,correct,profit,now_iso(),eid))
        db.commit()

        sym = "✓" if correct == 1 else ("✗" if correct == 0 else "—")
        prof_s = f"{profit:+.2f}" if profit is not None else "—"
        print(f"  [{ts_str()}] SETTLE {p['home_team']} vs {p['away_team']} "
              f"→ {result} {sym}  P&L={prof_s}  банк→{balance:.2f}", flush=True)

    return balance


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=600,
                    help="Секунд между опросами (default: 600 = 10 мин)")
    args = ap.parse_args()

    if not TOKEN: print("ERROR: BETSAPI_TOKEN не задан"); sys.exit(1)

    db = open_db()
    print(f"\n{'='*60}")
    print(f"  Terminal 2 — Prediction Watcher")
    print(f"  Интервал: {args.interval}s  |  Rule C: elo_diff≥75, odds<2.0, mkt 60-70%")
    print(f"  Предикты → {PRED_DB.name}")
    print(f"{'='*60}\n")

    print("  Строим Elo...", end=" ", flush=True)
    elo, games = build_elo()
    known = list(elo.keys())
    print(f"{len(known)} команд. Готово.\n")

    balance = get_balance(db)
    print(f"  Текущий банк: {balance:.2f}\n")

    cycle = 0
    while True:
        cycle += 1
        print(f"  [{ts_str()}] Цикл #{cycle} — сканируем upcoming...", flush=True)

        try:
            new_sigs, balance = scan_upcoming(db, elo, known, balance)
            balance = settle_past(db, balance)

            settled = db.execute("SELECT COUNT(*) FROM predictions WHERE settled_at IS NOT NULL").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM predictions WHERE settled_at IS NULL").fetchone()[0]
            print(f"  [{ts_str()}] Готово. Новых сигналов: {new_sigs} | "
                  f"Pending: {pending} | Settled: {settled} | Банк: {balance:.2f}", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as ex:
            print(f"  [ERR] {ex}", flush=True)

        print(f"  Следующий опрос через {args.interval//60} мин... (Ctrl+C)\n", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Остановлено.")
