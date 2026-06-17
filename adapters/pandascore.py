import requests


class PandaScoreClient:
    def __init__(self, token: str, base_url: str = "https://api.pandascore.co"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, params=None):
        if not self.token:
            raise RuntimeError("PANDASCORE_TOKEN is empty. Put it into .env")

        url = f"{self.base_url}{path}"

        resp = requests.get(
            url,
            headers=self._headers(),
            params=params or {},
            timeout=30,
        )

        resp.raise_for_status()
        return resp.json()

    def get_upcoming_dota_matches(self, limit: int = 50):
        return self._get(
            "/dota2/matches/upcoming",
            params={"per_page": limit},
        )

    def get_past_dota_matches(self, limit: int = 100, page: int = 1):
        return self._get(
            "/dota2/matches/past",
            params={
                "per_page": limit,
                "page": page,
            },
        )

    def get_running_dota_matches(self, limit: int = 50):
        return self._get(
            "/dota2/matches/running",
            params={"per_page": limit},
        )

    def get_match_by_id(self, match_id: int):
        return self._get(f"/dota2/matches/{match_id}")
