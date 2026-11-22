"""Unit tests for Stremio addon protocol helpers and fixed slot definitions."""

import pytest

from curio.services.catalogs import (
    FIXED_SLOTS,
    FIXED_SLOTS_BY_ID,
    parse_skip_extra,
    resolve_poster_url,
)


def test_fixed_slots_definitions() -> None:
    assert len(FIXED_SLOTS) == 6
    assert [(s.slot_id, s.name, s.media_type) for s in FIXED_SLOTS] == [
        ("curio_movie_1", "Curio Movies 1", "movie"),
        ("curio_movie_2", "Curio Movies 2", "movie"),
        ("curio_movie_3", "Curio Movies 3", "movie"),
        ("curio_series_1", "Curio Series 1", "series"),
        ("curio_series_2", "Curio Series 2", "series"),
        ("curio_series_3", "Curio Series 3", "series"),
    ]
    assert set(FIXED_SLOTS_BY_ID.keys()) == {s.slot_id for s in FIXED_SLOTS}


@pytest.mark.parametrize(
    ("extra", "query_skip", "expected"),
    [
        (None, None, 0),
        ("", None, 0),
        (None, 20, 20),
        (None, -5, 0),
        ("skip=20", None, 20),
        ("genre=Drama&skip=40", None, 40),
        ("skip=0&genre=Drama", None, 0),
        ("genre=Drama", 15, 15),
        ("skip=-10", 25, 0),
        ("skip%3D60", None, 60),
    ],
)
def test_parse_skip_extra_valid(extra: str | None, query_skip: int | None, expected: int) -> None:
    assert parse_skip_extra(extra, query_skip=query_skip) == expected


@pytest.mark.parametrize("extra", ["skip=abc", "skip=", "skip=1.5", "genre=Drama&skip=nan"])
def test_parse_skip_extra_invalid(extra: str) -> None:
    with pytest.raises(ValueError, match="invalid skip value"):
        parse_skip_extra(extra)


def test_resolve_poster_url_uses_tmdb_url_when_present() -> None:
    url = "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert resolve_poster_url(url, base_url="http://localhost:8000/") == url


@pytest.mark.parametrize("poster_url", [None, "", "   "])
def test_resolve_poster_url_falls_back_to_svg(poster_url: str | None) -> None:
    resolved = resolve_poster_url(poster_url, base_url="http://localhost:8000/")
    assert resolved == "http://localhost:8000/addon-assets/poster-fallback.svg"
