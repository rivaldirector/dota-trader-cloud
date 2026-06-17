#!/usr/bin/env python3
"""
matched_betting.py — Matched betting без биржи (back-back метод).

Схема работы:
  1. Букмекер даёт фрибет на сумму F
  2. Ставишь фрибет на команду A у BM1 (кэф odds_A)
  3. Ставишь реальные деньги на команду B у BM2 (кэф odds_B)
  4. Получаешь гарантированную прибыль ~40-67% от суммы фрибета

Режимы:
  --calc     Калькулятор: посчитать стейки для конкретного фрибета
  --add      Добавить новый бонус в трекер
  --list     Показать все бонусы и статус
  --use      Пометить бонус как использованный
  --report   Итоговый P&L по всем бонусам

Запуск:
  PYTHONPATH=. python3 scripts/matched_betting.py --calc --freebet 100 --odds-bm1 2.10 --odds-bm2 2.05
  PYTHONPATH=. python3 scripts/matched_betting.py --add --bm "GGBet" --bonus 150 --wager 1
  PYTHONPATH=. python3 scripts/matched_betting.py --list
  PYTHONPATH=. python3 scripts/matched_betting.py --report
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
MB_DB = ROOT / "data" / "matched_betting.db"


# ── Список букмекеров с типичными бонусами ────────────────────────────────────
# Обнови под себя — это стартовый список для СНГ-рынка
KNOWN_BOOKMAKERS = [
    {"bm": "GGBet",     "bonus_type": "freebet", "typical_amount": 100,  "wager_req": 1,  "url": "ggbet.com"},
    {"bm": "MelBet",    "bonus_type": "freebet", "typical_amount": 100,  "wager_req": 1,  "url": "melbet.com"},
    {"bm": "FonBet",    "bonus_type": "freebet", "typical_amount": 500,  "wager_req": 1,  "url": "fonbet.ru"},
    {"bm": "1xBet",     "bonus_type": "deposit", "typical_amount": 130,  "wager_req": 5,  "url": "1xbet.com"},
    {"bm": "Bet365",    "bonus_type": "freebet", "typical_amount": 30,   "wager_req": 1,  "url": "bet365.com"},
    {"bm": "BetWinner", "bonus_type": "deposit", "typical_amount": 100,  "wager_req": 3,  "url": "betwinner.com"},
    {"bm": "PariMatch", "bonus_type": "freebet", "typical_amount": 100,  "wager_req": 1,  "url": "parimatch.com"},
    {"bm": "Mostbet",   "bonus_type": "freebet", "typical_amount": 100,  "wager_req": 1,  "url": "mostbet.com"},
    {"bm": "888Sport",  "bonus_type": "freebet", "typical_amount": 30,   "wager_req": 1,  "url": "888sport.com"},
    {"bm": "10Bet",     "bonus_type": "freebet", "typical_amount": 50,   "wager_req": 1,  "url": "10bet.com"},
    {"bm": "22Bet",     "bonus_type": "freebet", "typical_amount": 50,   "wager_req": 1,  "url": "22bet.com"},
    {"bm": "CashPoint", "bonus_type": "freebet", "typical_amount": 30,   "wager_req": 1,  "url": "cashpoint.com"},
]


# ── DB ────────────────────────────────────────────────────────────────────────

def _init_mb_schema(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS bonuses (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        added_at     TEXT NOT NULL,
        bookmaker    TEXT NOT NULL,
        bonus_type   TEXT NOT NULL DEFAULT 'freebet',
        amount       REAL NOT NULL,
        wager_req    REAL NOT NULL DEFAULT 1,
        expiry       TEXT,
        status       TEXT NOT NULL DEFAULT 'AVAILABLE',
        used_at      TEXT,
        odds_bm1     REAL,
        odds_bm2     REAL,
        hedge_stake  REAL,
        extracted    REAL,
        extraction_pct REAL,
        notes        TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS mb_trades (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        bonus_id     INTEGER REFERENCES bonuses(id),
        created_at   TEXT NOT NULL,
        event        TEXT,
        pick_bm1     TEXT,
        pick_bm2     TEXT,
        odds_bm1     REAL,
        odds_bm2     REAL,
        freebet_amt  REAL,
        hedge_stake  REAL,
        guaranteed   REAL,
        extraction_pct REAL,
        status       TEXT DEFAULT 'PENDING',
        actual_profit REAL
    )
    """)
    conn.commit()


_MB_TMP = Path("/tmp/mb_tracker_working.db")


def get_mb_conn() -> sqlite3.Connection:
    """
    FUSE-safe: работаем в /tmp, синхронизируем с реальным путём.
    Reads OK from FUSE, writes FAIL — поэтому держим рабочую копию в /tmp.
    """
    import shutil
    MB_DB.parent.mkdir(parents=True, exist_ok=True)
    # Sync FUSE → /tmp if FUSE is newer or /tmp doesn't exist
    if MB_DB.exists() and MB_DB.stat().st_size > 0:
        if not _MB_TMP.exists() or MB_DB.stat().st_mtime > _MB_TMP.stat().st_mtime:
            try:
                shutil.copy2(str(MB_DB), str(_MB_TMP))
            except Exception:
                pass
    conn = sqlite3.connect(str(_MB_TMP))
    conn.row_factory = sqlite3.Row
    _init_mb_schema(conn)
    return conn


def save_mb(conn: sqlite3.Connection):
    """После записи — синхронизировать /tmp → FUSE."""
    import shutil
    try:
        conn.commit()
        shutil.copy2(str(_MB_TMP), str(MB_DB))
    except Exception:
        pass


# ── MATH ──────────────────────────────────────────────────────────────────────

def calc_freebet(freebet: float, odds_bm1: float, odds_bm2: float) -> dict:
    """
    Фрибет (profit-only): если выиграли у BM1 — получаем только прибыль, стейк не возвращается.

    Хедж на BM2:
      S_hedge = freebet * (odds_bm1 - 1) / odds_bm2
      Profit  = freebet * (odds_bm1 - 1) * (odds_bm2 - 1) / odds_bm2
    """
    if odds_bm1 <= 1 or odds_bm2 <= 1:
        return {}
    hedge = freebet * (odds_bm1 - 1) / odds_bm2
    profit = freebet * (odds_bm1 - 1) * (odds_bm2 - 1) / odds_bm2
    extraction_pct = profit / freebet * 100

    return dict(
        freebet=freebet,
        odds_bm1=odds_bm1,
        odds_bm2=odds_bm2,
        hedge=round(hedge, 2),
        profit=round(profit, 2),
        extraction_pct=round(extraction_pct, 1),
        # Что получаем при каждом исходе
        if_bm1_wins=round(freebet * (odds_bm1 - 1) - hedge, 2),
        if_bm2_wins=round(hedge * (odds_bm2 - 1), 2),
    )


def calc_deposit_bonus(deposit: float, bonus_pct: float, odds: float, wager: int = 5) -> dict:
    """
    Депозитный бонус: нужно прокрутить bonus * wager_req.
    Стратегия: ставить на одну сторону у BM, хеджить у другого BM.
    Каждая прокрутка теряет ~маржу = 1 - 1/odds_a - 1/odds_b ≈ 3-5%.
    """
    bonus = deposit * bonus_pct / 100
    required_turnover = bonus * wager
    # Потеря на каждой прокрутке (оверраунд ~5%)
    loss_per_turn = required_turnover * 0.05
    net = bonus - loss_per_turn
    return dict(
        deposit=deposit, bonus=bonus,
        turnover_needed=required_turnover,
        estimated_loss=round(loss_per_turn, 2),
        net_expected=round(net, 2),
    )


# ── CMD: CALC ──────────────────────────────────────────────────────────────────

def cmd_calc(freebet: float, odds_bm1: float, odds_bm2: float):
    r = calc_freebet(freebet, odds_bm1, odds_bm2)
    if not r:
        print("Ошибка: кэфы должны быть > 1.0")
        return

    print(f"\n{'='*55}")
    print(f"MATCHED BETTING КАЛЬКУЛЯТОР (back-back, без биржи)")
    print(f"{'='*55}")
    print(f"\n  Фрибет:          {freebet:.2f}$  (profit-only)")
    print(f"  Кэф BM1 (пик A): {odds_bm1:.3f}")
    print(f"  Кэф BM2 (пик B): {odds_bm2:.3f}")
    print()
    print(f"  ┌─ ЧТО ДЕЛАЕМ ──────────────────────────────────┐")
    print(f"  │  1. Ставим фрибет {freebet:.0f}$ на команду A у BM1 @ {odds_bm1:.2f}")
    print(f"  │  2. Ставим {r['hedge']:.2f}$ реальных на команду B у BM2 @ {odds_bm2:.2f}")
    print(f"  └───────────────────────────────────────────────┘")
    print()
    print(f"  Если A выигрывает: +{r['if_bm1_wins']:.2f}$")
    print(f"  Если B выигрывает: +{r['if_bm2_wins']:.2f}$")
    print()
    print(f"  ✅ Гарантированная прибыль: +{r['profit']:.2f}$")
    print(f"  📊 % извлечения фрибета:   {r['extraction_pct']:.1f}%")
    print(f"  💰 Нужно заморозить:        {r['hedge']:.2f}$ до результата")
    print()

    # Показываем как меняется при разных кэфах
    print(f"  Для сравнения при других кэфах (фрибет {freebet:.0f}$):")
    print(f"  {'Кэф':>6}  {'Хедж':>8}  {'Прибыль':>10}  {'Извлечение':>12}")
    print(f"  {'─'*6}  {'─'*8}  {'─'*10}  {'─'*12}")
    for o in [1.6, 1.8, 2.0, 2.2, 2.5, 3.0, 4.0]:
        rr = calc_freebet(freebet, o, o)
        marker = " ◄" if abs(o - odds_bm1) < 0.15 else ""
        print(f"  {o:>6.2f}  {rr['hedge']:>7.2f}$  {rr['profit']:>9.2f}$  {rr['extraction_pct']:>10.1f}%{marker}")
    print()


# ── CMD: ADD BONUS ─────────────────────────────────────────────────────────────

def cmd_add(bm: str, amount: float, bonus_type: str = "freebet",
            wager: float = 1.0, expiry: str = "", notes: str = ""):
    conn = get_mb_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("""
        INSERT INTO bonuses (added_at, bookmaker, bonus_type, amount, wager_req, expiry, notes)
        VALUES (?,?,?,?,?,?,?)
    """, (now, bm, bonus_type, amount, wager, expiry, notes))
    save_mb(conn)
    conn.close()
    print(f"\n  ✅ Бонус добавлен: {bm} | {bonus_type} {amount:.0f}$ | вейджер x{wager}\n")


# ── CMD: LIST ──────────────────────────────────────────────────────────────────

def cmd_list():
    conn = get_mb_conn()
    bonuses = conn.execute(
        "SELECT * FROM bonuses ORDER BY status ASC, added_at DESC"
    ).fetchall()
    conn.close()

    print(f"\n{'='*70}")
    print(f"ТРЕКЕР БОНУСОВ")
    print(f"{'='*70}\n")

    if not bonuses:
        print("  Нет бонусов. Добавь через --add.")
        _print_known_bms()
        return

    avail = [b for b in bonuses if b["status"] == "AVAILABLE"]
    used  = [b for b in bonuses if b["status"] == "USED"]

    if avail:
        print(f"  ДОСТУПНЫЕ ({len(avail)}):")
        for b in avail:
            exp = f"  до {b['expiry']}" if b["expiry"] else ""
            print(f"  [{b['id']:>3}] {b['bookmaker']:12}  {b['bonus_type']:8}  "
                  f"{b['amount']:>6.0f}$  x{b['wager_req']}{exp}")

    if used:
        print(f"\n  ИСПОЛЬЗОВАННЫЕ ({len(used)}):")
        for b in used:
            ext = f"  извлечено: +{b['extracted']:.0f}$ ({b['extraction_pct']:.0f}%)" if b["extracted"] else ""
            print(f"  [{b['id']:>3}] {b['bookmaker']:12}  {b['amount']:>6.0f}$  {b['used_at'][:10]}{ext}")

    total_potential = sum(
        calc_freebet(b["amount"], 2.0, 2.0)["profit"]
        for b in avail if b["bonus_type"] == "freebet"
    )
    print(f"\n  💡 Потенциал доступных фрибетов при кэфе 2.0: ~{total_potential:.0f}$")
    print()


def _print_known_bms():
    print(f"\n  Известные букмекеры с бонусами:")
    print(f"  {'BM':12}  {'Тип':8}  {'~Сумма':>8}  {'Вейджер':>8}")
    print(f"  {'─'*12}  {'─'*8}  {'─'*8}  {'─'*8}")
    for bm in KNOWN_BOOKMAKERS:
        est = calc_freebet(bm["typical_amount"], 2.0, 2.0)
        profit_str = f"+{est['profit']:.0f}$" if est else "?"
        print(f"  {bm['bm']:12}  {bm['bonus_type']:8}  {bm['typical_amount']:>6.0f}$  "
              f"x{bm['wager_req']:>5.0f}  → ~{profit_str}")
    print()


# ── CMD: USE ───────────────────────────────────────────────────────────────────

def cmd_use(bonus_id: int, odds_bm1: float, odds_bm2: float,
            event: str = "", extracted: float = None):
    conn = get_mb_conn()
    b = conn.execute("SELECT * FROM bonuses WHERE id=?", (bonus_id,)).fetchone()
    if not b:
        print(f"Бонус #{bonus_id} не найден.")
        conn.close()
        return

    r = calc_freebet(b["amount"], odds_bm1, odds_bm2)
    actual = extracted if extracted is not None else r["profit"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn.execute("""
        UPDATE bonuses SET status='USED', used_at=?, odds_bm1=?, odds_bm2=?,
        hedge_stake=?, extracted=?, extraction_pct=?
        WHERE id=?
    """, (now, odds_bm1, odds_bm2, r["hedge"], actual,
          actual / b["amount"] * 100, bonus_id))
    conn.execute("""
        INSERT INTO mb_trades (bonus_id, created_at, event, odds_bm1, odds_bm2,
        freebet_amt, hedge_stake, guaranteed, extraction_pct, status, actual_profit)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (bonus_id, now, event, odds_bm1, odds_bm2, b["amount"],
          r["hedge"], r["profit"], r["extraction_pct"], "SETTLED", actual))
    save_mb(conn)
    conn.close()
    print(f"\n  ✅ Бонус #{bonus_id} ({b['bookmaker']}) использован. Извлечено: +{actual:.0f}$\n")


# ── CMD: REPORT ───────────────────────────────────────────────────────────────

def cmd_report():
    conn = get_mb_conn()
    bonuses = conn.execute("SELECT * FROM bonuses").fetchall()
    trades  = conn.execute("SELECT * FROM mb_trades WHERE status='SETTLED'").fetchall()
    conn.close()

    avail = [b for b in bonuses if b["status"] == "AVAILABLE"]
    used  = [b for b in bonuses if b["status"] == "USED"]
    total_extracted = sum(t["actual_profit"] or 0 for t in trades)
    total_potential = sum(
        calc_freebet(b["amount"], 2.0, 2.0)["profit"]
        for b in avail if b["bonus_type"] == "freebet"
    )

    print(f"\n{'='*55}")
    print(f"MATCHED BETTING ОТЧЁТ")
    print(f"{'='*55}")
    print(f"\n  Бонусов всего:      {len(bonuses)}")
    print(f"  Использовано:       {len(used)}")
    print(f"  Доступных:          {len(avail)}")
    print()
    print(f"  Уже извлечено:      +{total_extracted:.0f}$")
    print(f"  Потенциал остатка:  ~+{total_potential:.0f}$ (при кэфе 2.0)")
    print(f"  Итого потенциал:    ~+{total_extracted + total_potential:.0f}$")

    if trades:
        print(f"\n  {'BM':12}  {'Фрибет':>8}  {'Кэф':>6}  {'Извлечено':>10}  {'%':>5}")
        print(f"  {'─'*12}  {'─'*8}  {'─'*6}  {'─'*10}  {'─'*5}")
        for t in trades:
            b = next((x for x in bonuses if x["id"] == t["bonus_id"]), None)
            bm_name = b["bookmaker"] if b else "?"
            pct = t["extraction_pct"] or 0
            print(f"  {bm_name:12}  {t['freebet_amt']:>7.0f}$  "
                  f"{t['odds_bm1']:>5.2f}  "
                  f"+{t['actual_profit']:>8.0f}$  {pct:>4.0f}%")
    print()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else "--help"

    def get_arg(flag, default=None, cast=str):
        for i, a in enumerate(sys.argv):
            if a == flag and i+1 < len(sys.argv):
                try: return cast(sys.argv[i+1])
                except: pass
        return default

    if raw == "--calc":
        fb   = get_arg("--freebet", 100.0, float)
        o1   = get_arg("--odds-bm1", 2.0, float)
        o2   = get_arg("--odds-bm2", 2.0, float)
        cmd_calc(fb, o1, o2)

    elif raw == "--add":
        bm   = get_arg("--bm", "?")
        amt  = get_arg("--bonus", 100.0, float)
        typ  = get_arg("--type", "freebet")
        wag  = get_arg("--wager", 1.0, float)
        exp  = get_arg("--expiry", "")
        note = get_arg("--notes", "")
        cmd_add(bm, amt, typ, wag, exp, note)

    elif raw == "--list":
        cmd_list()

    elif raw == "--use":
        bid  = get_arg("--id", None, int)
        o1   = get_arg("--odds-bm1", 2.0, float)
        o2   = get_arg("--odds-bm2", 2.0, float)
        ev   = get_arg("--event", "")
        ext  = get_arg("--extracted", None, float)
        if bid:
            cmd_use(bid, o1, o2, ev, ext)
        else:
            print("Нужно: --use --id 1 --odds-bm1 2.10 --odds-bm2 2.05")

    elif raw == "--report":
        cmd_report()

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
