#!/usr/bin/env python3
"""
seed_league_tiers.py — заполняет таблицу league_tiers в Supabase.

Тиры:
  T1 (tier=1): TI, ESL Pro League, DPC Major, DreamLeague Major
               edge_min=0.02, kelly_cap=0.05  ← рынок острее, но ликвидность есть
  T2 (tier=2): DPC Regional, ESL Challenger, BetBoom, ESL One
               edge_min=0.03, kelly_cap=0.05
  T3 (tier=3): qualifier, open qualifier, closed qual, regional qual, все остальные
               edge_min=0.05, kelly_cap=0.035 ← меньше кэп: рынок мягче но волатильнее

Логика тиров:
  T1 — самые большие турниры с наибольшей ликвидностью букмекеров.
       Рынок эффективнее → edge нужен меньше (2%), но он надёжнее.
  T2 — средние лиги, рынок менее острый → требуем 3% edge.
  T3 / квалификаторы — команды нестабильны, ростеры меняются,
       букмекеры дают большую маржу → нужен 5% edge, но кэп снижаем
       чтобы не перекладывать в один нестабильный матч.

Run:
    python3 scripts/seed_league_tiers.py
"""

import os
import requests
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

TIERS = [
    # ── T1: самые крупные турниры ────────────────────────────────────────────
    {"pattern": "the international",         "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "TI"},
    {"pattern": "ti2026",                    "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "TI 2026"},
    {"pattern": "esl pro league",            "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "ESL Pro League"},
    {"pattern": "dreamleague season",        "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "DreamLeague Major"},
    {"pattern": "pgl wallachia",             "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "PGL Wallachia Major"},
    {"pattern": "riyadh masters",            "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "Riyadh Masters"},
    {"pattern": "esl one",                   "tier": 1, "edge_min": 0.02, "kelly_cap": 0.05, "note": "ESL One Major"},

    # ── T2: средние турниры / DPC региональные ───────────────────────────────
    {"pattern": "dpc",                       "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "DPC Regional"},
    {"pattern": "esl challenger",            "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "ESL Challenger"},
    {"pattern": "betboom dacha",             "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "BetBoom Dacha"},
    {"pattern": "xtreme gaming",             "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "XG Invitational"},
    {"pattern": "bali major",                "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "Bali Major"},
    {"pattern": "epulze",                    "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "Epulze"},
    {"pattern": "elite league",              "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "Elite League"},
    {"pattern": "road to ti",                "tier": 2, "edge_min": 0.03, "kelly_cap": 0.05, "note": "Road to TI"},

    # ── T3: квалификаторы и малые турниры ────────────────────────────────────
    {"pattern": "qualifier",                 "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "Generic qualifier"},
    {"pattern": "open qualifier",            "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "Open qualifier"},
    {"pattern": "closed qualifier",          "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "Closed qualifier"},
    {"pattern": "regional qualifier",        "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "Regional qualifier"},
    {"pattern": "ti qualifier",              "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "TI qualifier"},
    {"pattern": "last chance qualifier",     "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "LCQ"},
    {"pattern": "hellbear",                  "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "HellBear Smashers"},
    {"pattern": "thunderpick",               "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "ThunderPick"},
    {"pattern": "gamers galaxy",             "tier": 3, "edge_min": 0.05, "kelly_cap": 0.035, "note": "Gamers Galaxy"},
]


def seed():
    url = f"{SUPABASE_URL}/rest/v1/league_tiers?on_conflict=pattern"
    r = requests.post(url, headers=HEADERS, json=TIERS, timeout=30)
    if r.status_code in (200, 201, 204):
        print(f"OK: {len(TIERS)} записей залито/обновлено в league_tiers")
    else:
        print(f"ERROR {r.status_code}: {r.text[:300]}")


if __name__ == "__main__":
    seed()
