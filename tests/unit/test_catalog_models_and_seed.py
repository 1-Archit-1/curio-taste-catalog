"""Unit tests for catalog ORM models, seed data invariants, and CLI wiring."""

from curio.cli import build_parser
from curio.db.models import (
    Base,
    CatalogCandidate,
    MediaItem,
    Preset,
    Slot,
    Veto,
    WatchHistory,
)
from curio.seed import _DEMO_MOVIES, _DEMO_SERIES


def test_catalog_models_registered_in_metadata() -> None:
    expected = {
        "media_items",
        "presets",
        "slots",
        "catalog_candidates",
        "watch_history",
        "vetoes",
    }
    assert expected.issubset(Base.metadata.tables.keys())


def test_media_items_is_shared_and_per_user_tables_include_user_id_in_pk() -> None:
    assert "user_id" not in MediaItem.__table__.columns
    assert [col.name for col in MediaItem.__table__.primary_key] == ["media_type", "tmdb_id"]
    assert MediaItem.__table__.c.imdb_id.nullable
    assert MediaItem.__table__.c.imdb_id.unique

    for model in (CatalogCandidate, WatchHistory, Veto):
        pk_cols = [col.name for col in model.__table__.primary_key]
        assert pk_cols[-2:] == ["media_type", "tmdb_id"], model.__tablename__

    per_user_models = (Preset, Slot, CatalogCandidate, WatchHistory, Veto)
    for model in per_user_models:
        pk_cols = [col.name for col in model.__table__.primary_key]
        assert "user_id" in pk_cols, f"{model.__tablename__} missing user_id in PK"


def test_catalog_candidates_index_present() -> None:
    index_names = {idx.name for idx in CatalogCandidate.__table__.indexes}
    assert "idx_catalog_candidates_preset_pos" in index_names


def test_demo_seed_catalog_invariants() -> None:
    assert len(_DEMO_MOVIES) >= 25
    assert len(_DEMO_SERIES) >= 25

    all_items = _DEMO_MOVIES + _DEMO_SERIES
    imdb_ids = [str(item["imdb_id"]) for item in all_items]
    assert len(imdb_ids) == len(set(imdb_ids))
    assert all(imdb_id.startswith("tt") for imdb_id in imdb_ids)

    for items in (_DEMO_MOVIES, _DEMO_SERIES):
        tmdb_ids = [item["tmdb_id"] for item in items]
        assert all(isinstance(tmdb_id, int) for tmdb_id in tmdb_ids)
        assert len(tmdb_ids) == len(set(tmdb_ids))

    ratings = [float(item["rating"]) for item in all_items]  # type: ignore[arg-type]
    assert all(4.0 <= r <= 9.0 for r in ratings)

    assert any(item["poster_url"] is None for item in _DEMO_MOVIES)
    assert any(item["poster_url"] is None for item in _DEMO_SERIES)
    assert any(item["poster_url"] is not None for item in _DEMO_MOVIES)
    assert any(item["poster_url"] is not None for item in _DEMO_SERIES)


def test_cli_parses_seed_demo() -> None:
    parser = build_parser()
    args_default = parser.parse_args(["seed-demo"])
    assert args_default.command == "seed-demo"
    assert args_default.username == "demo"

    args_custom = parser.parse_args(["seed-demo", "--username", "alice"])
    assert args_custom.command == "seed-demo"
    assert args_custom.username == "alice"
