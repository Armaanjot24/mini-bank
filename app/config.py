import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
MODEL_PATH = DATA_DIR / "fraud_model.joblib"

load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise RuntimeError(
            f"{name} is missing from {BASE_DIR / '.env'}. See .env.example."
        )
    return raw.strip().strip('"').strip("'")


MYSQL_HOST = _require("MYSQL_HOST")
MYSQL_USER = _require("MYSQL_USER")
MYSQL_PASSWORD = _require("MYSQL_PASSWORD")
MYSQL_DB = _require("MYSQL_DB")

DEFAULT_DAILY_TRANSFER_LIMIT = Decimal("100000.00")
DEFAULT_MAX_TXN_AMOUNT = Decimal("50000.00")

RISK_FLAG_THRESHOLD = 0.50
RISK_HIGH_THRESHOLD = 0.80

DATA_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)
