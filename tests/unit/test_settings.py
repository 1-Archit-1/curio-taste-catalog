import pytest
from pydantic import ValidationError

from curio.settings import Settings


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/curio")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://u:p@db:5432/curio"
    assert settings.db_pool_max_size == 3


@pytest.mark.parametrize("size", [0, 101])
def test_embedding_batch_size_must_be_between_1_and_100(size):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://unused", embedding_batch_size=size)


@pytest.mark.parametrize("size", [1, 100])
def test_embedding_batch_size_bounds_are_accepted(size):
    settings = Settings(
        _env_file=None, database_url="postgresql://unused", embedding_batch_size=size
    )
    assert settings.embedding_batch_size == size


def test_rss_refresh_interval_defaults_to_six_hours_and_must_be_positive():
    assert (
        Settings(_env_file=None, database_url="postgresql://unused").rss_refresh_interval_hours == 6
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://unused", rss_refresh_interval_hours=0)


def _simkl_settings(**values: object) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://unused", **values)  # type: ignore[arg-type]


def test_simkl_is_not_configured_without_credentials_or_redirect_uri():
    assert not _simkl_settings().simkl_oauth_configured
    assert not _simkl_settings(simkl_client_id="id", simkl_client_secret="s").simkl_oauth_configured
    assert not _simkl_settings(
        simkl_client_secret="s", public_base_url="https://curio.example"
    ).simkl_oauth_configured
    assert not _simkl_settings(
        simkl_client_id="id", public_base_url="https://curio.example"
    ).simkl_oauth_configured


def test_simkl_redirect_uri_derives_from_public_base_url():
    settings = _simkl_settings(
        simkl_client_id="id", simkl_client_secret="s", public_base_url="https://curio.example/"
    )
    assert settings.simkl_oauth_configured
    assert settings.simkl_effective_redirect_uri == "https://curio.example/api/simkl/callback"


def test_simkl_redirect_uri_setting_wins_over_public_base_url():
    settings = _simkl_settings(
        simkl_client_id="id",
        simkl_client_secret="s",
        simkl_redirect_uri="http://localhost:8000/api/simkl/callback",
        public_base_url="https://curio.example",
    )
    assert settings.simkl_effective_redirect_uri == "http://localhost:8000/api/simkl/callback"


def test_simkl_blank_values_from_compose_count_as_unset(monkeypatch):
    for name in ("SIMKL_CLIENT_ID", "SIMKL_CLIENT_SECRET", "SIMKL_REDIRECT_URI", "PUBLIC_BASE_URL"):
        monkeypatch.setenv(name, "")
    settings = _simkl_settings()
    assert settings.simkl_client_id is None
    assert settings.simkl_client_secret is None
    assert settings.simkl_effective_redirect_uri is None
    assert not settings.simkl_oauth_configured


@pytest.mark.parametrize(
    "uri",
    [
        "curio.example/api/simkl/callback",
        "javascript:alert(1)",
        "https://curio.example/cb?x=1",
        "https://curio.example/cb#frag",
    ],
)
def test_simkl_redirect_uri_must_be_a_plain_absolute_http_url(uri):
    with pytest.raises(ValidationError):
        _simkl_settings(simkl_redirect_uri=uri)


def test_simkl_client_secret_is_not_in_repr():
    settings = _simkl_settings(simkl_client_secret="very-secret-value")
    assert "very-secret-value" not in repr(settings)
