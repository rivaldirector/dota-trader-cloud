import requests

class PandaScoreClient:
    def __init__(self, token: str, base_url: str = "https://api.pandascore.co"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get_upcoming_dota_matches(self, limit: int = 50):
        if not self.token:
            raise RuntimeError("PANDASCORE_TOKEN is empty. Put it into .env")
        url = f"{self.base_url}/dota2/matches/upcoming"
        resp = requests.get(url, headers=self._headers(), params={"per_page": limit}, timeout=20)
        resp.raise_for_status()
        return resp.json()
