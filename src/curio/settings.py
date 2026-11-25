from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SIMKL_CALLBACK_PATH = "/api/simkl/callback"


class Settings(BaseSettings):
    """Process configuration, read from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    db_pool_max_size: int = 10
    db_pool_timeout_seconds: float = 5.0
    worker_heartbeat_seconds: float = 30.0
    log_level: str = "INFO"
    # Job queue (#25). A running job holds a lease that its worker renews every lease/3 seconds;
    # another worker reclaims the job once the lease has expired.
    job_lease_seconds: int = Field(default=120, ge=10)
    # Public Letterboxd diary RSS sources are polled by the worker at most this often (#5).
    rss_refresh_interval_hours: int = Field(default=6, ge=1)

    # SSE job streams (#25). Limits are per API process.
    sse_max_streams_per_user: int = Field(default=5, ge=1)
    sse_max_stream_seconds: float = Field(default=1800.0, gt=0)
    # Upper bound for the best-effort served-titles write after an addon catalog response.
    served_titles_timeout_seconds: float = Field(default=2.0, gt=0)

    # TMDB rate limiting (#4). The limit is per process: the total request budget divided by
    # the number of worker processes (TMDB asks for roughly 40-50 requests/second per IP).
    tmdb_requests_per_second_total: float = 40.0
    worker_process_count: int = 1
    tmdb_poster_base_url: str = "https://image.tmdb.org/t/p/w500"
    tmdb_max_pages: int = Field(
        default=5,
        ge=1,
        le=500,
        description="Most TMDB list pages one page-walking lookup reads. TMDB serves at most 500.",
    )
    tmdb_cache_max_age_days: int = Field(
        default=7,
        ge=1,
        description=(
            "Days before a cached title is refreshed from TMDB the next time it is requested. "
            "Must be at least 1: 0 would refresh every title on every request."
        ),
    )

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

    # Simkl (#6): register a "Server apps & services" app (AUTH V2) at simkl.com/settings/developer.
    # The redirect URI is never taken from a request. It is SIMKL_REDIRECT_URI, or
    # PUBLIC_BASE_URL + /api/simkl/callback. The API and the worker both need the client secret
    # (the worker refreshes and revokes tokens).
    simkl_client_id: str | None = None
    simkl_client_secret: SecretStr | None = None
    simkl_redirect_uri: str | None = None

    @field_validator("simkl_client_id", "simkl_client_secret", "simkl_redirect_uri", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # docker-compose passes unset variables through as empty strings.
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("simkl_redirect_uri")
    @classmethod
    def _redirect_uri_is_absolute_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("SIMKL_REDIRECT_URI must be an absolute http(s) URL")
        if parts.query or parts.fragment:
            # Simkl compares the URI as a plain string and rejects a query or fragment.
            raise ValueError("SIMKL_REDIRECT_URI must not have a query string or fragment")
        return value

    @property
    def simkl_effective_redirect_uri(self) -> str | None:
        """The `redirect_uri` sent to Simkl's authorize and token endpoints, or `None`."""
        if self.simkl_redirect_uri:
            return self.simkl_redirect_uri
        if self.public_base_url and self.public_base_url.strip():
            return self.public_base_url.strip().rstrip("/") + SIMKL_CALLBACK_PATH
        return None

    @property
    def simkl_oauth_configured(self) -> bool:
        return bool(
            self.simkl_client_id
            and self.simkl_client_secret
            and self.simkl_client_secret.get_secret_value()
            and self.simkl_effective_redirect_uri
        )

    @property
    def tmdb_requests_per_second_per_process(self) -> float:
        return self.tmdb_requests_per_second_total / max(1, self.worker_process_count)
