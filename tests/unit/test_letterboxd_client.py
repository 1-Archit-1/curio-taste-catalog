import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from curio.clients.letterboxd import (
    LetterboxdClient,
    LetterboxdError,
    LetterboxdNotFoundError,
    normalize_letterboxd_username,
    parse_export_zip,
    parse_rss,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "letterboxd"


def _build_fixture_zip(*, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel in (
            "profile.csv",
            "watched.csv",
            "ratings.csv",
            "diary.csv",
            "watchlist.csv",
            "lists/sci-fi-favorites.csv",
        ):
            zf.write(FIXTURES / rel, arcname=f"{prefix}{rel}")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice", "alice"),
        ("@Alice", "alice"),
        ("https://letterboxd.com/alice/", "alice"),
        ("http://www.letterboxd.com/Alice/rss/", "alice"),
        ("letterboxd.com/alice/films/", "alice"),
    ],
)
def test_normalize_letterboxd_username_accepts_handles_and_urls(raw: str, expected: str):
    assert normalize_letterboxd_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/alice/",
        "https://letterboxd.com/",
        "https://letterboxd.com/films/",
        "bad user!",
    ],
)
def test_normalize_letterboxd_username_rejects_invalid_inputs(raw: str):
    with pytest.raises(LetterboxdError):
        normalize_letterboxd_username(raw)


def test_parse_rss_extracts_movie_and_tv_entries():
    xml_text = (FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    feed = parse_rss(xml_text, username="alice", feed_url="https://letterboxd.com/alice/rss/")

    assert feed.username == "alice"
    assert feed.title == "Letterboxd - alice"
    assert len(feed.entries) == 2

    first, second = feed.entries
    assert first.media_type == "movie"
    assert first.tmdb_id == 603
    assert first.title == "The Matrix"
    assert first.year == 1999
    assert first.rating == 5.0
    assert first.watched_at is not None
    assert first.watched_at.strftime("%Y-%m-%d") == "2026-11-14"
    assert first.entry_url == "https://letterboxd.com/alice/film/the-matrix/"
    assert first.guid == "letterboxd-diary-1001"

    assert second.media_type == "series"
    assert second.tmdb_id == 1396
    assert second.rating == 4.5


async def test_fetch_rss_handles_success_404_and_500(respx_mock: respx.MockRouter):
    xml_text = (FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    respx_mock.get("https://letterboxd.com/alice/rss/").mock(
        return_value=httpx.Response(200, text=xml_text)
    )
    respx_mock.get("https://letterboxd.com/missing/rss/").mock(return_value=httpx.Response(404))
    respx_mock.get("https://letterboxd.com/broken/rss/").mock(return_value=httpx.Response(500))

    async with LetterboxdClient() as client:
        feed = await client.fetch_rss("https://letterboxd.com/alice/")
        assert len(feed.entries) == 2

        with pytest.raises(LetterboxdNotFoundError):
            await client.fetch_rss("missing")

        with pytest.raises(LetterboxdError):
            await client.fetch_rss("broken")


@pytest.mark.parametrize("prefix", ["", "letterboxd-alice-2026/"])
def test_parse_export_zip_extracts_profile_and_lists(prefix: str):
    archive = parse_export_zip(_build_fixture_zip(prefix=prefix))

    assert archive.username == "alice"
    kinds = [r.item_kind for r in archive.profile_rows]
    assert kinds == ["watched", "watched", "rating", "diary", "watchlist"]
    assert archive.profile_rows[4].watched_at is None

    assert len(archive.lists) == 1
    lst = archive.lists[0]
    assert lst.slug == "sci-fi-favorites"
    assert lst.name == "Sci-Fi Favorites"
    assert lst.url == "https://letterboxd.com/alice/list/sci-fi-favorites/"
    assert len(lst.rows) == 1
    assert lst.rows[0].title == "The Matrix"


def test_parse_export_zip_rejects_invalid_and_empty_archives():
    with pytest.raises(LetterboxdError):
        parse_export_zip(b"not-a-zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    with pytest.raises(LetterboxdError):
        parse_export_zip(buf.getvalue())
