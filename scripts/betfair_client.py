#!/usr/bin/env python3
"""
Betfair API client — авторизация, поиск рынков, котировки, ставки.

Docs: https://developer.betfair.com/exchange-api/

Требует переменных в .env:
    BETFAIR_USERNAME=...
    BETFAIR_PASSWORD=...
    BETFAIR_APP_KEY=...          # из developer.betfair.com
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

# ── Betfair endpoints ─────────────────────────────────────────────────────────

LOGIN_URL   = "https://identitysso.betfair.com/api/login"
BETTING_URL = "https://api.betfair.com/exchange/betting/rest/v1.0"
ACCOUNT_URL = "https://api.betfair.com/exchange/account/rest/v1.0"

# Betfair event type IDs
ESPORTS_EVENT_TYPE = "27454571"   # e-Sports

# Market types we care about
MATCH_ODDS = "MATCH_ODDS"

# Min liquidity threshold — don't bet if available to back < this
MIN_AVAILABLE = 50.0  # GBP/EUR


class BetfairError(Exception):
    pass


class BetfairClient:
    """
    Thin wrapper around Betfair Exchange REST API.

    Usage:
        client = BetfairClient()
        client.login()
        markets = client.list_esports_markets()
        book = client.get_market_book([m['marketId'] for m in markets])
        client.place_bet(market_id, selection_id, price, size)
        client.logout()
    """

    def __init__(self):
        self.username  = os.getenv("BETFAIR_USERNAME", "")
        self.password  = os.getenv("BETFAIR_PASSWORD", "")
        self.app_key   = os.getenv("BETFAIR_APP_KEY", "")
        self.session   = None
        self._http     = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self) -> None:
        """Authenticate and store session token."""
        if not all([self.username, self.password, self.app_key]):
            raise BetfairError(
                "Missing Betfair credentials. Set BETFAIR_USERNAME, "
                "BETFAIR_PASSWORD, BETFAIR_APP_KEY in .env"
            )
        resp = self._http.post(
            LOGIN_URL,
            data={"username": self.username, "password": self.password},
            headers={"X-Application": self.app_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "SUCCESS":
            raise BetfairError(f"Login failed: {data.get('error', data)}")
        self.session = data["token"]
        self._http.headers.update({
            "X-Application":    self.app_key,
            "X-Authentication": self.session,
        })
        print(f"[Betfair] Logged in as {self.username}")

    def logout(self) -> None:
        try:
            self._http.post(
                "https://identitysso.betfair.com/api/logout", timeout=10
            )
        except Exception:
            pass
        self.session = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *_):
        self.logout()

    # ── Core request ──────────────────────────────────────────────────────────

    def _post(self, endpoint: str, body: dict) -> Any:
        url  = f"{BETTING_URL}/{endpoint}/"
        resp = self._http.post(url, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("faultcode"):
            raise BetfairError(f"API fault: {data}")
        return data

    # ── Market catalogue ──────────────────────────────────────────────────────

    def list_esports_markets(
        self,
        hours_ahead: int = 24,
        max_results: int = 200,
    ) -> list[dict]:
        """
        Return upcoming esports MATCH_ODDS markets.
        Each dict has: marketId, marketName, event (name, openDate), runners.
        """
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=hours_ahead)

        body = {
            "filter": {
                "eventTypeIds":  [ESPORTS_EVENT_TYPE],
                "marketTypeCodes": [MATCH_ODDS],
                "marketStartTime": {
                    "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "inPlayOnly": False,
            },
            "marketProjection": [
                "EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"
            ],
            "sort":       "FIRST_TO_START",
            "maxResults": str(max_results),
            "locale":     "en",
        }
        return self._post("listMarketCatalogue", body)

    # ── Market book (live prices) ─────────────────────────────────────────────

    def get_market_book(self, market_ids: list[str]) -> list[dict]:
        """
        Return current best available back/lay prices for a list of markets.
        Each dict has: marketId, status, runners[].
        Each runner has: selectionId, status, ex.availableToBack/Lay.
        """
        if not market_ids:
            return []
        body = {
            "marketIds": market_ids,
            "priceProjection": {
                "priceData":          ["EX_BEST_OFFERS"],
                "exBestOffersOverrides": {
                    "bestPricesDepth": 3,
                    "rollupModel":     "STAKE",
                    "rollupLimit":     10,
                },
                "virtualise": False,
            },
            "matchProjection": "NO_ROLLUP",
            "currencyCode":    "GBP",
        }
        return self._post("listMarketBook", body)

    # ── Account ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Return available balance in GBP."""
        resp = self._http.post(
            f"{ACCOUNT_URL}/getAccountFunds/",
            json={"wallet": "UK wallet"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("availableToBetBalance", 0))

    # ── Place bet ─────────────────────────────────────────────────────────────

    def place_bet(
        self,
        market_id: str,
        selection_id: int,
        price: float,
        size: float,
        side: str = "BACK",
    ) -> dict:
        """
        Place a single bet.

        Args:
            market_id:    Betfair market ID (e.g. "1.234567890")
            selection_id: Runner/team selection ID
            price:        Decimal odds to back at (e.g. 1.85)
            size:         Stake in GBP (e.g. 10.0)
            side:         "BACK" (we think they'll win) or "LAY"

        Returns:
            placeOrders response dict with status and bet ID.
        """
        # Betfair minimum stake is £2
        if size < 2.0:
            raise BetfairError(f"Stake {size:.2f} below Betfair minimum £2")

        # Round price to valid Betfair increment
        price = _snap_price(price)

        body = {
            "marketId": market_id,
            "instructions": [{
                "orderType":          "LIMIT",
                "selectionId":        selection_id,
                "side":               side,
                "limitOrder": {
                    "size":            round(size, 2),
                    "price":           price,
                    "persistenceType": "LAPSE",  # cancel if unmatched at start
                },
            }],
            "customerRef": f"dota_trader_{int(time.time())}",
        }
        result = self._post("placeOrders", body)
        report = result.get("instructionReports", [{}])[0]
        status = result.get("status", "UNKNOWN")
        if status != "SUCCESS":
            raise BetfairError(
                f"placeOrders failed: {status} — {report.get('errorCode', '')}"
            )
        return {
            "status":        status,
            "bet_id":        report.get("betId"),
            "size_matched":  report.get("sizeMatched", 0),
            "avg_price":     report.get("averagePriceMatched", price),
            "market_id":     market_id,
            "selection_id":  selection_id,
        }

    # ── Cancel all unmatched ──────────────────────────────────────────────────

    def cancel_all(self, market_id: str) -> dict:
        return self._post("cancelOrders", {"marketId": market_id})


# ── Helpers ───────────────────────────────────────────────────────────────────

# Valid Betfair price increments
_PRICE_LADDER = (
    [(1.01 + i * 0.01) for i in range(99)]       # 1.01 – 2.00  step 0.01
    + [(2.02 + i * 0.02) for i in range(50)]      # 2.02 – 3.00  step 0.02
    + [(3.05 + i * 0.05) for i in range(40)]      # 3.05 – 5.00  step 0.05
    + [(5.10 + i * 0.10) for i in range(50)]      # 5.10 – 10.0  step 0.10
    + [(10.5 + i * 0.5)  for i in range(19)]      # 10.5 – 20.0  step 0.50
    + [(21.0 + i * 1.0)  for i in range(30)]      # 21   – 50    step 1
    + [(55.0 + i * 5.0)  for i in range(10)]      # 55   – 100   step 5
    + [(110.0 + i * 10.0) for i in range(9)]      # 110  – 990   step 10
    + [1000.0]
)
_PRICE_SET = set(round(p, 2) for p in _PRICE_LADDER)


def _snap_price(price: float) -> float:
    """Round price DOWN to the nearest valid Betfair increment."""
    price = round(price, 10)
    valid = sorted(_PRICE_SET)
    snapped = valid[0]
    for v in valid:
        if v <= price:
            snapped = v
        else:
            break
    return snapped


def best_back_price(runner: dict) -> tuple[float, float]:
    """
    Return (best_back_price, available_size) from a runner's exchange data.
    Returns (0, 0) if no back offers.
    """
    offers = (runner.get("ex") or {}).get("availableToBack", [])
    if not offers:
        return 0.0, 0.0
    best = offers[0]  # sorted best first
    return float(best.get("price", 0)), float(best.get("size", 0))


def parse_market(catalogue_item: dict, book_item: dict) -> dict | None:
    """
    Merge catalogue + book into a clean dict with runner names and prices.
    Returns None if market is not OPEN or has no runners with prices.
    """
    if book_item.get("status") != "OPEN":
        return None

    runners_cat = {
        r["selectionId"]: r.get("runnerName", "?")
        for r in catalogue_item.get("runners", [])
    }
    event_name  = (catalogue_item.get("event") or {}).get("name", "?")
    market_name = catalogue_item.get("marketName", "?")
    start_time  = catalogue_item.get("marketStartTime", "")

    parsed_runners = []
    for r in book_item.get("runners", []):
        if r.get("status") != "ACTIVE":
            continue
        sid   = r["selectionId"]
        name  = runners_cat.get(sid, str(sid))
        price, avail = best_back_price(r)
        if price > 1.0:
            parsed_runners.append({
                "selection_id": sid,
                "name":         name,
                "back_price":   price,
                "available":    avail,
            })

    if len(parsed_runners) < 2:
        return None

    return {
        "market_id":   catalogue_item["marketId"],
        "event_name":  event_name,
        "market_name": market_name,
        "start_time":  start_time,
        "runners":     parsed_runners,
    }


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with BetfairClient() as bf:
        balance = bf.get_balance()
        print(f"Balance: £{balance:.2f}")

        markets = bf.list_esports_markets(hours_ahead=48)
        print(f"Found {len(markets)} esports markets")

        if markets:
            ids   = [m["marketId"] for m in markets[:20]]
            books = bf.get_market_book(ids)

            book_map = {b["marketId"]: b for b in books}
            for cat in markets[:20]:
                book = book_map.get(cat["marketId"])
                if not book:
                    continue
                parsed = parse_market(cat, book)
                if not parsed:
                    continue
                print(f"\n  {parsed['event_name']} — {parsed['market_name']}")
                print(f"  Start: {parsed['start_time']}")
                for r in parsed["runners"]:
                    print(f"    {r['name']:30} back={r['back_price']:.2f}  avail=£{r['available']:.0f}")
