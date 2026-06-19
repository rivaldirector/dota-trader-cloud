"""
Shared helpers for the daily forecast / settle / report pipeline.

Risk model is the same quarter-Kelly + safety-cap model used in the bank
simulator and validated against betfair_bot.py / paper_trader.py:
    f*    = edge / (odds - 1)
    stake = f* * KELLY_FRAC * bank
    stake clamped to: >= MIN_STAKE, <= bank*SAFETY_PCT, <= CAP_USD, <= bank

edge here is the same definition used everywhere in this codebase
(signal_engine.py, the 326-bet backtest dataset): edge = bm_odds/pin_odds - 1,
i.e. how much extra payout the soft book offers vs. the Pinnacle-implied
fair price. This is mathematically the correct Kelly numerator because
p_fair ≈ 1/pin_odds, so (bm_odds*p_fair - 1)/(bm_odds-1) == edge/(bm_odds-1).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# ── Risk model constants (ground-truthed against betfair_bot.py / paper_trader.py) ──
MIN_STAKE   = 2.0
SAFETY_PCT  = 0.08     # never risk more than 8% of bank on one bet
KELLY_FRAC  = 0.25     # quarter-Kelly
CAP_USD     = float(os.getenv("DAILY_CAP_USD", "30"))   # early-stage dollar cap
EDGE_THRESHOLD = 0.12
SOFT_BOOKS  = {"GGBet", "Bet365"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sb_get(table: str, qs: str) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
                      headers={**SB_HEADERS, "Prefer": "return=representation"},
                      timeout=20)
    r.raise_for_status()
    return r.json()


def sb_insert(table: str, rows: list[dict]) -> list:
    if not rows:
        return []
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                       headers={**SB_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"},
                       json=rows, timeout=30)
    if r.status_code not in (200, 201):
        print(f"  [SB ERROR] insert {table}: {r.status_code} {r.text[:200]}")
        return []
    return r.json()


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> list:
    """Insert-or-update keyed on `on_conflict` (comma-separated column list)."""
    if not rows:
        return []
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                       headers={**SB_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"},
                       json=rows, timeout=30)
    if r.status_code not in (200, 201):
        print(f"  [SB ERROR] upsert {table}: {r.status_code} {r.text[:200]}")
        return []
    return r.json()


def sb_patch(table: str, qs: str, patch: dict) -> None:
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
                        headers=SB_HEADERS, json=patch, timeout=20)
    if r.status_code not in (200, 204):
        print(f"  [SB ERROR] patch {table}: {r.status_code} {r.text[:200]}")


def get_bank() -> dict:
    rows = sb_get("daily_bankroll", "id=eq.1")
    return rows[0] if rows else {"current_bank_usd": 1000, "peak_bank_usd": 1000, "start_bank_usd": 1000}


def set_bank(current: float, peak: float) -> None:
    sb_patch("daily_bankroll", "id=eq.1", {
        "current_bank_usd": round(current, 2),
        "peak_bank_usd": round(peak, 2),
        "updated_at": now_iso(),
    })


def kelly_stake(edge_pct: float, bm_odds: float, bank: float) -> float:
    """Quarter-Kelly stake in USD, clamped by safety%, dollar cap, and bank size."""
    b = bm_odds - 1
    if b <= 0 or bank <= 0.01:
        return 0.0
    edge = edge_pct / 100.0
    fstar = edge / b
    stake = fstar * KELLY_FRAC * bank
    stake = max(stake, MIN_STAKE)
    stake = min(stake, bank * SAFETY_PCT)
    stake = min(stake, CAP_USD)
    stake = min(stake, bank)
    return round(stake, 2)


def fmt_usd(x: float) -> str:
    sign = "+" if x > 0 else ("" if x == 0 else "-")
    return f"{sign}${abs(x):,.2f}"


def check_betsapi_alive(token: str) -> tuple[bool, str]:
    """Cheap canary call — confirms the BetsAPI token still works before a
    script burns its whole run on a dead key (which otherwise looks like a
    quiet 'no data found' instead of an actual outage)."""
    try:
        r = requests.get("https://api.b365api.com/v3/events/upcoming",
                          params={"token": token, "sport_id": 151, "page": 1}, timeout=15)
        if r.status_code in (401, 403):
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if not data.get("success"):
            return False, str(data.get("error") or data)[:200]
        return True, "ok"
    except Exception as ex:
        return False, str(ex)[:200]


def sb_set_pipeline_status(script: str, ok: bool, message: str = "") -> None:
    """Upsert a one-row-per-script health record so the read-only daily
    summary task can tell the difference between 'ran fine, nothing to
    report' and 'didn't actually run' (e.g. BetsAPI token expired)."""
    sb_upsert("pipeline_status", [{
        "script": script,
        "status": "ok" if ok else "error",
        "message": (message or "")[:500],
        "checked_at": now_iso(),
    }], on_conflict="script")
