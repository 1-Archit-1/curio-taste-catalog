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
