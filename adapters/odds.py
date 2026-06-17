import requests
from config import settings


class OddsApiClient:
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self):
        self.api_key = settings.odds_api_key

    def get_sports(self):
        resp = requests.get(
            f"{self.BASE_URL}/sports",
            params={"apiKey": self.api_key},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_dota_odds(self):
        resp = requests.get(
            f"{self.BASE_URL}/sports/esports_dota2/odds",
            params={
                "apiKey": self.api_key,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
