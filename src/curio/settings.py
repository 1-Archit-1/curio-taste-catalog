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
