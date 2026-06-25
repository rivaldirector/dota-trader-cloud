#!/usr/bin/env python3
"""
calibrate_signals.py — walk-forward калибровка весов ансамбля (Elo/Form/H2H).

Алгоритм:
  1. Загружает 6 месяцев матчей из betsapi_events + elo_pandascore_history
  2. Walk-forward: для каждого матча вычисляет сигналы ТОЛЬКО по данным ДО него
  3. Grid search по весам [w_elo, w_form, w_h2h] → минимум log-loss
  4. Сохраняет результат в model_config

Run: python3 scripts/calibrate_signals.py [--months 6] [--dry-run]
Workflow: recalibrate.yml — ежедневно, только если 30+ новых settled ставок.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT / "scripts"))
from signals import (
    compute_form,
    compute_h2h,
    compute_fatigue,
    fatigue_adjustment,
    normalize_team,
    fuzzy_match,
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]
SB_HEADERS   = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

START_ELO = 1500.0
K_FACTOR  = 32
EPS       = 1e-9


def sb_get(table, qs):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def sb_upsert_config(key, value, note=None):
    body = {"key": key, "value": str(value), "updated_at": datetime.now(timezone.utc).isoformat()}
    if note:
        body["note"] = note
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/model_config?on_conflict=key",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=body, timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  [WARN] model_config upsert: {r.status_code}")


def elo_exp(ra, rb):
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))


def log_loss(probs, outcomes):
    n = len(probs)
    if n == 0:
        return float("inf")
    return -sum(
        y * math.log(max(EPS, p)) + (1 - y) * math.log(max(EPS, 1 - p))
        for p, y in zip(probs, outcomes)
    ) / n


def brier_score(probs, outcomes):
    n = len(probs)
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n if n else float("inf")


def accuracy(probs, outcomes):
    n = len(probs)
    return sum(1 for p, y in zip(probs, outcomes) if (p >= 0.5) == bool(y)) / n if n else 0.0


def evaluate_weights(w_elo, w_form, w_h2h, data):
    probs, outcomes = [], []
    for elo_p, form_p, h2h_p, fat_adj, outcome in data:
        wt, ws = w_elo, w_elo * elo_p
        if form_p is not None:
            wt += w_form; ws += w_form * form_p
        if h2h_p is not None:
            wt += w_h2h; ws += w_h2h * h2h_p
        p = max(EPS, min(1 - EPS, ws / wt + fat_adj))
        probs.append(p)
        outcomes.append(outcome)
    return log_loss(probs, outcomes), probs, outcomes


def grid_search(data, steps=12):
    best_loss, best_w = float("inf"), (0.60, 0.25, 0.15)
    vals = [i / steps for i in range(1, steps)]
    print(f"  Grid search: {len(vals)**2} комбинаций...")
    for w_elo in vals:
        for w_form in vals:
            w_h2h = max(0.01, 1.0 - w_elo - w_form)
            if w_h2h > 0.6:
                continue
            loss, _, _ = evaluate_weights(w_elo, w_form, w_h2h, data)
            if loss < best_loss:
                best_loss = loss
                best_w = (w_elo, w_form, w_h2h)
    s = sum(best_w)
    return round(best_w[0]/s, 4), round(best_w[1]/s, 4), round(best_w[2]/s, 4), best_loss


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months",      type=int,  default=6)
    parser.add_argument("--min-matches", type=int,  default=3)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=args.months * 30)).timestamp())
    print(f"Калибровка за {args.months} мес (с {datetime.fromtimestamp(cutoff_ts).strftime('%d.%m.%Y')})")

    print("Загружаем историю...")
    page, all_rows, offset = 1000, [], 0
    while True:
        chunk = sb_get("betsapi_events",
            f"sport_tag=eq.dota2&status=eq.ended&winner=neq."
            f"&select=home_team,away_team,winner,start_time"
            f"&order=start_time.asc&limit={page}&offset={offset}")
        if not chunk: break
        all_rows.extend(chunk)
        if len(chunk) < page: break
        offset += page

    ps_rows, offset = [], 0
    while True:
        chunk = sb_get("elo_pandascore_history",
            f"winner=neq.&select=home_team,away_team,winner,start_time"
            f"&order=start_time.asc&limit={page}&offset={offset}")
        if not chunk: break
        ps_rows.extend(chunk)
        if len(chunk) < page: break
        offset += page

    seen, history = set(), []
    for r in all_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None: continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen: continue
        seen.add(key); history.append((int(st), t1, t2, 1.0 if w == t1 else 0.0))

    for r in ps_rows:
        t1, t2, w, st = r.get("home_team"), r.get("away_team"), r.get("winner"), r.get("start_time")
        if not t1 or not t2 or not w or st is None: continue
        key = (normalize_team(t1), normalize_team(t2), int(st) // 3600)
        if key in seen: continue
        nw = normalize_team(w)
        act_h = 1.0 if nw == normalize_team(t1) else (0.0 if nw == normalize_team(t2) else None)
        if act_h is None: continue
        seen.add(key); history.append((int(st), t1, t2, act_h))

    history.sort(key=lambda r: r[0])
    print(f"  Итого матчей: {len(history)}")

    print("Walk-forward вычисление сигналов...")
    elo: dict[str, float] = {}
    calibration_data = []

    for i, (st, t1, t2, act_h) in enumerate(history):
        e1, e2 = elo.get(t1, START_ELO), elo.get(t2, START_ELO)
        elo_p  = elo_exp(e1, e2)

        if st >= cutoff_ts:
            past = history[:i]
            if len(past) >= args.min_matches:
                calibration_data.append((
                    elo_p,
                    compute_form(t1, past, n=10),
                    compute_h2h(t1, t2, past, n=8),
                    fatigue_adjustment(compute_fatigue(t1, past, st), compute_fatigue(t2, past, st)),
                    float(act_h),
                ))

        ea = elo_exp(e1, e2)
        elo[t1] = e1 + K_FACTOR * (act_h - ea)
        elo[t2] = e2 + K_FACTOR * ((1 - act_h) - (1 - ea))

    print(f"  Калибровочная выборка: {len(calibration_data)} матчей")
    if len(calibration_data) < 10:
        print("  Недостаточно данных — выход"); return

    base_loss, base_probs, base_outcomes = evaluate_weights(0.60, 0.25, 0.15, calibration_data)
    print(f"\nBaseline (0.60/0.25/0.15): loss={base_loss:.5f}  brier={brier_score(base_probs,base_outcomes):.5f}  acc={accuracy(base_probs,base_outcomes):.3f}")

    print("\nОптимизация весов...")
    w_elo_opt, w_form_opt, w_h2h_opt, opt_loss = grid_search(calibration_data)
    _, opt_probs, opt_outcomes = evaluate_weights(w_elo_opt, w_form_opt, w_h2h_opt, calibration_data)
    improvement = (base_loss - opt_loss) / base_loss * 100

    print(f"Оптимум: elo={w_elo_opt}  form={w_form_opt}  h2h={w_h2h_opt}")
    print(f"  loss={opt_loss:.5f} ({improvement:+.2f}%)  brier={brier_score(opt_probs,opt_outcomes):.5f}  acc={accuracy(opt_probs,opt_outcomes):.3f}")

    if args.dry_run:
        print("\n[dry-run] не сохраняем"); return

    sb_upsert_config("w_elo",  w_elo_opt,  "Вес Elo (калибровано)")
    sb_upsert_config("w_form", w_form_opt, "Вес формы (калибровано)")
    sb_upsert_config("w_h2h",  w_h2h_opt,  "Вес H2H (калибровано)")
    sb_upsert_config("calibrated_at",      datetime.now(timezone.utc).isoformat())
    sb_upsert_config("calibration_n",      len(calibration_data))
    sb_upsert_config("calibration_logloss", round(opt_loss, 6))
    sb_upsert_config("calibration_brier",   round(brier_score(opt_probs, opt_outcomes), 6))
    sb_upsert_config("calibration_acc",     round(accuracy(opt_probs, opt_outcomes), 4))
    print("✓ Веса сохранены в model_config")


if __name__ == "__main__":
    main()
