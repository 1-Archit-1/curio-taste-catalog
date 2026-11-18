import pytest

from curio.db.migrate import MIGRATIONS_DIR, discover


def test_discover_sorts_by_version(tmp_path):
    for name in ["0010_later.sql", "0002_second.sql", "0001_first.sql"]:
        (tmp_path / name).write_text("SELECT 1;")
    assert [m.version for m in discover(tmp_path)] == [1, 2, 10]


@pytest.mark.parametrize("names", [["1_bad.sql"], ["0001_a.sql", "0001_b.sql"]])
def test_discover_rejects_bad_names_and_duplicates(tmp_path, names):
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(ValueError):
        discover(tmp_path)


def test_shipped_migrations_are_valid():
    migrations = discover(MIGRATIONS_DIR)
    assert migrations[0].version == 1
