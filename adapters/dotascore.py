"""
DotaScore API adapter.
Docs: https://api.dotascore.live  (no public docs — probing endpoints)
"""
from __future__ import annotations
from typing import Optional
import requests
from config import settings


class DotaScoreClient:
    BASE = "https://api.dotascore.live"

    def __init__(self):
        self.key = settings.dotascore_api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": self.key,
            "Accept": "application/json",
        })

    def _get(self, path: str, params: Optional[dict] = None):
        r = self.session.get(f"{self.BASE}{path}", params=params or {}, timeout=20)
        r.raise_for_status()
        return r.json()

    def probe(self) -> dict:
        """
        Probe known endpoint patterns to discover what's available.
        Returns dict of {endpoint: response_keys or error}.
        """
        candidates = [
            "/v1/matches/upcoming",
            "/v2/matches/upcoming",
            "/matches/upcoming",
            "/v1/dota2/matches",
            "/v1/odds",
            "/v1/matches/odds",
            "/api/v1/matches",
            "/api/matches",
        ]
        results = {}
        for path in candidates:
            try:
                data = self._get(path)
                if isinstance(data, list):
                    results[path] = f"list[{len(data)}] keys={list(data[0].keys()) if data else '[]'}"
                elif isinstance(data, dict):
                    results[path] = f"dict keys={list(data.keys())[:10]}"
                else:
                    results[path] = str(data)[:80]
            except requests.HTTPError as e:
                results[path] = f"HTTP {e.response.status_code}"
            except Exception as e:
                results[path] = f"ERR {e}"
        return results

    def get_upcoming_matches(self) -> list[dict]:
        """Try to get upcoming Dota 2 matches with odds."""
        for path in ["/v1/matches/upcoming", "/v2/matches/upcoming", "/matches/upcoming"]:
            try:
                data = self._get(path)
                if data:
                    return data if isinstance(data, list) else data.get("data", [])
            except Exception:
                continue
        return []

    def get_odds(self, match_id: Optional[str] = None) -> list[dict]:
        """Try to get odds data."""
        paths = ["/v1/odds", "/v1/matches/odds"]
        if match_id:
            paths = [f"/v1/matches/{match_id}/odds"] + paths
        for path in paths:
            try:
                data = self._get(path)
                return data if isinstance(data, list) else data.get("data", [])
            except Exception:
                continue
        return []
