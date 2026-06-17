#!/usr/bin/env python3
"""
Live daemon: каждый час собирает odds, логирует paper bets с edge > min_edge,
проставляет результаты завершённых матчей, обновляет дашборд.

Запуск:
    PYTHONPATH=. python3 scripts/daemon.py           # работает бесконечно
    PYTHONPATH=. python3 scripts/daemon.py --once    # один цикл и выход
    PYTHONPATH=. python3 scripts/daemon.py --dry-run # без записи в БД

Автозапуск (macOS launchd):
    python3 scripts/daemon.py --install   # создаёт ~/Library/LaunchAgents/dota.trader.plist
    launchctl load ~/Library/LaunchAgents/dota.trader.plist
"""
from __future__ import annotations

import sys, argparse, sqlite3, json, time, subprocess
from collections import defaultdict
from datetime import datetime, timezone
from math import pow
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

DB_PATH  = PROJECT_ROOT / settings.database_path
LOG_PATH = PROJECT_ROOT / "logs" / "daemon.log"
K        = 32
START_ELO = 1500.0
MIN_EDGE  = settings.min_edge          # default 0.06
MAX_STAKE = settings.max_stake_pct     # default 0.03
BANK      = settings.start_bank


# ── Elo ───────────────────────────────────────────────────────────────────────

def build_elo(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT team_1_name, team_2_name, winner_name
        FROM matches WHERE status='finished'
          AND team_1_name IS NOT NULL AND winner_name IS NOT NULL
        ORDER BY begin_at ASC
    """).fetchall()
    elo   = defaultdict(lambda: START_ELO)
    games = defaultdict(int)
    for r in rows:
        t1, t2, w = r[0], r[1], r[2]
        e1 = elo[t1]; e2 = elo[t2]
        ea = 1.0 / (1.0 + pow(10.0, (e2 - e1) / 400.0))
        s1 = 1 if w == t1 else 0
        elo[t1] = e1 + K * (s1 - ea)
        elo[t2] = e2 + K * ((1 - s1) - (1 - ea))
        games[t1] += 1; games[t2] += 1
    return dict(elo), dict(games)


def expected(ra, rb):
    return 1.0 / (1.0 + pow(10.0, (rb - ra) / 400.0))


# ── Fuzzy match ───────────────────────────────────────────────────────────────

import re as _re
def _norm(s: str) -> str:
    return _re.sub(r"[^a-z0-9]", "", s.lower())

def find_in_elo(elo: dict, name: str):
    n = _norm(name)
    for k in elo:
        if _norm(k) == n: return k
    for k in elo:
        nk = _norm(k)
        if n in nk or nk in n: return k
    return None


# ── No-vig ────────────────────────────────────────────────────────────────────

def novig(oh: float, oa: float):
    ih = 1/oh; ia = 1/oa; t = ih + ia
    return ih/t, ia/t


# ── Kelly ─────────────────────────────────────────────────────────────────────

def kelly_stake(prob: float, odds: float, bank: float,
                fraction: float = 0.25, max_pct: float = MAX_STAKE) -> float:
    edge = prob * odds - 1.0
    if edge <= 0:
        return 0.0
    k = (edge / (odds - 1.0)) * fraction
    k = min(k, max_pct)
    return round(k * bank, 2)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── Settle finished bets ──────────────────────────────────────────────────────

def settle_bets(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """
    Проставляем результат по paper bets где матч уже завершён.
    """
    pending = conn.execute("""
        SELECT b.id, b.match_external_id, b.selection, b.odds, b.stake
        FROM bets b
        WHERE b.status = 'pending'
          AND b.match_external_id IS NOT NULL
    """).fetchall()

    settled = 0
    for bet in pending:
        match = conn.execute(
            "SELECT winner_name, team_1_name, team_2_name, status FROM matches "
            "WHERE external_id = ?", (bet[1],)
        ).fetchone()

        if not match or match[3] != "finished":
            continue

        winner   = match[0]
        selection = bet[2]  # имя команды на которую ставили

        won = (winner == selection) or (_norm(winner) == _norm(selection))
        profit = round((bet[4] * bet[3] - bet[4]), 2) if won else round(-bet[4], 2)

        if not dry_run:
            conn.execute("""
                UPDATE bets SET status='settled', result=?, profit=?
                WHERE id=?
            """, ("win" if won else "loss", profit, bet[0]))

        settled += 1
        log(f"  Settled bet #{bet[0]}: {'WIN' if won else 'LOSS'} profit={profit:+.2f}")

    if not dry_run and settled:
        conn.commit()
    return settled


# ── Collect odds + place paper bets ──────────────────────────────────────────

def run_cycle(dry_run: bool = False) -> dict:
    log("=" * 50)
    log("Daemon cycle start")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Settle old bets
    settled = settle_bets(conn, dry_run)
    log(f"  Settled: {settled} bets")

    # 2. Build Elo
    log("  Building Elo...")
    elo, games = build_elo(conn)

    # 3. Fetch upcoming odds
    try:
        from adapters.betsapi import BetsAPIClient, _extract_moneyline
        client  = BetsAPIClient()
        events  = client.get_upcoming_dota2()
        log(f"  Upcoming Dota 2 events: {len(events)}")
    except Exception as e:
        log(f"  BetsAPI error: {e}")
        conn.close()
        return {"error": str(e)}

    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    placed    = 0
    skipped   = 0
    no_model  = 0

    # Уже поставленные матчи (не дублировать)
    existing = {r[0] for r in conn.execute(
        "SELECT match_external_id FROM bets WHERE status='pending'"
    ).fetchall()}

    for event in events:
        home   = event.get("home", {}).get("name", "")
        away   = event.get("away", {}).get("name", "")
        eid    = str(event.get("id", ""))
        league = event.get("league", {}).get("name", "")
        ts     = event.get("time", "")

        # Конвертируем betsapi event_id → pandascore external_id если есть
        ps_match = conn.execute("""
            SELECT external_id, team_1_name, team_2_name FROM matches
            WHERE status='not_started'
              AND team_1_name IS NOT NULL
            ORDER BY begin_at ASC LIMIT 200
        """).fetchall()

        matched_ext_id = None
        matched_name   = f"{home} vs {away}"
        for m in ps_match:
            d1 = _norm(m[1] or ""); d2 = _norm(m[2] or "")
            h  = _norm(home);       a  = _norm(away)
            if (d1 == h and d2 == a) or (d1 == a and d2 == h):
                matched_ext_id = m[0]
                break

        if matched_ext_id and matched_ext_id in existing:
            skipped += 1
            continue

        # Model prob
        t1k = find_in_elo(elo, home)
        t2k = find_in_elo(elo, away)
        if not t1k or not t2k:
            no_model += 1
            continue
        if min(games.get(t1k, 0), games.get(t2k, 0)) < 10:
            no_model += 1
            continue

        prob_home = expected(elo[t1k], elo[t2k])

        # Odds
        try:
            summary = client.get_odds_summary(eid)
            bms     = _extract_moneyline(summary)
        except Exception:
            continue
        if not bms:
            continue

        chosen = next((b for b in bms if b["bookmaker"] == "Bet365"), bms[0])
        oh = chosen["close_home"]
        oa = chosen["close_away"]
        mkt_h, mkt_a = novig(oh, oa)

        # Edge для обеих сторон
        edge_h = prob_home - mkt_h
        edge_a = (1 - prob_home) - mkt_a

        best_edge = edge_h
        best_team = home
        best_odds = oh
        best_prob = prob_home
        if edge_a > edge_h:
            best_edge = edge_a
            best_team = away
            best_odds = oa
            best_prob = 1 - prob_home

        if best_edge < MIN_EDGE:
            skipped += 1
            continue

        stake = kelly_stake(best_prob, best_odds, BANK)
        if stake <= 0:
            continue

        log(f"  VALUE BET: {home} vs {away} | pick={best_team} "
            f"prob={best_prob:.3f} odds={best_odds:.2f} "
            f"edge={best_edge:+.3f} stake=${stake:.2f}")

        if not dry_run:
            conn.execute("""
                INSERT OR IGNORE INTO bets
                  (match_name, market, selection, odds,
                   model_probability, book_probability, edge,
                   stake, status, source, notes, match_external_id)
                VALUES (?,?,?,?,?,?,?,?,'pending','daemon',?,?)
            """, (
                matched_name, "h2h", best_team, best_odds,
                round(best_prob, 4), round(mkt_h if best_team == home else mkt_a, 4),
                round(best_edge, 4), stake,
                f"league={league} bm={chosen['bookmaker']}",
                matched_ext_id or eid,
            ))
            conn.commit()

        placed += 1

    conn.close()

    # 4. Обновляем дашборд
    try:
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "dashboard.py"), "--no-live"],
            cwd=str(PROJECT_ROOT),
            env={**__import__("os").environ, "PYTHONPATH": str(PROJECT_ROOT)},
            timeout=30,
        )
        log("  Dashboard updated")
    except Exception as e:
        log(f"  Dashboard error: {e}")

    log(f"  Cycle done: placed={placed} skipped={skipped} no_model={no_model}")
    return {"placed": placed, "skipped": skipped, "settled": settled}


# ── launchd installer ─────────────────────────────────────────────────────────

def install_launchd():
    plist_path = Path.home() / "Library" / "LaunchAgents" / "dota.trader.plist"
    python     = sys.executable
    script     = str(PROJECT_ROOT / "scripts" / "daemon.py")
    log_out    = str(PROJECT_ROOT / "logs" / "daemon_stdout.log")
    log_err    = str(PROJECT_ROOT / "logs" / "daemon_stderr.log")

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>         <string>dota.trader</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>--once</string>
  </array>
  <key>WorkingDirectory</key> <string>{PROJECT_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key> <string>{PROJECT_ROOT}</string>
  </dict>
  <key>StartInterval</key> <integer>3600</integer>
  <key>StandardOutPath</key> <string>{log_out}</string>
  <key>StandardErrorPath</key> <string>{log_err}</string>
  <key>RunAtLoad</key> <true/>
</dict>
</plist>"""

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"✓ plist: {plist_path}")
    print(f"\nАктивировать:")
    print(f"  launchctl load {plist_path}")
    print(f"\nОстановить:")
    print(f"  launchctl unload {plist_path}")
    print(f"\nЛоги:")
    print(f"  tail -f {PROJECT_ROOT}/logs/daemon.log")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",    action="store_true", help="Один цикл и выход")
    parser.add_argument("--dry-run", action="store_true", help="Без записи в БД")
    parser.add_argument("--install", action="store_true", help="Установить launchd plist")
    parser.add_argument("--interval", type=int, default=3600,
                        help="Интервал между циклами в секундах (default: 3600)")
    args = parser.parse_args()

    if args.install:
        install_launchd()
        return

    log(f"Daemon starting | once={args.once} dry_run={args.dry_run} "
        f"min_edge={MIN_EDGE} max_stake={MAX_STAKE*100:.0f}%")

    while True:
        try:
            run_cycle(dry_run=args.dry_run)
        except KeyboardInterrupt:
            log("Daemon stopped by user")
            break
        except Exception as e:
            log(f"Cycle ERROR: {e}")

        if args.once:
            break

        log(f"Следующий цикл через {args.interval}s...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
