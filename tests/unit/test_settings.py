from curio.settings import Settings


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/curio")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://u:p@db:5432/curio"
    assert settings.db_pool_max_size == 3
