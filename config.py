from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
import os

load_dotenv()

class Settings(BaseModel):
    start_bank: float = float(os.getenv("START_BANK", "100"))
    currency: str = os.getenv("CURRENCY", "USD")
    trading_mode: str = os.getenv("TRADING_MODE", "paper")
    database_path: Path = Path(os.getenv("DATABASE_PATH", "storage/dota_trader.sqlite3"))
    min_edge: float = float(os.getenv("MIN_EDGE", "0.06"))
    max_stake_pct: float = float(os.getenv("MAX_STAKE_PCT", "0.03"))

    pandascore_token: str = os.getenv("PANDASCORE_TOKEN", "")
    pandascore_base_url: str = os.getenv("PANDASCORE_BASE_URL", "https://api.pandascore.co")

    odds_api_key: str = os.getenv("ODDS_API_KEY", "")

    rapidapi_key: str = os.getenv("RAPIDAPI_KEY", "")

    dotascore_api_key: str = os.getenv("DOTASCORE_API_KEY", "")
    dotascore_base_url: str = os.getenv("DOTASCORE_BASE_URL", "https://api.dotascore.live")

    stratz_token: str = os.getenv("STRATZ_TOKEN", "")

    betsapi_token: str = os.getenv("BETSAPI_TOKEN", "")
    betsapi_base_url: str = os.getenv("BETSAPI_BASE_URL", "https://api.b365api.com")

settings = Settings()
