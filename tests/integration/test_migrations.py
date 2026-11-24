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
    assert versions == ["0005"]
    assert has_vector is not None


def test_latest_migration_downgrades_and_upgrades_cleanly(empty_database_url):
    cfg = make_alembic_config(empty_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003")
    with psycopg.connect(empty_database_url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }
        key_columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'user_api_keys'"
            )
        }
    assert tables.isdisjoint({"jobs", "runs", "run_events", "served_titles"})
    assert not key_columns & {"paused_reason", "paused_at"}
    command.upgrade(cfg, "head")
    command.check(cfg)


def _insert_user(conn: psycopg.Connection) -> uuid.UUID:
    row = conn.execute(
        "INSERT INTO users (username, addon_token) VALUES ('mig_user', 'mig_token') RETURNING id"
    ).fetchone()
    assert row is not None
    return row[0]


def test_sources_migration_enforces_ownership_and_downgrades_cleanly(empty_database_url):
    cfg = make_alembic_config(empty_database_url)
    command.upgrade(cfg, "head")
    with psycopg.connect(empty_database_url, autocommit=True) as conn:
        user_id = _insert_user(conn)
        insert_source = (
            "INSERT INTO sources (kind, owner_user_id, external_key, label) "
            "VALUES (%s, %s, %s, 'x')"
        )
        # Public sources have no owner, and two of them cannot share a key (NULLs not distinct).
        conn.execute(insert_source, ("lb_diary_rss", None, "alice"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert_source, ("lb_diary_rss", None, "alice"))
        # Owned sources need an owner, and public ones must not have one.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert_source, ("lb_profile_csv", None, "profile"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert_source, ("lb_diary_rss", user_id, "bob"))
        conn.execute(insert_source, ("lb_profile_csv", user_id, "profile"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert_source, ("lb_profile_csv", user_id, "profile"))
        conn.execute(
            "INSERT INTO jobs (user_id, job_kind) VALUES (%s, 'lb_import')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO jobs (user_id, job_kind) VALUES (%s, 'regenerate')",
            (user_id,),
        )
    command.downgrade(cfg, "0004")
    with psycopg.connect(empty_database_url, autocommit=True) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }
        history_columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'watch_history'"
            )
        }
        kinds = [r[0] for r in conn.execute("SELECT job_kind FROM jobs")]
        assert tables.isdisjoint({"sources", "user_sources", "source_items", "source_uploads"})
        assert "source_id" not in history_columns
        assert kinds == ["regenerate"]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO jobs (user_id, job_kind) SELECT id, 'lb_import' FROM users")
    command.upgrade(cfg, "head")
    command.check(cfg)


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
    assert versions == ["0005"]
