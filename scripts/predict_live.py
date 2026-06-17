#!/usr/bin/env python3
"""
Live Prediction + CLV Tracker + Virtual Bankroll

Режимы:
  --predict      Предикты + paper ставки по Rule C сигналам
  --clv          Расчёт CLV + settle ставок + обновление банка
  --bank         История банка и P&L
  --check ID     Детальный дамп event_id из BetsAPI

Rule C (FROZEN):
  elo_diff >= 75  AND  edge > 0  AND  odds < 2.0  AND  market_prob 60-70%

Kelly: четверть-Kelly от текущего баланса банка
FREEZE_DATE: 2026-06-16 (все матчи с этой даты — paper trading)

Usage:
  python3 scripts/predict_live.py --predict
  python3 scripts/predict_live.py --clv
  python3 scripts/predict_live.py --bank
  python3 scripts/predict_live.py --check 12042842
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
STARTING_BANK  = 1000.0      # начальный банк (виртуальные единицы)
KELLY_FRACTION = 0.25        # четверть-Kelly
MIN_STAKE      = 1.0         # минимальная ставка
PREFERRED_BM   = ["PinnacleSports", "Bet365", "GGBet", "MelBet", "YSB88"]
FREEZE_DATE    = "2026-06-16"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT UNIQUE,
    home_team   TEXT,
    away_team   TEXT,
    league      TEXT,
    start_time  INTEGER,
    elo_home    REAL,
    elo_away    REAL,
    elo_diff    REAL,
    model_prob  REAL,
    bookmaker   TEXT,
    open_odds_h REAL,
    open_odds_a REAL,
    mkt_prob    REAL,
    edge        REAL,
    rule_c      INTEGER DEFAULT 0,
    pick        TEXT,
    pick_odds   REAL,
    stake       REAL,
    close_odds  REAL,
    clv         REAL,
    result      TEXT,
    correct     INTEGER,
    profit      REAL,
    created_at  TEXT,
    settled_at  TEXT
);

CREATE TABLE IF NOT EXISTS bankroll (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    event_id   TEXT,
    action     TEXT,      -- 'init' | 'bet' | 'win' | 'loss' | 'push'
    amount     REAL,      -- + добавляет, - вычитает из баланса
    balance    REAL,      -- баланс ПОСЛЕ операции
    note       TEXT
);
"""


# ── Utilities ─────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def api_get(path, params={}, pause=2.0):
    time.sleep(pause)
    r = requests.get(f"{BASE}{path}", params={"token": TOKEN, **params}, timeout=15)
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

def best_match(name, candidates, threshold=0.6):
    best, score = None, 0.0
    for c in candidates:
        s = fuzzy(name, c)
        if s > score: best, score = c, s
    return best if score >= threshold else None

def kelly_stake(prob, odds, bank):
    """Четверть-Kelly от текущего банка."""
    if odds <= 1 or prob <= 0: return 0.0
    f = (prob * odds - 1) / (odds - 1)
    f = max(0.0, f) * KELLY_FRACTION
    return round(max(MIN_STAKE, f * bank), 2)


# ── DB helpers ────────────────────────────────────────────────────────────────

def open_db():
    PRED_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(PRED_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def get_balance(conn):
    r = conn.execute("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
    if r: return r["balance"]
    # Первый запуск — инициализируем банк
    conn.execute(
        "INSERT INTO bankroll(ts,action,amount,balance,note) VALUES(?,?,?,?,?)",
        (now_iso(), "init", STARTING_BANK, STARTING_BANK, f"Старт банка {STARTING_BANK}")
    )
    conn.commit()
    return STARTING_BANK


# ── Elo ───────────────────────────────────────────────────────────────────────

def build_elo():
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
        t1, t2, w = r["team_1_name"], r["team_2_name"], r["winner_name"]
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        ea = elo_exp(e1, e2)
        s1 = 1 if w == t1 else 0
        elo[t1] = e1 + K_FACTOR * (s1 - ea)
        elo[t2] = e2 + K_FACTOR * ((1-s1) - (1-ea))
        games[t1] = games.get(t1, 0) + 1
        games[t2] = games.get(t2, 0) + 1
    return elo, games


# ── PREDICT ───────────────────────────────────────────────────────────────────

def mode_predict():
    db = open_db()
    balance = get_balance(db)

    print(f"\n{'='*72}")
    print(f"  PREDICT  {now_iso()}   Банк: {balance:.2f}")
    print(f"{'='*72}\n")

    print("  Строим Elo...", end=" ", flush=True)
    elo, games = build_elo()
    known = list(elo.keys())
    print(f"{len(known)} команд\n")

    data = api_get("/v3/events/upcoming", {"sport_id": SPORT_ID})
    if not data: print("  BetsAPI: нет данных"); return

    events = [e for e in data.get("results", [])
              if "dota" in e.get("league", {}).get("name", "").lower()]
    print(f"  Upcoming Dota 2: {len(events)} матчей\n")

    rule_c_count = 0
    bets_placed  = 0

    for e in events:
        eid    = str(e.get("id"))
        home   = e.get("home", {}).get("name", "?")
        away   = e.get("away", {}).get("name", "?")
        league = e.get("league", {}).get("name", "")
        start  = int(e.get("time", 0))
        start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%d.%m %H:%M") if start else "?"

        # Elo
        hm = best_match(home, known)
        am = best_match(away, known)
        elo_h = elo.get(hm, START_ELO) if hm else START_ELO
        elo_a = elo.get(am, START_ELO) if am else START_ELO
        g_h   = games.get(hm, 0) if hm else 0
        g_a   = games.get(am, 0) if am else 0
        known_flag = hm is not None and am is not None
        prob_h = elo_exp(elo_h, elo_a)
        diff   = abs(elo_h - elo_a)

        # Live odds
        odds_data = api_get("/v2/event/odds/summary", {"event_id": eid})
        bm_name, oh, oa = None, None, None
        if odds_data:
            for pref in PREFERRED_BM:
                bd = odds_data.get("results", {}).get(pref)
                if not bd: continue
                od = bd.get("odds", {}) or {}
                end = od.get("end") or od.get("start") or {}
                m1 = end.get("151_1") or {}
                try:
                    oh = float(m1["home_od"]); oa = float(m1["away_od"])
                    bm_name = pref; break
                except: pass

        mkt_h, mkt_a = novig(oh, oa)
        edge = round(prob_h - mkt_h, 4) if mkt_h and known_flag else None

        # Rule C
        rule_c = bool(
            known_flag and edge is not None and edge > 0
            and diff >= 75 and oh is not None and oh < 2.0
            and mkt_h is not None and 0.60 <= mkt_h <= 0.70
        )

        # Kelly stake (только для Rule C)
        stake = kelly_stake(prob_h, oh, balance) if rule_c else 0.0

        # Pick
        pick, pick_odds = None, None
        if known_flag and edge is not None:
            if edge > 0: pick, pick_odds = "home", oh
            else:        pick, pick_odds = "away", oa

        # Сохраняем предикт
        db.execute("""
            INSERT OR REPLACE INTO predictions
              (event_id,home_team,away_team,league,start_time,
               elo_home,elo_away,elo_diff,model_prob,
               bookmaker,open_odds_h,open_odds_a,mkt_prob,edge,
               rule_c,pick,pick_odds,stake,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (eid, home, away, league, start,
              round(elo_h,1), round(elo_a,1), round(diff,1),
              round(prob_h,4), bm_name, oh, oa,
              round(mkt_h,4) if mkt_h else None, edge,
              int(rule_c), pick, pick_odds, stake, now_iso()))

        # Paper bet: списываем со счёта
        if rule_c and stake > 0:
            balance -= stake
            db.execute(
                "INSERT INTO bankroll(ts,event_id,action,amount,balance,note) VALUES(?,?,?,?,?,?)",
                (now_iso(), eid, "bet", -stake, balance,
                 f"Rule C: {home} vs {away} @ {oh} ×{stake:.2f}")
            )
            rule_c_count += 1
            bets_placed  += 1

        db.commit()

        # Вывод
        rc = "  ★ RULE C" if rule_c else ""
        edge_s = f"{edge:+.4f}" if edge is not None else "    ?"
        odds_s = f"{oh:.3f}/{oa:.3f} [{bm_name}]" if oh else "нет odds"
        stake_s = f"  → СТАВКА {stake:.2f} (банк → {balance:.2f})" if rule_c else ""

        print(f"  [{start_str}] {home} vs {away}{rc}")
        print(f"    Лига:  {league}")
        print(f"    Elo:   {elo_h:.0f} vs {elo_a:.0f}  diff={diff:.0f}  games={g_h}/{g_a}")
        print(f"    Model: {prob_h:.3f}  Market: {mkt_h:.3f if mkt_h else '?':>5}  Edge: {edge_s}")
        print(f"    Odds:  {odds_s}{stake_s}")
        if not known_flag:
            print(f"    ⚠  Не в Elo: {hm or '?'} / {am or '?'}")
        print()

    print(f"{'─'*72}")
    print(f"  ★ Rule C сигналов:  {rule_c_count}")
    print(f"  Бумажных ставок:    {bets_placed}")
    print(f"  Текущий банк:       {balance:.2f}  (из {STARTING_BANK:.0f})")
    print(f"\n  После матчей: python3 scripts/predict_live.py --clv")
    print(f"{'='*72}\n")
    db.close()


# ── CLV + SETTLE ──────────────────────────────────────────────────────────────

def mode_clv():
    db = open_db()
    balance = get_balance(db)

    print(f"\n{'='*72}")
    print(f"  CLV & SETTLE  {now_iso()}   Банк: {balance:.2f}")
    print(f"{'='*72}\n")

    preds = db.execute("""
        SELECT * FROM predictions
        WHERE settled_at IS NULL
        ORDER BY start_time ASC
    """).fetchall()

    if not preds:
        print("  Нет несettled предиктов.")
    else:
        now_ts = time.time()
        for p in preds:
            eid, home, away = p["event_id"], p["home_team"], p["away_team"]
            start_ts = p["start_time"] or 0

            if now_ts < start_ts:
                mins = (start_ts - now_ts) / 60
                print(f"  [{eid}] {home} vs {away} → старт через {mins:.0f} мин")
                continue

            print(f"  [{eid}] {home} vs {away} → проверяем...", flush=True)

            # Закрывающие odds
            odds_data = api_get("/v2/event/odds/summary", {"event_id": eid})
            close_h = None
            if odds_data:
                for pref in PREFERRED_BM:
                    bd = odds_data.get("results", {}).get(pref)
                    if not bd: continue
                    od = bd.get("odds", {}) or {}
                    end = od.get("end") or od.get("start") or {}
                    m1 = end.get("151_1") or {}
                    try:
                        close_h = float(m1["home_od"]); break
                    except: pass

            # Результат из истории odds
            result = None
            hist = api_get("/v2/event/odds", {"event_id": eid, "since_time": "0"})
            if hist:
                r = hist.get("results", {})
                if isinstance(r, dict):
                    score = r.get("score", "")
                    if score and score != "0-0":
                        try:
                            sh, sa = map(int, str(score).split("-"))
                            result = "home" if sh > sa else "away"
                        except: pass

            # CLV
            clv = None
            if p["mkt_prob"] and close_h:
                close_mkt, _ = novig(close_h, close_h)  # упрощённо, без away
                # Лучше: CLV = наша открывающая mkt_prob vs закрывающая
                # Используем: close_h напрямую
                try:
                    open_mkt  = p["mkt_prob"]          # наша open market prob
                    # грубое закрытие без away — просто raw
                    clv = round(open_mkt - (1 / close_h if close_h else open_mkt), 4)
                except: pass

            # P&L
            correct, profit = None, None
            if result and p["pick"] and p["stake"]:
                correct = int(result == p["pick"])
                stake   = p["stake"]
                if correct:
                    profit  = round(stake * (p["pick_odds"] - 1), 2)
                    balance += stake + profit   # возврат ставки + выигрыш
                    action   = "win"
                    amount   = stake + profit
                    note     = f"WIN {home} vs {away}"
                else:
                    profit  = -stake
                    action  = "loss"
                    amount  = 0.0              # ставка уже была списана при bet
                    note    = f"LOSS {home} vs {away}"
                db.execute(
                    "INSERT INTO bankroll(ts,event_id,action,amount,balance,note) VALUES(?,?,?,?,?,?)",
                    (now_iso(), eid, action, amount, balance, note)
                )

            db.execute("""
                UPDATE predictions
                SET close_odds=?, clv=?, result=?, correct=?, profit=?, settled_at=?
                WHERE event_id=?
            """, (close_h, clv, result, correct, profit, now_iso(), eid))
            db.commit()

            # Вывод строки
            r_sym  = {"home": "H✓" if result=="home" and p["pick"]=="home" else "H",
                      "away": "A✓" if result=="away" and p["pick"]=="away" else "A",
                      None:   "?"}.get(result, "?")
            c_sym  = "✓" if correct == 1 else ("✗" if correct == 0 else "—")
            clv_s  = f"{clv:+.4f}" if clv else "   ?"
            prof_s = f"{profit:+.2f}" if profit is not None else "—"
            print(f"    result={result or '?'}  {c_sym}  CLV={clv_s}  profit={prof_s}  банк→{balance:.2f}")

    # ── Итоговая статистика ──────────────────────────────────────────────────
    all_preds  = db.execute("SELECT * FROM predictions WHERE settled_at IS NOT NULL").fetchall()
    rc_preds   = [p for p in all_preds if p["rule_c"] == 1]
    all_clvs   = [p["clv"]    for p in all_preds if p["clv"]    is not None]
    rc_profits = [p["profit"] for p in rc_preds  if p["profit"] is not None]

    print(f"\n{'═'*72}")
    print(f"  ИТОГИ")
    print(f"{'─'*72}")

    if all_preds:
        wins = sum(1 for p in all_preds if p["correct"] == 1)
        n    = len(all_preds)
        print(f"  Всего предиктов:    {n}  |  Win rate: {wins}/{n} ({wins/n*100:.1f}%)")

    if rc_preds:
        rc_wins = sum(1 for p in rc_preds if p["correct"] == 1)
        n_rc    = len(rc_preds)
        total_profit = sum(rc_profits) if rc_profits else 0
        print(f"  Rule C ставки:      {n_rc}  |  Win rate: {rc_wins}/{n_rc} ({rc_wins/n_rc*100:.1f}%)")
        print(f"  Суммарный P&L:      {total_profit:+.2f}")
        print(f"  Текущий банк:       {balance:.2f}  (старт {STARTING_BANK:.0f}  |  "
              f"ROI {(balance-STARTING_BANK)/STARTING_BANK*100:+.1f}%)")

    if all_clvs:
        avg_clv = sum(all_clvs) / len(all_clvs)
        pos_clv = sum(1 for c in all_clvs if c > 0)
        print(f"  Avg CLV:            {avg_clv:+.4f}  |  Positive: {pos_clv}/{len(all_clvs)}")

    print(f"{'═'*72}\n")
    db.close()


# ── BANK ─────────────────────────────────────────────────────────────────────

def mode_bank():
    db = open_db()
    rows = db.execute(
        "SELECT * FROM bankroll ORDER BY id ASC"
    ).fetchall()

    print(f"\n{'='*72}")
    print(f"  VIRTUAL BANKROLL HISTORY")
    print(f"{'='*72}")
    print(f"  {'#':>3}  {'Дата':>16}  {'Действие':>6}  {'Сумма':>8}  {'Баланс':>8}  Примечание")
    print(f"  {'─'*66}")

    for r in rows:
        sign = f"{r['amount']:+.2f}" if r["amount"] else "    —"
        print(f"  {r['id']:>3}  {r['ts'][5:16]:>16}  {r['action']:>6}  "
              f"{sign:>8}  {r['balance']:>8.2f}  {r['note'] or ''}")

    if rows:
        start_bal = rows[0]["balance"] if rows[0]["action"] == "init" else STARTING_BANK
        end_bal   = rows[-1]["balance"]
        roi       = (end_bal - start_bal) / start_bal * 100
        bets_n    = sum(1 for r in rows if r["action"] == "bet")
        wins_n    = sum(1 for r in rows if r["action"] == "win")

        print(f"  {'─'*66}")
        print(f"  Старт: {start_bal:.2f}  →  Текущий: {end_bal:.2f}  |  "
              f"ROI: {roi:+.1f}%  |  Ставок: {bets_n}  Выигрышей: {wins_n}")

    print(f"{'='*72}\n")
    db.close()


# ── CHECK ─────────────────────────────────────────────────────────────────────

def mode_check(eid):
    print(f"\n  Дамп event_id={eid}\n")
    d = api_get("/v2/event/odds/summary", {"event_id": eid})
    if d:
        for bm, bd in (d.get("results") or {}).items():
            od = (bd.get("odds") or {})
            end = od.get("end") or od.get("start") or {}
            m1 = end.get("151_1") or {}
            if m1:
                print(f"  {bm:20} home={m1.get('home_od')}  away={m1.get('away_od')}")
    hist = api_get("/v2/event/odds", {"event_id": eid, "since_time": "0"})
    if hist:
        r = hist.get("results", {})
        if isinstance(r, dict):
            print(f"\n  Score: {r.get('score','n/a')}  Timer: {r.get('timer','n/a')}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--predict", action="store_true", help="Предикты + paper ставки")
    g.add_argument("--clv",     action="store_true", help="Settle + CLV + P&L")
    g.add_argument("--bank",    action="store_true", help="История банка")
    g.add_argument("--check",   metavar="EVENT_ID",  help="Дамп матча")
    args = ap.parse_args()

    if not TOKEN: print("ERROR: BETSAPI_TOKEN не задан"); sys.exit(1)

    if   args.predict: mode_predict()
    elif args.clv:     mode_clv()
    elif args.bank:    mode_bank()
    elif args.check:   mode_check(args.check)

if __name__ == "__main__":
    main()
