from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, read from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    db_pool_max_size: int = 10
    db_pool_timeout_seconds: float = 5.0
    worker_heartbeat_seconds: float = 30.0
    log_level: str = "INFO"

    # TMDB rate limiting (#4). The limit is per process: the total request budget divided by
    # the number of worker processes (TMDB asks for roughly 40-50 requests/second per IP).
    tmdb_requests_per_second_total: float = 40.0
    worker_process_count: int = 1
    tmdb_poster_base_url: str = "https://image.tmdb.org/t/p/w500"

    # Embeddings (#4). The stored column is vector(768); changing the dimension needs a migration.
    embedding_model: str = "gemini-embedding-001"
    # Gemini batchEmbedContents accepts at most 100 requests per call.
    embedding_batch_size: int = Field(default=100, ge=1, le=100)

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
    frontend_dist_dir: str | None = None

    @property
    def tmdb_requests_per_second_per_process(self) -> float:
        return self.tmdb_requests_per_second_total / max(1, self.worker_process_count)
