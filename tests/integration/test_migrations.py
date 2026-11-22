import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from alembic import command
from sqlalchemy.engine import make_url

from tests.integration.conftest import ALEMBIC_INI, make_alembic_config


@pytest.fixture
def empty_database_url(database_url: str) -> Iterator[str]:
    """A fresh, empty database on the same server, dropped afterwards."""
    name = f"curio_migrate_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_url(database_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_migrations_apply_to_empty_database_and_are_idempotent(empty_database_url):
    cfg = make_alembic_config(empty_database_url)

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    command.check(cfg)

    with psycopg.connect(empty_database_url) as conn:
        versions = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
        has_vector = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert versions == ["0003"]
    assert has_vector is not None


def test_concurrent_runs_apply_each_migration_once(empty_database_url):
    env = {**os.environ, "DATABASE_URL": empty_database_url}

    def run_upgrade(_: int) -> None:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_upgrade, range(4)))

    with psycopg.connect(empty_database_url) as conn:
        versions = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
    assert versions == ["0003"]
