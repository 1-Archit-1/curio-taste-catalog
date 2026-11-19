from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, read from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_pool_timeout_seconds: float = 5.0
    worker_heartbeat_seconds: float = 30.0
    log_level: str = "INFO"

    # Process-split sealed-box keypair (ADR 0002):
    # - API processes receive only KEY_PUBLIC_KEY (can encrypt, never decrypt).
    # - Worker processes receive KEY_PRIVATE_KEY (can decrypt).
    key_public_key: str | None = None
    key_private_key: str | None = None

    # Control-plane sessions & public addon URL
    session_cookie_name: str = "curio_session"
    session_ttl_days: int = 30
    session_cookie_secure: bool = False
    public_base_url: str | None = None
