import os
from pathlib import Path

from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "Financial Hygiene"
    debug: bool = True

    # "sqlite" or "postgresql" — set via env to switch engines
    db_backend: str = "sqlite"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'finance.db'}"

    upload_dir: str = str(BASE_DIR / "uploads")

    # FX
    base_currency: str = "USD"
    fx_api_key: str = ""  # for future live-rate provider

    # Transfer detection
    transfer_date_window_days: int = 3
    transfer_amount_tolerance: float = 0.01

    # Date parsing — True = dd/mm (UK/EU), False = mm/dd (US)
    date_dayfirst: bool = True

    # Import performance — rows flushed per batch for bulk inserts
    import_batch_size: int = 5000

    class Config:
        env_file = str(BASE_DIR / ".env")


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(BASE_DIR / "data", exist_ok=True)
