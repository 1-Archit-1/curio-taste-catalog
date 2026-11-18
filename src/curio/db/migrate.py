"""Apply numbered SQL migrations in order: `python -m curio.db.migrate`.

Files are named `NNNN_description.sql`. Applied versions are recorded in
`schema_migrations`. A session-level advisory lock serialises concurrent runs,
and each migration runs in its own transaction, so a failed migration leaves
nothing half-applied and re-running is a no-op once everything is applied.
"""

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from curio.settings import Settings

log = logging.getLogger("curio.db.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary constant shared by every process running migrations.
ADVISORY_LOCK_ID = 7_265_310_001
_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migrations sorted by version. Rejects badly named or duplicate versions."""
    migrations: dict[int, Migration] = {}
    for path in directory.glob("*.sql"):
        match = _FILENAME.match(path.name)
        if not match:
            raise ValueError(f"migration file name must be NNNN_description.sql: {path.name}")
        version = int(match.group(1))
        if version in migrations:
            raise ValueError(f"duplicate migration version {version}: {path.name}")
        migrations[version] = Migration(version, path.stem, path)
    return [migrations[v] for v in sorted(migrations)]


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations; return the versions applied by this call."""
    migrations = discover(directory)
    applied_now: list[int] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    integer PRIMARY KEY,
                    name       text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for m in migrations:
                if m.version in done:
                    continue
                log.info("applying migration %s", m.name)
                with conn.transaction():
                    conn.execute(m.path.read_text())
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                        (m.version, m.name),
                    )
                applied_now.append(m.version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
    return applied_now


def main() -> int:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    applied = migrate(settings.database_url)
    if applied:
        log.info("applied %d migration(s): %s", len(applied), applied)
    else:
        log.info("database is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
