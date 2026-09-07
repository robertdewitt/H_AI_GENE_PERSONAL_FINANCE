import os
from pathlib import Path
from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_ignore_empty=True,
    )

    app_name: str = "Financial Hygiene"
    debug: bool = True

    # Major.minor prefix — patch is auto-derived from git commit count (see app/build_info.py).
    # Bump this manually only for significant feature releases.
    app_version: str = "0.2"

    # "sqlite" or "postgresql" — set via env to switch engines
    db_backend: str = "sqlite"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'finance.db'}"

    upload_dir: str = str(BASE_DIR / "uploads")

    # FX
    base_currency: str = "USD"
    fx_api_key: str = ""  # for future live-rate provider

    # Transfer detection
    transfer_date_window_days: int = 3
    transfer_amount_tolerance: Decimal = Decimal("0.01")

    # Date parsing — True = dd/mm (UK/EU), False = mm/dd (US)
    date_dayfirst: bool = True

    # Import performance — rows flushed per batch for bulk inserts
    import_batch_size: int = 5000

    # ── Local LLM (Ollama) ──
    # Kept here rather than as module constants so the model can be changed
    # without a code edit. Every caller degrades gracefully when the daemon
    # is absent — the app must work with no local model installed.
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.6:35b-a3b"
    ollama_embed_model: str = "embeddinggemma"
    ollama_timeout: int = 10
    # Multimodal model, for documents that arrive as screenshots or scans
    # with no text layer. Vision runs are slower than text, hence the
    # separate timeout.
    ollama_vision_model: str = "gemma4"
    ollama_vision_timeout: int = 120
    # Embedding-backed description similarity. Merchant strings compare far
    # better as vectors than as character sequences ("TST* KI'S RESTAURANT"
    # vs "KIS RESTAURANT LONDON"), and vectors are cached so the comparison
    # is cheap enough to run over every candidate pair.
    use_embeddings: bool = True

    # ── Authentication (WebAuthn passkeys + sessions) ──
    # rp_id ("relying party id") must match the *hostname* the browser
    # sees (no scheme, no port). For localhost / 127.0.0.1 the browser
    # accepts "localhost". Override via env for LAN/remote deployments —
    # WebAuthn also requires HTTPS off-localhost.
    rp_id: str = "localhost"
    rp_origin: str = "http://localhost:8000"
    rp_name: str = "Financial Hygiene"


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(BASE_DIR / "data", exist_ok=True)
