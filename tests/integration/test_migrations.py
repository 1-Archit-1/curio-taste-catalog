import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from curio.db.migrate import discover, migrate


@pytest.fixture
def empty_database_url(database_url: str) -> Iterator[str]:
    """A fresh, empty database on the same server, dropped afterwards."""
    name = f"curio_migrate_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(database_url, dbname=name)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_migrations_apply_to_empty_database_and_are_idempotent(empty_database_url):
    expected = [m.version for m in discover()]

    assert migrate(empty_database_url) == expected
    assert migrate(empty_database_url) == []

    with psycopg.connect(empty_database_url) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        has_vector = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert versions == expected
    assert has_vector is not None


def test_failed_migration_rolls_back(empty_database_url, tmp_path):
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE ok_table (id int);")
    (tmp_path / "0002_broken.sql").write_text("CREATE TABLE half (id int); SELECT nope;")

    with pytest.raises(psycopg.errors.UndefinedColumn):
        migrate(empty_database_url, tmp_path)

    with psycopg.connect(empty_database_url) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        half = conn.execute("SELECT to_regclass('half')").fetchone()[0]
    assert versions == [1]
    assert half is None


def test_concurrent_runs_apply_each_migration_once(empty_database_url):
    expected = [m.version for m in discover()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: migrate(empty_database_url), range(4)))
    # Exactly one run applies everything; the others wait on the lock and find nothing to do.
    assert sorted(results) == [[], [], [], expected]
